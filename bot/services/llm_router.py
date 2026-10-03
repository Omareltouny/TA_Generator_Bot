"""Provider router with fallback, backoff and rate-limit tracking.

Providers are thin async callables (prompt -> text) so tests can stub them.
Real HTTP providers (Groq/OpenRouter are OpenAI-compatible; Gemini has its own shape)
are at the bottom.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx


class RetryableError(Exception):
    """429/5xx/timeout: try next model/provider. retry_after seconds if known."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class FatalError(Exception):
    """Non-retryable for this provider (bad key, bad request)."""


class AllProvidersBusy(Exception):
    """Every provider is rate-limited/failing. Callers should requeue with backoff."""

    def __init__(self, msg: str, retry_after: float = 60):
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str


class Provider(Protocol):
    name: str
    models: list[str]

    async def complete(self, model: str, system: str, prompt: str, json_mode: bool, max_tokens: int = 4096) -> str: ...


class LLMRouter:
    def __init__(self, providers: list[Provider], *, retries_per_model: int = 2,
                 base_backoff: float = 1.0, cooldown_s: float = 60.0,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, clock=time.monotonic):
        self.providers = providers
        self.retries, self.base_backoff, self.cooldown = retries_per_model, base_backoff, cooldown_s
        self._sleep, self._clock = sleep, clock
        self._cooldown_until: dict[tuple[str, str], float] = {}

    def _available(self, p: Provider, m: str) -> bool:
        return self._cooldown_until.get((p.name, m), 0) <= self._clock()

    async def complete(self, prompt: str, *, system: str = "", json_mode: bool = False, max_tokens: int = 4096) -> LLMResult:
        soonest: float | None = None
        for p in self.providers:
            for m in p.models:
                if not self._available(p, m):
                    wait = self._cooldown_until[(p.name, m)] - self._clock()
                    soonest = wait if soonest is None else min(soonest, wait)
                    continue
                for attempt in range(self.retries):
                    try:
                        text = await p.complete(m, system, prompt, json_mode, max_tokens)
                        return LLMResult(text, p.name, m)
                    except FatalError:
                        break  # skip this model entirely
                    except RetryableError as e:
                        if e.retry_after is not None:
                            # Provider told us how long: cool this model down and move on.
                            self._cooldown_until[(p.name, m)] = self._clock() + e.retry_after
                            soonest = e.retry_after if soonest is None else min(soonest, e.retry_after)
                            break
                        if attempt + 1 < self.retries:
                            await self._sleep(self.base_backoff * 2 ** attempt)
                        else:
                            self._cooldown_until[(p.name, m)] = self._clock() + self.cooldown
        raise AllProvidersBusy("all LLM providers are rate-limited or failing", soonest or self.cooldown)

    async def complete_json(self, prompt: str, *, system: str = "", validate: Callable[[dict], None] | None = None,
                            max_attempts: int = 3, max_tokens: int = 4096) -> tuple[dict, LLMResult]:
        """Ask for JSON, parse (tolerating code fences), validate; re-prompt with the error on failure."""
        last_err = ""
        for _ in range(max_attempts):
            p = prompt if not last_err else f"{prompt}\n\nYour previous reply was invalid ({last_err}). Return ONLY valid JSON."
            res = await self.complete(p, system=system, json_mode=True, max_tokens=max_tokens)
            try:
                data = extract_json(res.text)
                if validate:
                    validate(data)
                return data, res
            except (ValueError, KeyError, TypeError) as e:
                last_err = str(e)[:300]
        raise ValueError(f"LLM did not return valid JSON after {max_attempts} attempts: {last_err}")


def extract_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start:end + 1])
        raise ValueError("no JSON object found")


# ---- real HTTP providers ---------------------------------------------------

def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code == 429 or resp.status_code >= 500:
        ra = resp.headers.get("retry-after")
        raise RetryableError(f"HTTP {resp.status_code}", float(ra) if ra and ra.replace(".", "").isdigit() else None)
    if resp.status_code >= 400:
        raise FatalError(f"HTTP {resp.status_code}: {resp.text[:200]}")


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, api_key: str, models: list[str], timeout: float = 120):
        self.name, self.base_url, self.api_key, self.models, self.timeout = name, base_url, api_key, models, timeout

    async def _post(self, body: dict) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                return await c.post(f"{self.base_url}/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {self.api_key}"})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(str(e))

    async def complete(self, model, system, prompt, json_mode, max_tokens=4096):
        body = {"model": model, "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self._post(body)
        if r.status_code == 400 and json_mode:  # some (free) models reject response_format: retry without it
            body.pop("response_format")
            r = await self._post(body)
        _raise_for(r)
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RetryableError("output truncated (hit max_tokens)")
        text = (choice.get("message") or {}).get("content")
        if not text:
            raise RetryableError("empty response")
        return text


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, models: list[str], timeout: float = 120):
        self.api_key, self.models, self.timeout = api_key, models, timeout

    async def complete(self, model, system, prompt, json_mode, max_tokens=4096):
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["generationConfig"] = {"maxOutputTokens": max_tokens, **({"responseMimeType": "application/json"} if json_mode else {})}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(str(e))
        _raise_for(r)
        try:
            cand = r.json()["candidates"][0]
            if cand.get("finishReason") == "MAX_TOKENS":
                raise RetryableError("output truncated (hit max tokens)")
            return "".join(p.get("text", "") for p in cand["content"]["parts"])
        except (KeyError, IndexError):
            raise RetryableError("empty Gemini response")


def build_router(cfg) -> LLMRouter:
    makers = {
        "groq": lambda: OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", cfg.api_keys["groq"], cfg.models["groq"]),
        "openrouter": lambda: OpenAICompatProvider("openrouter", "https://openrouter.ai/api/v1", cfg.api_keys["openrouter"], cfg.models["openrouter"]),
        "gemini": lambda: GeminiProvider(cfg.api_keys["gemini"], cfg.models["gemini"]),
    }
    return LLMRouter([makers[n]() for n in cfg.provider_order if n in makers and cfg.api_keys.get(n)])
