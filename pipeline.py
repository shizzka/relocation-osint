import argparse
import time

from core import CRITERIA, ROOT, criterion_ids, destination_key, destinations, downgrade_invalid_citations, fingerprint, now, read_json, validate_report, validate_scores, write_json
from evidence import Retriever, classify
from providers import Client, RateLimitError, accounts, judge_model, models, scout_accounts, settings


RAW = ROOT / "data/raw_v2"
OUT = ROOT / "data/out"
PROFILE = {
    "citizenship": settings().get("RELOCATION_PROFILE_CITIZENSHIP", ""),
    "income": settings().get("RELOCATION_PROFILE_INCOME", ""),
    "plans": settings().get("RELOCATION_PROFILE_PLANS", ""),
}
RATE_LIMIT_STATE = OUT / "rate-limit.json"
FETCH_STATE = OUT / "fetch-state.json"


def record_rate_limit(error, stage, target, layer, model=None):
    write_json(RATE_LIMIT_STATE, {
        "detected_at": now(),
        "stage": stage,
        "country": target.get("country"),
        "city": target.get("city"),
        "layer": layer,
        "model": model,
        "retry_after_seconds": error.retry_after,
    })


def arguments(stage):
    parser = argparse.ArgumentParser(description="Relocation OSINT: " + stage)
    parser.add_argument("--destinations", type=str, help="JSON списка country/city")
    parser.add_argument("--country", help="Ограничить одной страной")
    parser.add_argument("--city", help="Ограничить одним городом")
    parser.add_argument("--plan", action="store_true", help="Показать план без сетевых запросов")
    parser.add_argument("--refresh", action="store_true", help="Обновить evidence и пересчитать отчёты")
    return parser.parse_args()


def targets_for(args):
    return [target for target in destinations(args.destinations) if (not args.country or args.country == target["country"]) and (not args.city or args.city == target["city"])]


def tasks_for(targets):
    tasks = {}
    for target in targets:
        tasks[("country", target["country"])] = {"country": target["country"]}
        tasks[("city", destination_key(target))] = target
    return tasks


def raw_path(model):
    return RAW / (model.replace("/", "_").replace(":", "_") + ".json")


def task_key(target, layer):
    return target["country"] if layer == "country" else destination_key(target)


def report_signature(target, layer, pack, instructions, model):
    return report_signature_from_hash(target, layer, pack["evidence_hash"], instructions, model)


def report_signature_from_hash(target, layer, evidence_hash, instructions, model):
    return fingerprint({"target": target, "layer": layer, "evidence_hash": evidence_hash, "instructions": instructions, "model": model, "profile": PROFILE, "criteria": CRITERIA, "schema_version": 2})


def fetch_config_hash(tasks, configured_models, instructions, policy):
    return fingerprint({
        "tasks": [{"layer": layer, "key": key, "target": target} for (layer, key), target in tasks.items()],
        "models": configured_models,
        "instructions": instructions,
        "profile": PROFILE,
        "criteria": CRITERIA,
        "policy": policy,
        "schema_version": 1,
    })


def fetch_task_id(layer, key):
    return f"{layer}:{key}"


def cached_report_is_current(report, target, layer, instructions, model, policy):
    if not isinstance(report, dict) or not isinstance(report.get("evidence_hash"), str):
        return False
    expected = report_signature_from_hash(target, layer, report["evidence_hash"], instructions, model)
    if report.get("input_hash") != expected:
        return False
    try:
        validate_report(report, target, layer, report.get("citations", []))
        return all(classify(citation.get("url"), target["country"], policy)[1] for citation in report.get("citations", []))
    except (ValueError, KeyError, TypeError):
        return False


def bootstrap_completed_tasks(tasks, configured_models, results, instructions, policy):
    completed = set()
    for (layer, key), target in tasks.items():
        if all(cached_report_is_current(results[model].get(key), target, layer, instructions, model, policy) for model in configured_models):
            completed.add(fetch_task_id(layer, key))
    return completed


def write_fetch_state(run_id, config_hash, completed, total, started_at, fetch_complete=False, pipeline_complete=False):
    write_json(FETCH_STATE, {
        "schema_version": 1,
        "run_id": run_id,
        "config_hash": config_hash,
        "started_at": started_at,
        "updated_at": now(),
        "completed_tasks": sorted(completed),
        "completed_count": len(completed),
        "total_tasks": total,
        "fetch_complete": fetch_complete,
        "pipeline_complete": pipeline_complete,
    })


def fetch_main():
    args = arguments("сбор")
    targets = targets_for(args)
    tasks = tasks_for(targets)
    configured_models = models()
    if not tasks or not configured_models:
        print("Нет направлений или моделей для сбора")
        return 1
    if args.plan:
        print(f"Направлений: {len(targets)}, country/city отчётов на модель: {len(tasks)}")
        print("Исследователи: " + ", ".join(configured_models))
        for (layer, key) in tasks:
            print(f"  {layer}: {key}, критериев: {len(criterion_ids(layer))}")
        return 0
    instructions = (ROOT / "prompts/scout_v2.txt").read_text(encoding="utf-8")
    retriever = Retriever()
    results = {model: read_json(raw_path(model)) for model in configured_models}
    clients = {model: Client(scout_accounts(model)) for model in configured_models}
    run_id = settings().get("RELOCATION_RUN_ID") or fingerprint({"started_at": now(), "targets": targets})
    config_hash = fetch_config_hash(tasks, configured_models, instructions, retriever.policy)
    existing_state = read_json(FETCH_STATE)
    if not args.refresh and existing_state.get("run_id") == run_id and existing_state.get("config_hash") == config_hash:
        completed = set(existing_state.get("completed_tasks", []))
        started_at = existing_state.get("started_at") or now()
    elif not args.refresh and not existing_state:
        completed = bootstrap_completed_tasks(tasks, configured_models, results, instructions, retriever.policy)
        started_at = now()
    else:
        completed = set()
        started_at = now()
    valid_task_ids = {fetch_task_id(layer, key) for layer, key in tasks}
    completed &= valid_task_ids
    write_fetch_state(run_id, config_hash, completed, len(tasks), started_at)
    print(f"Checkpoint сбора: {len(completed)}/{len(tasks)}; run_id={run_id[:8]}", flush=True)
    failures = 0
    for (layer, key), target in tasks.items():
        current_task_id = fetch_task_id(layer, key)
        if current_task_id in completed:
            continue
        print(f"Сбор {layer}: {key}", flush=True)
        try:
            pack = retriever.pack(target, layer, args.refresh, require_provider_metadata=True)
        except RateLimitError as error:
            record_rate_limit(error, "search", target, layer)
            print(f"  Поиск временно ограничен: {error}", flush=True)
            return 75
        except Exception as error:
            print(f"  Поиск не удался: {error}", flush=True)
            failures += 1
            continue
        for model in configured_models:
            signature = report_signature(target, layer, pack, instructions, model)
            cached = results[model].get(key)
            if cached and not args.refresh and cached.get("input_hash") == signature:
                try:
                    validate_report(cached, target, layer, pack["citations"])
                    print(f"  {model}: актуальный отчёт уже есть", flush=True)
                    continue
                except (ValueError, KeyError, TypeError):
                    pass
            payload = {
                "target": target, "layer": layer, "profile": PROFILE, "as_of": now(),
                "criteria": [{"id": criterion_id, "name": CRITERIA[criterion_id][0]} for criterion_id in criterion_ids(layer)],
                "allowed_citation_ids_by_criterion": {
                    str(criterion_id): [citation["id"] for citation in pack["citations"] if criterion_id in citation["criterion_ids"]]
                    for criterion_id in criterion_ids(layer)
                },
                "evidence": pack["citations"],
            }
            try:
                print(f"  {model}: запрос", flush=True)
                report = clients[model].json_report(model, instructions, payload, lambda parsed: validate_report(downgrade_invalid_citations(parsed, pack["citations"]), target, layer, pack["citations"]), RAW / "logs" / fingerprint({"key": key, "model": model, "time": now()}))
                report.update({"model": model, "fetched_at": now(), "input_hash": signature, "evidence_hash": pack["evidence_hash"], "retrieval_provider": pack["retrieval_provider"], "retrieval_distribution": pack.get("retrieval_distribution", {}), "independent_retrieval": False})
                results[model][key] = report
                write_json(raw_path(model), results[model])
                print(f"  {model}: сохранено", flush=True)
            except Exception as error:
                if isinstance(error, RateLimitError):
                    record_rate_limit(error, "scout", target, layer, model)
                    print(f"  {model}: временное ограничение API: {error}", flush=True)
                    return 75
                failures += 1
                print(f"  {model}: {error}", flush=True)
            time.sleep(float(settings().get("RELOCATION_PAUSE_SECONDS", "1")))
        if all(
            cached_report_is_current(results[model].get(key), target, layer, instructions, model, retriever.policy)
            for model in configured_models
        ):
            completed.add(current_task_id)
            write_fetch_state(run_id, config_hash, completed, len(tasks), started_at)
    fetch_complete = len(completed) == len(tasks)
    write_fetch_state(run_id, config_hash, completed, len(tasks), started_at, fetch_complete=fetch_complete)
    return 1 if failures or not fetch_complete else 0


def collect_opinions(target, layer):
    opinions = {}
    for model in models():
        report = read_json(raw_path(model)).get(task_key(target, layer))
        if report:
            validate_report(report, target, layer, report.get("citations", []))
            opinions[model] = report
    return opinions


def combine_evidence(opinions, fresh):
    sources = {}
    mappings = {}
    for model, report in list(opinions.items()) + [("judge_search", fresh)]:
        mapping = {}
        for citation in report.get("citations", []):
            url = citation["url"]
            if url not in sources:
                sources[url] = {**citation, "id": len(sources) + 1}
            else:
                sources[url]["criterion_ids"] = sorted(set(sources[url]["criterion_ids"] + citation["criterion_ids"]))
            if model == "judge_search":
                sources[url].update({key: value for key, value in citation.items() if key not in ("id", "criterion_ids")})
            mapping[citation["id"]] = sources[url]["id"]
        mappings[model] = mapping
    remapped = {}
    for model, report in opinions.items():
        remapped[model] = {
            "provenance": report.get("provenance", {}),
            "scores": [{**item, "citation_indices": [mappings[model][index] for index in item["citation_indices"]]} for item in report["scores"]],
            "red_flags": report["red_flags"], "detailed_analysis": report["detailed_analysis"],
        }
    return list(sources.values()), remapped


def judge_layer(client, retriever, target, layer, cache, instructions, refresh=False):
    key = task_key(target, layer)
    opinions = collect_opinions(target, layer)
    if not opinions:
        raise ValueError(f"Нет исследовательских отчётов для {layer}: {key}")
    for report in opinions.values():
        for citation in report.get("citations", []):
            if not classify(citation["url"], target["country"], retriever.policy)[1]:
                raise ValueError("Изменилась политика источников: сначала повтори сбор")
    pack = retriever.pack(target, layer, refresh)
    citations, remapped = combine_evidence(opinions, pack)
    signature = fingerprint({"opinions": opinions, "evidence": citations, "instructions": instructions, "judge": judge_model(), "criteria": CRITERIA, "schema_version": 2})
    if not refresh and cache.get(key, {}).get("input_hash") == signature:
        existing = cache[key]
        validate_scores(existing["scores"], criterion_ids(layer), existing["citations"])
        return existing
    payload = {
        "target": target, "layer": layer, "profile": PROFILE, "as_of": now(),
        "criteria": [{"id": criterion_id, "name": CRITERIA[criterion_id][0]} for criterion_id in criterion_ids(layer)],
        "allowed_citation_ids_by_criterion": {
            str(criterion_id): [citation["id"] for citation in citations if criterion_id in citation["criterion_ids"]]
            for criterion_id in criterion_ids(layer)
        },
        "opinions": remapped, "evidence": citations,
        "independent_retrieval": False,
        "instruction": "Проверь ВСЕ критерии, включая совпавшие оценки, и полноту источников. Совпадение баллов не доказывает факты.",
    }

    def validator(parsed):
        if not isinstance(parsed, dict):
            raise ValueError("Вердикт должен быть объектом")
        report = {"country": target["country"], "city": target.get("city"), "scores": parsed.get("resolved_scores"), "red_flags": parsed.get("final_red_flags"), "detailed_analysis": parsed.get("final_detailed_analysis")}
        return validate_report(report, target, layer, citations)

    report = client.json_report(judge_model(), instructions, payload, validator, RAW / "_judge" / fingerprint({"key": key, "time": now()}), thinking=True)
    identities = {(item.get("provenance", {}).get("base_url"), item.get("provenance", {}).get("actual_model", model)) for model, item in opinions.items()}
    report.update({"judge_model": judge_model(), "judged_at": now(), "input_hash": signature, "sources": list(opinions), "independent_model_count": len(identities), "retrieval_provider": pack["retrieval_provider"], "retrieval_distribution": pack.get("retrieval_distribution", {}), "independent_retrieval": False, "research_provenance": {model: item.get("provenance", {}) for model, item in opinions.items()}})
    if len(identities) < 2:
        for item in report["scores"]:
            item["confidence"] = "low"
    cache[key] = report
    write_json(OUT / ("judged_" + layer + "_v2.json"), cache)
    return report


def assemble(target, country, city):
    scores = []
    citations = []
    for report in (country, city):
        offset = len(citations)
        citations.extend({**item, "id": item["id"] + offset} for item in report["citations"])
        scores.extend({**item, "citation_indices": [index + offset for index in item["citation_indices"]]} for item in report["scores"])
    scores = validate_scores(scores, list(CRITERIA), citations)
    for item in scores:
        layer_report = country if item["scope"] == "country" else city
        if layer_report["independent_model_count"] < 2:
            item["confidence"] = "low"
    return {
        **target, "schema_version": 2, "scores": scores, "citations": citations,
        "red_flags": country["red_flags"] + "\n" + city["red_flags"],
        "detailed_analysis": "Страна:\n" + country["detailed_analysis"] + "\n\nГород:\n" + city["detailed_analysis"],
        "country_layer": country, "city_layer": city,
        "coverage": sum(item["score"] is not None for item in scores) / len(CRITERIA),
        "confidence": "low" if any(item["confidence"] == "low" for item in scores) else "medium",
        "independent_retrieval": False, "judged_at": now(),
        "input_hash": fingerprint([country["input_hash"], city["input_hash"]]),
    }


def judge_main():
    args = arguments("суд")
    targets = targets_for(args)
    if not targets:
        print("Нет направлений для анализа")
        return 1
    if args.plan:
        print(f"Судья: {judge_model()}, thinking=enabled/high; направлений: {len(targets)}")
        print("Country layer проверяется один раз на страну; оцениваются все критерии")
        return 0
    client = Client(accounts(judge=True))
    retriever = Retriever()
    instructions = (ROOT / "prompts/judge_v2.txt").read_text(encoding="utf-8")
    master = read_json(OUT / "master_v2.json")
    caches = {layer: read_json(OUT / ("judged_" + layer + "_v2.json")) for layer in ("country", "city")}
    country_reports = {}
    failures = 0
    for target in targets:
        key = destination_key(target)
        print("Суд: " + key, flush=True)
        try:
            if target["country"] not in country_reports:
                country_reports[target["country"]] = judge_layer(client, retriever, {"country": target["country"]}, "country", caches["country"], instructions, args.refresh)
            city = judge_layer(client, retriever, target, "city", caches["city"], instructions, args.refresh)
            master[key] = assemble(target, country_reports[target["country"]], city)
            write_json(OUT / "master_v2.json", master)
        except RateLimitError as error:
            record_rate_limit(error, "judge", target, "target")
            print(f"  Судья временно ограничен: {error}", flush=True)
            return 75
        except Exception as error:
            failures += 1
            print(f"  Ошибка: {error}", flush=True)
    return 1 if failures else 0
