import json
import logging
from collections.abc import Iterator

import httpx

from app.config import get_settings

log = logging.getLogger("homepilot.ollama")


class OllamaUnavailable(Exception):
    pass


def chat_completion(messages: list[dict], tools: list[dict]) -> dict:
    """One non-streaming call to Ollama /api/chat. Returns the assistant `message` dict."""
    s = get_settings()
    payload: dict = {"model": s.ollama_model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    try:
        r = httpx.post(f"{s.ollama_base_url.rstrip('/')}/api/chat", json=payload, timeout=s.ollama_timeout_seconds)
        r.raise_for_status()
        return r.json()["message"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("Ollama request failed: %s", exc.__class__.__name__)
        raise OllamaUnavailable() from exc


def chat_completion_stream(messages: list[dict]) -> Iterator[str]:
    """Streaming call to Ollama /api/chat (no tools — final answer only).

    Yields decoded token strings as they arrive.
    Raises OllamaUnavailable on connection or HTTP errors.
    """
    s = get_settings()
    payload: dict = {"model": s.ollama_model, "messages": messages, "stream": True}
    try:
        with httpx.stream(
            "POST",
            f"{s.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=s.ollama_timeout_seconds,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                token = (chunk.get("message") or {}).get("content") or ""
                if token:
                    yield token
                if chunk.get("done"):
                    break
    except httpx.HTTPError as exc:
        log.warning("Ollama stream failed: %s", exc.__class__.__name__)
        raise OllamaUnavailable() from exc
