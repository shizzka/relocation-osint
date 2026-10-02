import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CRITERIA = {
    1: ("Легализация", "country", "immigration residence citizenship Russian passport"),
    2: ("Аренда и стоимость жизни", "city", "apartment rent two bedroom monthly living costs"),
    3: ("Налоги", "country", "tax resident foreign income cryptocurrency tax treaty Russia"),
    4: ("Крипта", "country", "cryptocurrency USDT regulation AML cash out"),
    5: ("Виза", "country", "entry visa Russian citizens official requirements"),
    6: ("Банки", "country", "bank account Russian citizens residence KYC AML"),
    7: ("Климат", "city", "climate monthly temperature humidity PM2.5 flood risk"),
    8: ("Сервис", "city", "delivery internet utilities taxi services"),
    9: ("Медицина", "city", "hospital maternity healthcare insurance waiting times"),
    10: ("Образование", "city", "international school kindergarten tuition fees"),
    11: ("Транспорт", "city", "public transport fares routes car dependence"),
    12: ("Пешеходность", "city", "walkability sidewalks stroller accessibility"),
    13: ("Природа", "city", "parks green space beaches environment"),
    14: ("Язык", "city", "language English Russian access public services"),
    15: ("Экономика", "country", "inflation currency economic outlook IMF"),
    16: ("Геополитические и правовые риски", "country", "sanctions geopolitical legal risks Russian citizens"),
    17: ("Дети", "city", "family children playground childcare maternity"),
    18: ("Безопасность", "city", "crime statistics neighborhood safety natural hazards"),
    19: ("Культура", "city", "expat discrimination community foreign residents"),
    20: ("Перспективы", "country", "long term demographic economic political outlook"),
}
CRITICAL_IDS = {1, 3, 4, 5, 6, 16}
COUNTRY_ALIASES = {}
CITY_ALIASES = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


_GEOGRAPHY_ALIASES = read_json(ROOT / "geography_aliases.json", {})
COUNTRY_ALIASES = _GEOGRAPHY_ALIASES.get("countries", {})
CITY_ALIASES = _GEOGRAPHY_ALIASES.get("cities", {})
LEGACY_COUNTRY_ALIASES = _GEOGRAPHY_ALIASES.get("legacy_countries", {})


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def criterion_ids(layer):
    return [criterion_id for criterion_id, definition in CRITERIA.items() if definition[1] == layer]


def validate_scores(items, expected_ids, citations):
    if not isinstance(items, list):
        raise ValueError("scores должен быть массивом")
    sources = {item["id"]: item for item in citations}
    scores = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Критерий должен быть объектом")
        criterion_id = item.get("id")
        if type(criterion_id) is not int or criterion_id not in expected_ids or criterion_id in scores:
            raise ValueError(f"Неожиданный или повторный id критерия: {criterion_id}")
        if any(key not in item for key in ("score", "summary", "citation_indices")):
            raise ValueError(f"Критерий {criterion_id}: нужны score, summary, citation_indices")
        score = item.get("score")
        if score is not None and (type(score) is not int or not 0 <= score <= 10):
            raise ValueError(f"Критерий {criterion_id}: score должен быть целым 0..10 или null")
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"Критерий {criterion_id}: требуется summary")
        indices = item.get("citation_indices", [])
        if not isinstance(indices, list) or any(type(index) is not int or index not in sources for index in indices):
            raise ValueError(f"Критерий {criterion_id}: неизвестная ссылка")
        if any(criterion_id not in sources[index].get("criterion_ids", expected_ids) for index in indices):
            raise ValueError(f"Критерий {criterion_id}: ссылка относится к другому критерию")
        indices = sorted(set(indices))
        if score is not None and not indices:
            raise ValueError(f"Критерий {criterion_id}: оценка без источников; используй null")
        primary = any(sources[index].get("source_type") == "official" for index in indices)
        trusted = any(sources[index].get("source_score", 0) >= 2 for index in indices)
        confidence = "medium" if score is not None and trusted else "low"
        if criterion_id in CRITICAL_IDS and not primary:
            confidence = "low"
        scores[criterion_id] = {
            "id": criterion_id,
            "name": CRITERIA[criterion_id][0],
            "scope": CRITERIA[criterion_id][1],
            "score": score,
            "summary": summary.strip(),
            "citation_indices": indices,
            "confidence": confidence,
            "manual_check": criterion_id in CRITICAL_IDS or score is None,
        }
    if set(scores) != set(expected_ids):
        raise ValueError(f"Не хватает критериев: {sorted(set(expected_ids) - set(scores))}")
    return [scores[criterion_id] for criterion_id in sorted(scores)]


def validate_report(parsed, target, layer, citations):
    if not isinstance(parsed, dict) or not matches_location(parsed.get("country"), target["country"], COUNTRY_ALIASES):
        raise ValueError("Ответ относится к другой стране")
    if layer == "city" and not matches_location(parsed.get("city"), target["city"], CITY_ALIASES):
        raise ValueError("Ответ относится к другому городу")
    if layer == "country" and parsed.get("city") is not None:
        raise ValueError("Country layer не должен описывать отдельный город")
    scores = validate_scores(parsed.get("scores"), criterion_ids(layer), citations)
    for name in ("red_flags", "detailed_analysis"):
        if not isinstance(parsed.get(name), str) or not parsed[name].strip():
            raise ValueError(f"Требуется непустой {name}")
    return {**parsed, "country": target["country"], "city": target.get("city"), "scores": scores, "citations": citations, "layer": layer, "schema_version": 2}


def downgrade_invalid_citations(parsed, citations):
    """Preserve a report while marking unsupported claims as unknown."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("scores"), list):
        return parsed
    result = deepcopy(parsed)
    sources = {item.get("id"): item for item in citations if isinstance(item, dict)}
    warnings = []
    for item in result["scores"]:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        original = item.get("citation_indices")
        if not isinstance(original, list):
            continue
        valid = [index for index in original if type(index) is int and index in sources and item["id"] in sources[index].get("criterion_ids", [])]
        if len(valid) != len(original):
            item["score"] = None
            item["citation_indices"] = sorted(set(valid))
            item["summary"] = str(item.get("summary") or "").strip() + " [Оценка снята: модель указала нерелевантный источник.]"
            warnings.append({"criterion_id": item["id"], "invalid_citation_indices": original})
    if warnings:
        result["citation_validation_warnings"] = warnings
    return result


def matches_location(value, expected, aliases):
    if not isinstance(value, str):
        return False
    return value.strip().casefold() in {expected.casefold(), *aliases.get(expected, set())}


def destinations(path=None):
    values = read_json(path or ROOT / "destinations.json", [])
    seen = set()
    for target in values:
        if not isinstance(target, dict) or any(not isinstance(target.get(key), str) or not target[key].strip() for key in ("country", "city")):
            raise ValueError("Каждое направление должно содержать country и city")
        key = (target["country"], target["city"])
        if key in seen or " / " in target["country"] or " / " in target["city"]:
            raise ValueError(f"Повторное или неоднозначное направление: {key}")
        seen.add(key)
    return values


def destination_key(target):
    return f'{target["country"]} / {target["city"]}'
