import os
import sys
import json
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

import ppx

# Загружаем переменные из .env файла
load_dotenv()

PERPLEXITY_KEY = os.getenv("PERPLEXITY_API_KEY")

ppx.force_utf8_stdout()

# Список используемых движков (Agent API, формат provider/model)
MODELS = [
    "perplexity/sonar",
    "openai/gpt-5.6-terra",
    "anthropic/claude-sonnet-5",
]

# Глубина агентского цикла. Без него Agent API даёт ровно 1 шаг:
# движок тратит его на web_search и возвращает ответ без текста.
MAX_STEPS = 8

MAX_OUTPUT_TOKENS = 20000
TEMPERATURE = 0.0

SEARCH_CONTEXT_SIZE = "high"
SEARCH_RECENCY = None
REQUEST_PAUSE_SEC = 4
CRITERIA_COUNT = 20

COUNTRIES_FILE = "countries.txt"
PROMPT_FILE = "prompts/osint.txt"
RAW_DIR = "data/raw"

OSINT_SCHEMA = {
    "type": "object",
    "properties": {
        "country": {"type": "string"},
        "scores": {
            "type": "array",
            "minItems": CRITERIA_COUNT,
            "maxItems": CRITERIA_COUNT,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "maximum": CRITERIA_COUNT},
                    "name": {"type": "string"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "summary": {"type": "string"},
                    "citation_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Идентификаторы источников из search_results, нумерация с 1"
                    }
                },
                "required": ["id", "name", "score", "summary", "citation_indices"],
                "additionalProperties": False
            }
        },
        "red_flags": {"type": "string"},
        "detailed_analysis": {"type": "string"}
    },
    "required": ["country", "scores", "red_flags", "detailed_analysis"],
    "additionalProperties": False
}


def to_int_list(values):
    result = []
    for value in values or []:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def normalize_report(parsed, country, model_name, citations):
    if not isinstance(parsed, dict):
        raise ValueError("Ответ движка не является JSON-объектом")

    if not isinstance(parsed.get("scores"), list):
        raise ValueError("В ответе движка нет массива scores")

    scores = []
    seen = set()

    for item in parsed["scores"]:
        if not isinstance(item, dict):
            continue
        try:
            criterion_id = int(item.get("id"))
            score = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        if criterion_id in seen or not 1 <= criterion_id <= CRITERIA_COUNT:
            continue

        seen.add(criterion_id)
        scores.append({
            "id": criterion_id,
            "name": str(item.get("name") or "").strip(),
            "score": max(0, min(10, score)),
            "summary": str(item.get("summary") or "").strip(),
            "citation_indices": to_int_list(item.get("citation_indices")),
        })

    if len(scores) != CRITERIA_COUNT:
        raise ValueError(f"Ожидалось критериев: {CRITERIA_COUNT}, получено валидных: {len(scores)}")

    scores.sort(key=lambda item: item["id"])

    return {
        "country": str(parsed.get("country") or country).strip() or country,
        "model": model_name,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scores": scores,
        "red_flags": str(parsed.get("red_flags") or "").strip(),
        "detailed_analysis": str(parsed.get("detailed_analysis") or "").strip(),
        "citations": citations,
    }


def build_task(country):
    return (
        f"Страна для анализа: {country}. "
        f"Собери актуальные данные по всем {CRITERIA_COUNT} критериям из системной инструкции: "
        "легализация и ВНЖ, стоимость жизни, налоги, крипта, визы, банки, климат, сервис, медицина, "
        "образование, транспорт, пешеходность, природа, язык, экономика, политика, дети, безопасность, "
        "культура, перспективы. "
        "Верни результат одним JSON-объектом строго по заданной схеме, без markdown и пояснений."
    )


def query_model(client, model_name, country, instructions):
    request = ppx.finalize_request(
        {
            "model": model_name,
            "instructions": instructions,
            "input": build_task(country),
            "max_steps": MAX_STEPS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "tools": [ppx.web_search_tool(SEARCH_CONTEXT_SIZE, SEARCH_RECENCY)],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "osint_report", "schema": OSINT_SCHEMA},
            },
        },
        TEMPERATURE,
    )

    log_dir = os.path.join(RAW_DIR, ppx.model_slug(model_name))
    parsed, citations, log_path = ppx.request_json(client, request, log_dir, country)
    print(f"  📝 Сырой ответ сохранён: {log_path}")

    return normalize_report(parsed, country, model_name, citations)


def aggregate_path(model_name):
    return os.path.join(RAW_DIR, f"{ppx.model_slug(model_name)}.json")


def load_aggregate(model_name):
    path = aggregate_path(model_name)
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError:
            return {}

    return data if isinstance(data, dict) else {}


def save_aggregate(model_name, results):
    with open(aggregate_path(model_name), "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)


def read_countries():
    with open(COUNTRIES_FILE, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def read_prompt_template():
    with open(PROMPT_FILE, "r", encoding="utf-8") as handle:
        return handle.read()


def main():
    if not PERPLEXITY_KEY:
        print("⚠️ Ахтунг: PERPLEXITY_API_KEY не найден в .env файле")
        return 1

    if not MODELS:
        print("⚠️ Ахтунг: список MODELS пуст, собирать нечем")
        return 1

    os.makedirs(RAW_DIR, exist_ok=True)

    countries = read_countries()
    template = read_prompt_template()
    client = ppx.make_client(PERPLEXITY_KEY)

    all_results = {model: load_aggregate(model) for model in MODELS}
    failures = 0

    for country in countries:
        print(f"\n🌍 Парсинг страны: {country}")
        instructions = template.replace("[COUNTRY]", country)

        for model in MODELS:
            if country in all_results[model]:
                print(f"  ⏭️ [{model}]: {country} уже есть в базе, пропускаем")
                continue

            try:
                print(f"  🚀 Сбор данных с помощью [{model}], max_steps={MAX_STEPS}...")
                all_results[model][country] = query_model(client, model, country, instructions)
                save_aggregate(model, all_results[model])
                print(f"  ✓ [{model}] данные записаны в {aggregate_path(model)}")
            except Exception as error:
                failures += 1
                print(f"  ❌ Ошибка на модели [{model}]: {error}")
                traceback.print_exc()

            time.sleep(REQUEST_PAUSE_SEC)

    if failures:
        print(f"\n⚠️ OSINT-сбор завершён с ошибками: {failures}")
        return 1

    print("\n✅ OSINT-сбор успешно завершён!")
    return 0


if __name__ == "__main__":
    if "--legacy" in sys.argv:
        sys.exit(main())
    from pipeline import fetch_main

    sys.exit(fetch_main())
