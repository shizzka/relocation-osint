import json
import os
import re
import sys
from datetime import datetime, timezone

# Две особенности Anthropic на Agent API, обе отдают глухое 400 invalid request:
# явный temperature и JSON Schema с ограничениями размера и диапазона.
TEMPERATURE_UNSUPPORTED_PREFIXES = ("anthropic/",)
SCHEMA_CONSTRAINTS_UNSUPPORTED_PREFIXES = ("anthropic/",)
SCHEMA_CONSTRAINT_KEYWORDS = frozenset({"minItems", "maxItems", "minimum", "maximum"})


def force_utf8_stdout():
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def make_client(api_key):
    from perplexity import Perplexity

    return Perplexity(api_key=api_key)


def model_slug(model_name):
    return model_name.replace("/", "_")


def file_slug(value):
    return re.sub(r"[^\w-]+", "_", value, flags=re.UNICODE).strip("_") or "unknown"


def field(source, name, default=None):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def clean_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def supports_temperature(model_name):
    return not model_name.startswith(TEMPERATURE_UNSUPPORTED_PREFIXES)


def strip_schema_keywords(node, keywords):
    if isinstance(node, dict):
        return {key: strip_schema_keywords(value, keywords) for key, value in node.items() if key not in keywords}
    if isinstance(node, list):
        return [strip_schema_keywords(item, keywords) for item in node]
    return node


def schema_for(model_name, schema):
    if model_name.startswith(SCHEMA_CONSTRAINTS_UNSUPPORTED_PREFIXES):
        return strip_schema_keywords(schema, SCHEMA_CONSTRAINT_KEYWORDS)
    return schema


def web_search_tool(search_context_size, search_recency):
    tool = {
        "type": "web_search",
        "search_context_size": search_context_size,
    }
    if search_recency:
        tool["filters"] = {"search_recency_filter": search_recency}
    return tool


def finalize_request(request, temperature=None):
    model_name = request["model"]
    finalized = dict(request)

    response_format = finalized.get("response_format") or {}
    json_schema = response_format.get("json_schema")

    if isinstance(json_schema, dict) and "schema" in json_schema:
        finalized["response_format"] = {
            **response_format,
            "json_schema": {**json_schema, "schema": schema_for(model_name, json_schema["schema"])},
        }

    if not supports_temperature(model_name):
        finalized.pop("temperature", None)
    elif temperature is not None:
        finalized["temperature"] = temperature

    return finalized


def raw_payload(raw_response):
    text = getattr(raw_response, "text", None)
    if isinstance(text, str) and text.strip():
        return json.loads(text)

    decoder = getattr(raw_response, "json", None)
    if callable(decoder):
        return decoder()

    raise RuntimeError("Тело сырого ответа API недоступно ни через text, ни через json()")


def extract_output_text(response):
    direct = field(response, "output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    parts = []
    for item in field(response, "output") or []:
        if field(item, "type") != "message":
            continue
        for content in field(item, "content") or []:
            text = field(content, "text")
            if isinstance(text, str) and text:
                parts.append(text)

    return "".join(parts)


def extract_citations(response):
    citations = {}

    for item in field(response, "output") or []:
        if field(item, "type") != "search_results":
            continue
        for result in field(item, "results") or []:
            try:
                index = int(field(result, "id"))
            except (TypeError, ValueError):
                continue
            citations[index] = {
                "id": index,
                "url": field(result, "url"),
                "title": field(result, "title"),
                "date": field(result, "date"),
                "last_updated": field(result, "last_updated"),
            }

    return [citations[index] for index in sorted(citations)]


def write_log(log_dir, name, payload, kind):
    os.makedirs(log_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{file_slug(name)}_{stamp}_{kind}.json")

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)

    return path


def request_json(client, request, log_dir, log_name):
    model_name = request.get("model", "?")

    try:
        raw = client.responses.with_raw_response.create(**request)
    except Exception as error:
        log_path = write_log(log_dir, log_name, {"request": request, "error": repr(error)}, "error")
        raise RuntimeError(f"Запрос провалился [{model_name}], лог: {log_path}") from error

    try:
        response = raw_payload(raw)
    except Exception as error:
        log_path = write_log(
            log_dir,
            log_name,
            {"request": request, "raw_text": getattr(raw, "text", None), "error": repr(error)},
            "error",
        )
        raise RuntimeError(f"Тело ответа не разобрать как JSON [{model_name}], лог: {log_path}") from error

    headers = getattr(raw, "headers", None)
    log_path = write_log(
        log_dir,
        log_name,
        {
            "request": request,
            "http_status": getattr(raw, "status_code", None),
            "request_id": headers.get("X-Request-ID") if headers is not None else None,
            "response": response,
        },
        "response",
    )

    text = extract_output_text(response)
    if not text.strip():
        raise RuntimeError(
            f"Пустой текст в ответе [{model_name}] (status={field(response, 'status')}, "
            f"incomplete_details={field(response, 'incomplete_details')}). Разбирай лог: {log_path}"
        )

    try:
        parsed = json.loads(clean_json(text))
    except json.JSONDecodeError as error:
        raise ValueError(f"Невалидный JSON в ответе [{model_name}]: {error}. Разбирай лог: {log_path}") from error

    return parsed, extract_citations(response), log_path
