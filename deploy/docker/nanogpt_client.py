"""Server-side NanoGPT chat calls.

The key and base URL come from the process environment (the same
OPENAI_API_KEY / OPENAI_BASE_URL pair already pointed at NanoGPT).
A request cannot supply either value.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import httpx


request_usage: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("nanogpt_request_usage", default=None)


class NanoGPTError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def nanogpt_settings() -> Dict[str, str]:
    """Resolve the key, base URL, and model from the server environment."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base = os.environ.get("OPENAI_BASE_URL", "https://nano-gpt.com/api/v1").strip().rstrip("/")
    provider = os.environ.get("LLM_PROVIDER", "deepseek/deepseek-v4-flash").strip()
    model = provider[7:] if provider.startswith("openai/") else provider
    if not api_key:
        raise NanoGPTError(500, "NanoGPT API key is not configured")
    if not model:
        raise NanoGPTError(500, "NanoGPT model is not configured")
    return {"api_key": api_key, "base_url": base, "model": model}


async def complete(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0,
    json_mode: bool = False,
    timeout: float = 60,
    model: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """One non-streaming chat completion. Returns the assistant text."""
    settings = nanogpt_settings()
    chosen = (model or settings["model"]).strip()
    if chosen.startswith("openai/"):
        chosen = chosen[7:]
    payload: Dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    url = settings["base_url"] + "/chat/completions"

    async def _post(http: httpx.AsyncClient) -> str:
        response = await http.post(url, json=payload, headers=headers)
        if response.status_code == 401:
            raise NanoGPTError(401, "NanoGPT rejected the API key")
        if response.status_code >= 400:
            raise NanoGPTError(response.status_code, "NanoGPT request failed")
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise NanoGPTError(502, "NanoGPT returned an unexpected body") from exc
        try:
            calls = request_usage.get()
            if calls is not None:
                usage = body.get("usage") or {}
                calls.append({
                    "model": body.get("model") or chosen,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "cost": body.get("cost", usage.get("cost")),
                })
        except Exception:
            pass
        return content

    if client is not None:
        return await _post(client)
    async with httpx.AsyncClient(timeout=timeout) as http:
        return await _post(http)
