"""Streaming clients for supported LLM providers."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

PROVIDERS = {
    "openai": {
        "label": "OpenAI / OpenAI-compatible",
        "default_model": "gpt-4o-mini",
        "default_base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "default_model": "claude-3-5-sonnet-latest",
        "default_base_url": "https://api.anthropic.com/v1",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "label": "Google Gemini",
        "default_model": "gemini-1.5-flash",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "env_key": "GEMINI_API_KEY",
    },
    "offline": {
        "label": "Offline (static analysis, no API key)",
        "default_model": "",
        "default_base_url": "",
        "env_key": "",
    },
}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMSettings:
    provider: str = "openai"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 8192

    def resolved(self) -> "LLMSettings":
        spec = PROVIDERS.get(self.provider)
        if spec is None:
            raise LLMError(f"Unknown provider '{self.provider}'")
        env_model = os.getenv("LLM_MODEL") if os.getenv("LLM_PROVIDER", "openai") == self.provider else None
        env_base = os.getenv("LLM_BASE_URL") if self.provider == "openai" else None
        return LLMSettings(
            provider=self.provider,
            model=self.model or env_model or spec["default_model"],
            api_key=self.api_key or (os.getenv(spec["env_key"], "") if spec["env_key"] else ""),
            base_url=(self.base_url or env_base or spec["default_base_url"]).rstrip("/"),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


def server_has_key(provider: str) -> bool:
    env = PROVIDERS.get(provider, {}).get("env_key")
    return bool(env and os.getenv(env))


async def _sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    async for line in resp.aiter_lines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                yield data


async def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode("utf-8", errors="replace")
    message = body
    try:
        err = json.loads(body)
        if isinstance(err, list) and err:
            err = err[0]
        e = err.get("error", err)
        message = e.get("message", body) if isinstance(e, dict) else str(e)
    except (ValueError, AttributeError):
        pass
    raise LLMError(f"Provider returned HTTP {resp.status_code}: {message[:500]}")


async def stream_completion(settings: LLMSettings, system: str, user: str) -> AsyncIterator[str]:
    s = settings.resolved()
    local_openai = s.provider == "openai" and any(h in s.base_url for h in ("localhost", "127.0.0.1"))
    if not s.api_key and not local_openai:
        raise LLMError(
            f"No API key configured for {PROVIDERS[s.provider]['label']}. Add one in Settings, "
            "set it on the server, or switch to Offline mode."
        )
    timeout = httpx.Timeout(300, connect=20)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if s.provider == "openai":
            headers = {"Content-Type": "application/json"}
            if s.api_key:
                headers["Authorization"] = f"Bearer {s.api_key}"
            payload = {
                "model": s.model,
                "stream": True,
                "temperature": s.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }
            async with client.stream("POST", f"{s.base_url}/chat/completions", headers=headers, json=payload) as r:
                await _raise_for_status(r)
                async for data in _sse_lines(r):
                    choices = json.loads(data).get("choices") or [{}]
                    text = (choices[0].get("delta") or {}).get("content")
                    if text:
                        yield text
        elif s.provider == "anthropic":
            headers = {"x-api-key": s.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            payload = {
                "model": s.model,
                "max_tokens": s.max_tokens,
                "temperature": s.temperature,
                "stream": True,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            async with client.stream("POST", f"{s.base_url}/messages", headers=headers, json=payload) as r:
                await _raise_for_status(r)
                async for data in _sse_lines(r):
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text")
                        if text:
                            yield text
                    elif event.get("type") == "error":
                        raise LLMError(event.get("error", {}).get("message", "Anthropic stream error"))
        elif s.provider == "gemini":
            url = f"{s.base_url}/models/{s.model}:streamGenerateContent"
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": s.temperature, "maxOutputTokens": s.max_tokens},
            }
            params = {"alt": "sse", "key": s.api_key}
            async with client.stream("POST", url, params=params, json=payload) as r:
                await _raise_for_status(r)
                async for data in _sse_lines(r):
                    for cand in json.loads(data).get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if part.get("text"):
                                yield part["text"]
        else:
            raise LLMError(f"Provider '{s.provider}' does not support streaming completions")
