import json
from collections import Counter

from core import CRITERIA, CRITICAL_IDS, LEGACY_COUNTRY_ALIASES, ROOT, read_json, write_json
from evidence import classify


def main():
    policy = read_json(ROOT / "source_policy.json")
    findings = []
    domains = Counter()
    for path in sorted((ROOT / "data/raw").glob("*.json")) + [ROOT / "data/out/master.json"]:
        for country, report in read_json(path).items():
            items = report.get("scores", [])
            ids = [item.get("id") for item in items]
            missing = sorted(set(CRITERIA) - set(ids))
            if missing or len(ids) != len(set(ids)):
                findings.append({"file": str(path.relative_to(ROOT)), "country": country, "issue": "criterion_ids", "missing": missing, "count": len(ids)})
            citation_map = {item.get("id"): item for item in report.get("citations", [])}
            normalized_country = LEGACY_COUNTRY_ALIASES.get(country, country)
            for item in items:
                refs = item.get("citation_indices", [])
                if not refs:
                    findings.append({"file": str(path.relative_to(ROOT)), "country": country, "criterion_id": item.get("id"), "issue": "no_provenance"})
                invalid = [index for index in refs if index not in citation_map]
                if invalid:
                    findings.append({"file": str(path.relative_to(ROOT)), "country": country, "criterion_id": item.get("id"), "issue": "unknown_citations", "indices": invalid})
                if item.get("id") in CRITICAL_IDS and not any(classify(citation_map[index].get("url"), normalized_country, policy)[0] == "official" for index in refs if index in citation_map):
                    findings.append({"file": str(path.relative_to(ROOT)), "country": country, "criterion_id": item.get("id"), "issue": "critical_without_official_source"})
            for citation in citation_map.values():
                source_type, _ = classify(citation.get("url"), normalized_country, policy)
                domains[source_type] += 1
    output = {"source_classification": dict(domains), "findings": findings, "limitations": "Проверка структуры и происхождения, не истинности правовых фактов. Старые отчёты не являются city layer."}
    write_json(ROOT / "data/out/audit.json", output)
    print(json.dumps({"source_classification": output["source_classification"], "finding_counts": dict(Counter(item["issue"] for item in findings))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
