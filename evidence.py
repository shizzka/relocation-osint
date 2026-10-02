import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from core import CRITERIA, ROOT, criterion_ids, fingerprint, now, read_json, write_json
from providers import Client, ProviderError, RateLimitError, retry_after_seconds, settings


class TavilySearch:
    def __init__(self):
        values = settings()
        self.keys = []
        seen = set()
        for index in range(1, 10):
            name = "TAVILY_API_KEY" if index == 1 else f"TAVILY{index}_API_KEY"
            key = values.get(name)
            if key and key not in seen:
                self.keys.append((name.lower().replace("_api_key", ""), key))
                seen.add(key)
        self.session = requests.Session()
        self.session.trust_env = False
        self.active = 0
        self.remaining = [None] * len(self.keys)
        self.last_call = {}
        self.usage_month = datetime.now(timezone.utc).strftime("%Y-%m")
        self.usage_path = ROOT / "data/out/tavily-usage-local.json"
        saved_usage = read_json(self.usage_path)
        self.local_used = saved_usage.get("by_account", {}) if saved_usage.get("month") == self.usage_month else {}
        self._collect_evidence_usage()
        self._load_usage()

    @property
    def available(self):
        return bool(self.keys)

    def _load_usage(self):
        for index, (name, key) in enumerate(self.keys):
            try:
                response = self.session.get(
                    "https://api.tavily.com/usage",
                    headers={"Authorization": "Bearer " + key},
                    timeout=(8, 20),
                )
                if not response.ok:
                    print(f"Tavily {name}: usage HTTP {response.status_code}; остаток неизвестен", flush=True)
                    continue
                data = response.json()
                account = data.get("account") or {}
                key_usage = data.get("key") or {}
                plan_limit = account.get("plan_limit")
                plan_used = account.get("plan_usage")
                paygo_limit = account.get("paygo_limit")
                paygo_used = account.get("paygo_usage")
                reported_used = max(
                    [value for value in (plan_used, key_usage.get("usage")) if isinstance(value, int)]
                    or [0]
                )
                local_used = int(self.local_used.get(name, 0))
                remaining = None
                if isinstance(plan_limit, int) and isinstance(plan_used, int):
                    remaining = max(0, plan_limit - max(reported_used, local_used))
                    if isinstance(paygo_limit, int) and isinstance(paygo_used, int):
                        remaining += max(0, paygo_limit - paygo_used)
                elif isinstance(key_usage.get("limit"), int) and isinstance(key_usage.get("usage"), int):
                    remaining = max(0, key_usage["limit"] - max(reported_used, local_used))
                self.remaining[index] = remaining
                plan = account.get("current_plan", "план не указан")
                if remaining is None:
                    print(f"Tavily {name}: {plan}; остаток API не сообщил", flush=True)
                else:
                    print(f"Tavily {name}: {plan}; использовано по API {reported_used}, локально {local_used}; оценка остатка {remaining}", flush=True)
            except (requests.RequestException, ValueError, TypeError) as error:
                print(f"Tavily {name}: usage недоступен ({type(error).__name__}); остаток неизвестен", flush=True)

    def _collect_evidence_usage(self):
        evidence_dir = ROOT / "data/evidence"
        if not evidence_dir.exists():
            return
        observed = {}
        for path in evidence_dir.glob("*.json"):
            try:
                pack = read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(pack, dict):
                continue
            for query in pack.get("queries", []):
                if not isinstance(query, dict) or query.get("provider") != "tavily/search":
                    continue
                stamp = query.get("retrieved_at") or pack.get("retrieved_at")
                try:
                    query_month = datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m")
                except (TypeError, ValueError):
                    continue
                if query_month != self.usage_month:
                    continue
                name = query.get("account")
                if name:
                    observed[name] = observed.get(name, 0) + int(query.get("credits_used", 1))
        for name, used in observed.items():
            self.local_used[name] = max(int(self.local_used.get(name, 0)), used)

    def search(self, query, max_results=6, include_domains=None):
        if not self.keys:
            raise ProviderError("Не настроен TAVILY_API_KEY")
        attempted = []
        retry_delays = []
        order = [(self.active + offset) % len(self.keys) for offset in range(len(self.keys))]
        for index in order:
            name, key = self.keys[index]
            if self.remaining[index] == 0:
                attempted.append(name + ":quota")
                continue
            try:
                response = self.session.post(
                    "https://api.tavily.com/search",
                    json={
                        "query": query,
                        "search_depth": "basic",
                        "max_results": max_results,
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_published_date": True,
                        "include_domains": include_domains or [],
                        "include_domains_mode": "restrict",
                    },
                    headers={"Authorization": "Bearer " + key},
                    timeout=(10, 90),
                )
            except requests.RequestException as error:
                attempted.append(name + ":network")
                continue
            if response.status_code == 429:
                attempted.append(name + ":429")
                delay = retry_after_seconds(response)
                if delay is not None:
                    retry_delays.append(delay)
                continue
            if response.status_code in (401, 402, 403, 432, 433):
                attempted.append(name + ":" + str(response.status_code))
                if response.status_code in (432, 433):
                    self.remaining[index] = 0
                continue
            if response.status_code >= 500:
                attempted.append(name + ":" + str(response.status_code))
                continue
            if not response.ok:
                attempted.append(name + ":" + str(response.status_code))
                continue
            try:
                data = response.json()
            except ValueError:
                attempted.append(name + ":invalid_response")
                continue
            results = data.get("results")
            if not isinstance(results, list):
                attempted.append(name + ":invalid_response")
                continue
            usage = data.get("usage") or {}
            credits = usage.get("credits", 1) if isinstance(usage, dict) else 1
            try:
                credits = max(1, int(credits))
            except (TypeError, ValueError):
                credits = 1
            if self.remaining[index] is not None:
                self.remaining[index] = max(0, self.remaining[index] - credits)
            self.local_used[name] = int(self.local_used.get(name, 0)) + credits
            write_json(self.usage_path, {"month": self.usage_month, "by_account": self.local_used, "updated_at": now()})
            self.active = index
            self.last_call = {
                "provider": "tavily/search",
                "account": name,
                "credits_used": credits,
                "retrieved_at": now(),
            }
            return {
                "results": [
                    {
                        "url": item.get("url"),
                        "title": item.get("title", ""),
                        "content": item.get("content", ""),
                        "date": item.get("published_date"),
                    }
                    for item in results if isinstance(item, dict)
                ]
            }
        if attempted and all(item.endswith(":429") for item in attempted):
            retry_after = min(retry_delays) if len(retry_delays) == len(attempted) else None
            raise RateLimitError("Все ключи Tavily временно ограничены", retry_after)
        raise ProviderError("Tavily недоступен: " + ", ".join(attempted))


class BalancedSearch:
    def __init__(self, ollama=None, enable_tavily=True):
        self.ollama = ollama or Client()
        self.tavily = TavilySearch() if enable_tavily else None
        self.cooldown_until = {"ollama": 0.0, "tavily": 0.0}
        self.failure_streak = {"ollama": 0, "tavily": 0}
        self.cooldown_seconds = {"ollama": 900, "tavily": 900}
        self.notified = set()
        self.last_call = {}

    @staticmethod
    def provider_available(provider, ollama, tavily):
        return ollama is not None if provider == "ollama" else bool(tavily and tavily.available)

    def _failure(self, provider, error):
        self.failure_streak[provider] += 1
        delay = error.retry_after if isinstance(error, RateLimitError) else None
        if not isinstance(delay, (int, float)) or delay <= 0:
            delay = self.cooldown_seconds[provider]
        self.cooldown_until[provider] = time.monotonic() + delay
        self.cooldown_seconds[provider] = min(3600, max(900, int(delay) * 2))
        if provider not in self.notified:
            print(f"Поиск {provider}: временно недоступен; проверю снова примерно через {int(delay)} сек.", flush=True)
            self.notified.add(provider)

    def _success(self, provider, metadata):
        self.failure_streak[provider] = 0
        self.cooldown_until[provider] = 0.0
        self.cooldown_seconds[provider] = 900
        self.notified.discard(provider)
        self.last_call = metadata

    def search(self, query, preferred, include_domains=None, ollama_query=None):
        ollama = self.ollama
        tavily = self.tavily
        search_provider = settings().get("RELOCATION_SEARCH_PROVIDER", "balanced").strip().lower()
        if search_provider == "tavily":
            providers = ["tavily"]
        elif search_provider == "ollama":
            providers = ["ollama"]
        else:
            providers = [preferred, "tavily" if preferred == "ollama" else "ollama"]
        errors = []
        for provider in providers:
            if not self.provider_available(provider, ollama, tavily):
                continue
            if time.monotonic() < self.cooldown_until[provider]:
                continue
            try:
                if provider == "ollama":
                    result = ollama.post(
                        "", {"query": ollama_query or query, "max_results": 6},
                        "https://ollama.com/api/web_search",
                    )
                    metadata = {
                        "provider": "ollama/web_search",
                        **ollama.last_call,
                    }
                else:
                    result = tavily.search(query, max_results=6, include_domains=include_domains)
                    metadata = dict(tavily.last_call)
                self._success(provider, metadata)
                return result
            except (RateLimitError, ProviderError) as error:
                self._failure(provider, error)
                errors.append((provider, error))
        available_cooldowns = [
            until - time.monotonic() for until in self.cooldown_until.values()
            if until > time.monotonic()
        ]
        retry_after = max(1, int(min(available_cooldowns))) if available_cooldowns else None
        if errors and all(isinstance(error, RateLimitError) for _, error in errors):
            raise RateLimitError("Все поисковые провайдеры временно ограничены", retry_after)
        if not errors and available_cooldowns:
            raise RateLimitError("Все доступные поисковые провайдеры на cooldown", retry_after)
        details = ", ".join(provider + ":" + str(error) for provider, error in errors)
        if not details:
            details = "нет доступных поисковых провайдеров"
        raise ProviderError("Не удалось выполнить поиск: " + details)


def matches(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def classify(url, country, policy):
    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or not host:
        return "rejected", 0
    if matches(host, policy["deny"]):
        return "denied", 0
    for name, domains, score in (
        ("official", policy["official"].get(country, []), 3),
        ("dataset", policy["datasets"], 2),
        ("media", policy["media"], 1),
        ("community", policy["community"], 1),
    ):
        if matches(host, domains):
            return name, score
    return "unreviewed", 0


class Retriever:
    def __init__(self, client=None):
        self.client = client or Client()
        self.search = BalancedSearch(
            ollama=self.client,
            enable_tavily=client is None,
        )
        self.policy = read_json(ROOT / "source_policy.json")

    def pack(self, target, layer, refresh=False, require_provider_metadata=False):
        signature = fingerprint({"target": target, "layer": layer, "policy": self.policy, "retriever_version": 1})
        path = ROOT / "data/evidence" / (signature + ".json")
        cached = read_json(path)
        ttl = int(settings().get("RELOCATION_EVIDENCE_TTL_HOURS", "24")) * 3600
        pin_cached = settings().get("RELOCATION_PIN_EVIDENCE", "").strip().lower() in ("1", "true", "yes")
        has_provider_metadata = cached.get("retrieval_version") == 2 and isinstance(cached.get("retrieval_distribution"), dict)
        search_provider = settings().get("RELOCATION_SEARCH_PROVIDER", "balanced").strip().lower()
        uses_only_tavily = search_provider == "tavily" and cached.get("retrieval_distribution", {}).get("ollama", 1) == 0
        if cached and not refresh and (not require_provider_metadata or has_provider_metadata) and (search_provider != "tavily" or uses_only_tavily):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["retrieved_at"])).total_seconds()
            if pin_cached or 0 <= age < ttl:
                return cached
        sources = {}
        rejected = []
        queries = []
        location = target["country"] + (" " + target["city"] if layer == "city" else "")
        actual_counts = {"ollama": 0, "tavily": 0}
        allowed_domains = list(dict.fromkeys(
            self.policy["official"].get(target["country"], [])
            + self.policy["datasets"] + self.policy["media"] + self.policy["community"]
        ))
        for query_index, criterion_id in enumerate(criterion_ids(layer)):
            print(f"  Поиск: {CRITERIA[criterion_id][0]}", flush=True)
            topic = CRITERIA[criterion_id][2]
            query = location + " " + topic
            ollama_query = query
            if layer == "country":
                domains = self.policy["official"].get(target["country"], [])
                if domains:
                    ollama_query += " (" + " OR ".join("site:" + domain for domain in domains[:8]) + ")"
            search_provider = settings().get("RELOCATION_SEARCH_PROVIDER", "balanced").strip().lower()
            if search_provider in ("ollama", "tavily"):
                preferred = search_provider
            else:
                preferred = "ollama" if query_index % 2 == 0 else "tavily"
            result = self.search.search(query, preferred, allowed_domains, ollama_query)
            actual_provider = self.search.last_call.get("provider", "")
            actual_counts["tavily" if actual_provider.startswith("tavily/") else "ollama"] += 1
            queries.append({
                "criterion_id": criterion_id,
                "query": query,
                "assigned_provider": preferred,
                **self.search.last_call,
            })
            for item in result.get("results", []):
                url = item.get("url") or ""
                source_type, score = classify(url, target["country"], self.policy)
                if not score or not isinstance(item.get("content"), str) or not item["content"].strip():
                    rejected.append({"url": url, "reason": source_type if not score else "empty_content", "criterion_id": criterion_id})
                    continue
                if url not in sources:
                    sources[url] = {
                        "id": len(sources) + 1, "url": url, "title": item.get("title", ""),
                        "content": item["content"][:5000], "content_kind": "search_excerpt",
                        "source_type": source_type, "source_score": score,
                        "criterion_ids": [], "retrieved_at": now(),
                        "published_at": item.get("date"), "effective_at": None,
                    }
                if criterion_id not in sources[url]["criterion_ids"]:
                    sources[url]["criterion_ids"].append(criterion_id)
            time.sleep(float(settings().get("RELOCATION_SEARCH_PAUSE_SECONDS", "2")))
        citations = list(sources.values())
        used_providers = [name for name, count in actual_counts.items() if count]
        pack = {
            "target": target, "layer": layer, "retrieved_at": now(),
            "retrieval_version": 2,
            "retrieval_provider": "+".join(used_providers),
            "retrieval_distribution": actual_counts,
            "independent_retrieval": False,
            "policy_hash": fingerprint(self.policy), "queries": queries,
            "citations": citations, "rejected_sources": rejected,
            "evidence_hash": fingerprint(citations),
        }
        write_json(path, pack)
        return pack
