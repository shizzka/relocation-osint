import os
import sys
import json
import time
import glob
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

import ppx

load_dotenv()

PERPLEXITY_KEY = os.getenv("PERPLEXITY_API_KEY")

ppx.force_utf8_stdout()

# Судья не должен быть участником процесса: модели завышают оценку собственным ответам.
JUDGE_MODEL = "google/gemini-3.1-pro-preview"
#JUDGE_MODEL = "xai/grok-4.6"

MAX_STEPS = 8
MAX_OUTPUT_TOKENS = 20000
TEMPERATURE = 0.0

SEARCH_CONTEXT_SIZE = "high"
SEARCH_RECENCY = None
JUDGE_PAUSE_SEC = 3
CRITERIA_COUNT = 20

RAW_DIR = "data/raw"
OUT_DIR = "data/out"
MASTER_FILE = os.path.join(OUT_DIR, "master.json")
JUDGE_LOG_DIR = os.path.join(RAW_DIR, "_judge")
PROMPT_FILE = "prompts/judge.txt"

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "maximum": CRITERIA_COUNT},
                    "name": {"type": "string"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "summary": {"type": "string"}
                },
                "required": ["id", "name", "score", "summary"],
                "additionalProperties": False
            }
        },
        "final_red_flags": {"type": "string"},
        "final_detailed_analysis": {"type": "string"}
    },
    "required": ["resolved_scores", "final_red_flags", "final_detailed_analysis"],
    "additionalProperties": False
}


def discover_sources():
    sources = {}

    for path in sorted(glob.glob(os.path.join(RAW_DIR, "*.json"))):
        if not os.path.isfile(path):
            continue

        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError as error:
                print(f"⚠️ {path}: битый JSON, пропускаем ({error})")
                continue

        if isinstance(data, dict) and data:
            sources[name] = data
        else:
            print(f"⚠️ {path}: пусто или неожиданный формат, пропускаем")

    return sources


def collect_countries(sources):
    countries = []
    for data in sources.values():
        for country in data:
            if country not in countries:
                countries.append(country)
    return countries


def collect_opinions(sources, country):
    opinions = {}
    for name, data in sources.items():
        report = data.get(country)
        if isinstance(report, dict) and report:
            opinions[name] = report
    return opinions


def find_criterion(report, criterion_id):
    for item in report.get("scores") or []:
        if isinstance(item, dict) and item.get("id") == criterion_id:
            return item
    return None


def split_opinions(opinions):
    resolved = []
    discrepancies = []

    for criterion_id in range(1, CRITERIA_COUNT + 1):
        entries = []
        name = ""

        for source, report in opinions.items():
            criterion = find_criterion(report, criterion_id)
            if criterion is None:
                continue
            name = name or str(criterion.get("name") or "")
            entries.append({
                "source": source,
                "score": criterion.get("score"),
                "summary": criterion.get("summary"),
            })

        if not entries:
            continue

        if len({entry["score"] for entry in entries}) == 1:
            resolved.append({
                "id": criterion_id,
                "name": name,
                "score": entries[0]["score"],
                "summary": entries[0]["summary"],
            })
        else:
            discrepancies.append({"id": criterion_id, "name": name, "opinions": entries})

    return resolved, discrepancies


def build_payload(country, opinions, discrepancies):
    return {
        "country": country,
        "discrepancies": discrepancies,
        "red_flags_opinions": [
            {"source": name, "text": report.get("red_flags")} for name, report in opinions.items()
        ],
        "detailed_analysis_opinions": [
            {"source": name, "text": report.get("detailed_analysis")} for name, report in opinions.items()
        ],
    }


def build_input(payload):
    return (
        "ДАННЫЕ ДЛЯ АНАЛИЗА (JSON):\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nСпорные цифры и факты перепроверь поиском в интернете, не полагайся только на правдоподобие "
        "мнений. Верни вердикт одним JSON-объектом строго по заданной схеме, без markdown и пояснений."
    )


def query_judge(client, template, country, payload):
    request = ppx.finalize_request(
        {
            "model": JUDGE_MODEL,
            "instructions": template,
            "input": build_input(payload),
            "max_steps": MAX_STEPS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "tools": [ppx.web_search_tool(SEARCH_CONTEXT_SIZE, SEARCH_RECENCY)],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "judge_verdict", "schema": VERDICT_SCHEMA},
            },
        },
        TEMPERATURE,
    )

    verdict, citations, log_path = ppx.request_json(client, request, JUDGE_LOG_DIR, country)
    print(f"  📝 Ответ судьи сохранён: {log_path}")

    return verdict, citations


def merge_verdict(country, sources, resolved, verdict, citations):
    scores = {item["id"]: item for item in resolved}

    for item in verdict.get("resolved_scores") or []:
        if not isinstance(item, dict):
            continue
        try:
            criterion_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        scores[criterion_id] = {
            "id": criterion_id,
            "name": str(item.get("name") or "").strip(),
            "score": item.get("score"),
            "summary": str(item.get("summary") or "").strip(),
        }

    return {
        "country": country,
        "judge_model": JUDGE_MODEL,
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "scores": [scores[key] for key in sorted(scores)],
        "red_flags": str(verdict.get("final_red_flags") or "").strip(),
        "detailed_analysis": str(verdict.get("final_detailed_analysis") or "").strip(),
        "citations": citations,
    }


def load_master():
    if not os.path.exists(MASTER_FILE):
        return {}

    with open(MASTER_FILE, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError:
            return {}

    return data if isinstance(data, dict) else {}


def save_master(master):
    with open(MASTER_FILE, "w", encoding="utf-8") as handle:
        json.dump(master, handle, ensure_ascii=False, indent=2)


def main():
    if not PERPLEXITY_KEY:
        print("⚠️ Ахтунг: PERPLEXITY_API_KEY не найден в .env файле")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)

    sources = discover_sources()
    if not sources:
        print(f"⚠️ Ахтунг: в {RAW_DIR} нет ни одного пригодного JSON")
        return 1

    print(f"📚 Источники ({len(sources)}): {', '.join(sources)}")
    print(f"⚖️ Судья: {JUDGE_MODEL}, max_steps={MAX_STEPS}")

    if any(name.startswith(JUDGE_MODEL.split("/")[0]) for name in sources):
        print("⚠️ Судья из того же семейства, что и один из источников: ждите симпатий к самому себе")

    with open(PROMPT_FILE, "r", encoding="utf-8") as handle:
        template = handle.read()

    master = load_master()
    client = ppx.make_client(PERPLEXITY_KEY)
    failures = 0

    for country in collect_countries(sources):
        if country in master:
            print(f"⏭️ {country}: вердикт уже есть, пропускаем")
            continue

        opinions = collect_opinions(sources, country)
        if not opinions:
            continue

        print(f"\n⚖️ Суд над страной: {country} (мнений: {len(opinions)})")
        resolved, discrepancies = split_opinions(opinions)
        print(f"  консенсус: {len(resolved)}, спорных: {len(discrepancies)}")

        try:
            verdict, citations = query_judge(client, template, country, build_payload(country, opinions, discrepancies))
        except Exception as error:
            failures += 1
            print(f"  ❌ Ошибка суда: {error}")
            traceback.print_exc()
            continue

        master[country] = merge_verdict(country, sorted(opinions), resolved, verdict, citations)
        save_master(master)
        print(f"  ✓ вердикт записан, критериев: {len(master[country]['scores'])}, источников судьи: {len(citations)}")

        time.sleep(JUDGE_PAUSE_SEC)

    if failures:
        print(f"\n⚠️ Заседание окончено с ошибками: {failures}")
        return 1

    print("\n✅ Судебное заседание окончено. Мастер-JSON готов.")
    return 0


if __name__ == "__main__":
    if "--legacy" in sys.argv:
        sys.exit(main())
    from pipeline import judge_main

    sys.exit(judge_main())
