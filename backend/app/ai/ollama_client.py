import logging

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
