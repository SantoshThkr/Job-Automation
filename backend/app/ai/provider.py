"""Local LLM provider (Ollama). Everything degrades gracefully when Ollama is not running."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class AIUnavailable(Exception):
    """Ollama is disabled, unreachable, or the model is missing."""


class AIResponseError(Exception):
    """The model answered, but the answer could not be parsed/validated."""


def extract_json(text: str) -> Any:
    """Parse JSON from a model response, tolerating code fences, <think> blocks and chatter."""
    if text is None:
        raise AIResponseError("empty response")
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # find the first balanced {...} block
    start = cleaned.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # common small-model glitch: trailing commas
                        try:
                            return json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
                        except json.JSONDecodeError:
                            break
        start = cleaned.find("{", start + 1)
    raise AIResponseError(f"no JSON object found in response: {text[:200]!r}")


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float | None = None,
                 enabled: bool | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        s = get_settings()
        self.base_url = (base_url or s.ollama_url).rstrip("/")
        self.model = model or s.ollama_model
        self.timeout = timeout or s.ollama_timeout_s
        self.enabled = s.ai_enabled if enabled is None else enabled
        self._transport = transport
        self._down_until = 0.0  # circuit breaker after connection failures
        self._health_cache: tuple[float, dict[str, Any]] | None = None
        self._supports_think_flag = True

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout, transport=self._transport)

    async def health(self, use_cache: bool = True) -> dict[str, Any]:
        now = time.monotonic()
        if use_cache and self._health_cache and now - self._health_cache[0] < 30:
            return self._health_cache[1]
        result: dict[str, Any] = {"enabled": self.enabled, "available": False, "model": self.model,
                                  "model_present": False, "models": [], "error": ""}
        if not self.enabled:
            result["error"] = "AI disabled (PJA_AI_ENABLED=false)"
        else:
            try:
                async with self._client(timeout=5) as c:
                    r = await c.get("/api/tags")
                    r.raise_for_status()
                    models = [m.get("name", "") for m in r.json().get("models", [])]
                result["models"] = models
                result["available"] = True
                result["model_present"] = any(m == self.model or m.split(":")[0] == self.model for m in models)
                if not result["model_present"]:
                    result["error"] = f"Model '{self.model}' not pulled. Run: ollama pull {self.model}"
            except Exception as exc:  # connection refused, timeout ...
                result["error"] = f"Ollama not reachable at {self.base_url}: {exc.__class__.__name__}"
        self._health_cache = (now, result)
        return result

    async def is_available(self) -> bool:
        if not self.enabled or time.monotonic() < self._down_until:
            return False
        h = await self.health()
        return bool(h["available"] and h["model_present"])

    async def chat(self, system: str, prompt: str, *, json_schema: dict | None = None, json_mode: bool = False,
                   temperature: float = 0.1, max_tokens: int = 700) -> str:
        if not self.enabled:
            raise AIUnavailable("AI disabled")
        if time.monotonic() < self._down_until:
            raise AIUnavailable("Ollama recently unreachable (circuit open)")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_schema is not None:
            body["format"] = json_schema
        elif json_mode:
            body["format"] = "json"
        if self._supports_think_flag:
            body["think"] = False
        try:
            async with self._client() as c:
                r = await c.post("/api/chat", json=body)
                if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
                    self._supports_think_flag = False
                    body.pop("think")
                    r = await c.post("/api/chat", json=body)
                if r.status_code == 404:
                    raise AIUnavailable(f"model '{self.model}' not found: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            self._down_until = time.monotonic() + 60
            raise AIUnavailable(f"Ollama request failed: {exc.__class__.__name__}") from exc
        except httpx.HTTPStatusError as exc:
            raise AIUnavailable(f"Ollama HTTP {exc.response.status_code}") from exc
        content = (data.get("message") or {}).get("content")
        if not content:
            raise AIResponseError("empty message content")
        return content

    async def chat_json(self, system: str, prompt: str, *, json_schema: dict | None = None,
                        temperature: float = 0.1, max_tokens: int = 700, retries: int = 1) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            content = await self.chat(system, prompt, json_schema=json_schema, json_mode=json_schema is None,
                                      temperature=temperature if attempt == 0 else 0.0, max_tokens=max_tokens)
            try:
                return extract_json(content)
            except AIResponseError as exc:
                last = exc
                log.warning("Malformed AI JSON (attempt %d): %s", attempt + 1, exc)
        raise AIResponseError(str(last))


_provider: OllamaProvider | None = None


def get_provider() -> OllamaProvider:
    global _provider
    if _provider is None:
        _provider = OllamaProvider()
    return _provider


def set_provider(provider: OllamaProvider | None) -> None:
    global _provider
    _provider = provider
