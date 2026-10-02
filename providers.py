import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

from core import ROOT, now, write_json


@dataclass
class Account:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model_aliases: dict = field(default_factory=dict)


def settings():
    path = Path(os.getenv("RELOCATION_PROVIDERS_FILE", "~/.job-hunter/llm-providers.env")).expanduser()
    return {**dotenv_values(path), **dotenv_values(ROOT / ".env"), **os.environ}


def account_names(prefix, maximum=9):
    return tuple(prefix if index == 1 else f"{prefix}{index}" for index in range(1, maximum + 1))


def accounts(judge=False):
    values = settings()
    names = account_names("DEEPSEEK" if judge else "OLLAMA")
    result = []
    seen = set()
    for name in names:
        base = values.get(name + "_BASE_URL") or values.get(names[0] + "_BASE_URL")
        base = base or ("https://api.deepseek.com/v1" if judge else "https://ollama.com/v1")
        key = values.get(name + "_API_KEY", "")
        if key and (base, key) not in seen:
            result.append(Account(name.lower(), base.rstrip("/"), key))
            seen.add((base, key))
    return result


def openrouter_accounts(model):
    """Free-only OpenRouter fallback for scout generation, never search or judge."""
    values = settings()
    free_model = values.get(
        "RELOCATION_OPENROUTER_FREE_MODEL",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ).strip()
    if not free_model.endswith(":free"):
        raise ValueError("RELOCATION_OPENROUTER_FREE_MODEL должен быть бесплатным вариантом с суффиксом :free")
    base = (values.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    result = []
    seen = set()
    for name in ("OPENROUTER_API_KEY",) + tuple(f"OPENROUTER_API_KEY_{i}" for i in range(2, 10)):
        key = values.get(name, "")
        if not key or (base, key) in seen:
            continue
        account_name = name.replace("_API_KEY", "").lower().rstrip("_")
        account = Account(account_name, base, key)
        account.model_aliases[model] = free_model
        result.append(account)
        seen.add((base, key))
    return result


def models():
    third = "gemini-3.5-flash" if settings().get("GEMINI_API_KEY") else "gemma4:31b"
    return [value.strip() for value in settings().get("RELOCATION_MODELS", "gpt-oss:120b,nemotron-3-super," + third).split(",") if value.strip()]


def scout_accounts(model):
    if not model.startswith("gemini-"):
        return accounts() + openrouter_accounts(model)
    values = settings()
    result = []
    for name in account_names("GEMINI"):
        if values.get(name + "_API_KEY"):
            result.append(Account(name.lower(), values.get(name + "_BASE_URL") or values.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"), values[name + "_API_KEY"]))
    for account in accounts():
        account.model_aliases[model] = values.get("RELOCATION_GEMINI_FALLBACK_MODEL", "gemma4:31b")
        result.append(account)
    result.extend(openrouter_accounts(model))
    return result


def judge_model():
    return settings().get("RELOCATION_JUDGE_MODEL", "deepseek-v4-pro")


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(response):
    """Return a server-requested cooldown without exposing response content."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(1, math.ceil(float(value)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(1, math.ceil((parsed - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    for header in ("X-RateLimit-Reset", "RateLimit-Reset", "X-RateLimit-Reset-Requests"):
        value = response.headers.get(header)
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if number > time.time() - 60:
            number -= time.time()
        return max(1, math.ceil(number))
    return None


class Client:
    def __init__(self, configured=None):
        self.accounts = accounts() if configured is None else configured
        self.session = requests.Session()
        self.session.trust_env = False
        self.active = 0
        self.last_call = {}

    def post(self, endpoint, payload, fixed_url=None):
        if not self.accounts:
            raise ProviderError("Не найдены ключи в RELOCATION_PROVIDERS_FILE")
        attempted = []
        retry_delays = []
        start = self.active
        if self.accounts[start].base_url.startswith("https://openrouter.ai/"):
            # OpenRouter is a reserve: re-check primary providers on each new request.
            start = 0
        for offset in range(len(self.accounts)):
            index = (start + offset) % len(self.accounts)
            account = self.accounts[index]
            account_payload = dict(payload)
            if "model" in account_payload:
                account_payload["model"] = account.model_aliases.get(account_payload["model"], account_payload["model"])
            if account.base_url.startswith("https://openrouter.ai/"):
                # Leave token budget for the report rather than hidden free-model reasoning.
                account_payload["reasoning"] = {"effort": "none"}
            attempts = int(settings().get("RELOCATION_HTTP_ATTEMPTS", "2"))
            for attempt in range(attempts):
                try:
                    response = self.session.post(
                        fixed_url or account.base_url + endpoint,
                        json=account_payload,
                        headers={"Authorization": "Bearer " + account.api_key},
                        timeout=(10, 240),
                    )
                except requests.RequestException:
                    if attempt + 1 < attempts:
                        time.sleep(2 ** attempt)
                        continue
                    attempted.append(account.name + ":network")
                    break
                if response.status_code in (401, 402, 403, 404, 410, 429):
                    attempted.append(account.name + ":" + str(response.status_code))
                    if response.status_code == 429:
                        delay = retry_after_seconds(response)
                        if delay is not None:
                            retry_delays.append(delay)
                    break
                if response.status_code >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(2 ** attempt)
                        continue
                    attempted.append(account.name + ":" + str(response.status_code))
                    break
                if not response.ok:
                    raise ProviderError(f"{account.name}: HTTP {response.status_code}; запрос отклонён")
                try:
                    result = response.json()
                except ValueError:
                    attempted.append(account.name + ":invalid_response")
                    break
                self.active = index
                self.last_call = {"account": account.name, "base_url": account.base_url, "retrieved_at": now(), "served_model": account_payload.get("model")}
                return result
        if attempted and all(item.endswith(":429") for item in attempted):
            # If even one account omits reset metadata, it may recover sooner;
            # let the runner poll at the configured short interval.
            retry_after = min(retry_delays) if len(retry_delays) == len(attempted) else None
            suffix = f"; cooldown не менее {retry_after} сек." if retry_after is not None else "; сервер не сообщил время сброса"
            raise RateLimitError("Все аккаунты временно ограничены: " + ", ".join(attempted) + suffix, retry_after)
        raise ProviderError("Все аккаунты исчерпаны: " + ", ".join(attempted))

    def json_report(self, model, instructions, payload, validator, log_path, thinking=False):
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error = None
        for attempt in range(3):
            request = {"model": model, "messages": messages, "max_tokens": 12000, "response_format": {"type": "json_object"}}
            if thinking:
                request.update({"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
            else:
                request["temperature"] = 0
            response = self.post("/chat/completions", request)
            choice = (response.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content") or ""
            metadata = {
                **self.last_call,
                "requested_model": model,
                "actual_model": response.get("model") or self.last_call.get("served_model") or model,
                "usage": response.get("usage"),
                "thinking": thinking,
            }
            write_json(str(log_path) + f".attempt-{attempt + 1}.json", {
                "request": request, "provenance": metadata, "content": content,
                "finish_reason": choice.get("finish_reason"),
            })
            try:
                if choice.get("finish_reason") == "length":
                    raise ValueError("Ответ обрезан лимитом токенов")
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
                parsed = json.loads(cleaned)
                report = validator(parsed)
                report["provenance"] = metadata
                return report
            except (ValueError, TypeError, KeyError) as error:
                last_error = str(error)
                messages.append({"role": "user", "content": "Исправь JSON по исходным данным. Ошибка проверки: " + last_error})
        raise ValueError("Ответ не прошёл проверку после 3 попыток: " + str(last_error))
