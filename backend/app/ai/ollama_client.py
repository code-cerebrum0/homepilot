import json
import logging
import re
from collections.abc import Iterator

import httpx

from app.config import get_settings

log = logging.getLogger("homepilot.ollama")

# Matches <think>…</think> blocks emitted by reasoning models (e.g. deepseek-r1).
# These are internal chain-of-thought and must not appear in user-facing replies.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaUnavailable(Exception):
    pass


def _strip_think(text: str) -> str:
    """Remove <think>…</think> reasoning blocks from model output."""
    return _THINK_RE.sub("", text).strip()


def chat_completion(messages: list[dict], tools: list[dict]) -> dict:
    """One non-streaming call to Ollama /api/chat. Returns the assistant `message` dict.

    If the model does not support the native tools API (e.g. deepseek-r1), Ollama
    returns HTTP 400. In that case the call is automatically retried without tools so
    that the text-form tool-call extraction fallback in chat.py can handle it.

    <think>…</think> blocks emitted by reasoning models are stripped from the
    returned content so they never appear in user-facing replies.
    """
    s = get_settings()
    url = f"{s.ollama_base_url.rstrip('/')}/api/chat"

    def _call(include_tools: bool) -> dict:
        payload: dict = {"model": s.ollama_model, "messages": messages, "stream": False}
        if include_tools and tools:
            payload["tools"] = tools
        r = httpx.post(url, json=payload, timeout=s.ollama_timeout_seconds)
        r.raise_for_status()
        return r.json()["message"]

    try:
        msg = _call(include_tools=True)
    except httpx.HTTPStatusError as exc:
        # 400 "does not support tools" — retry without tools so text-form extraction
        # can pick up any JSON tool-call block the model emits in plain text.
        if exc.response.status_code == 400 and tools:
            log.info(
                "Model %s does not support native tools API (HTTP 400); "
                "retrying without tools (text-form extraction will be used).",
                s.ollama_model,
            )
            try:
                msg = _call(include_tools=False)
            except (httpx.HTTPError, KeyError, ValueError) as inner_exc:
                log.warning("Ollama retry without tools failed: %s", inner_exc.__class__.__name__)
                raise OllamaUnavailable() from inner_exc
        else:
            log.warning("Ollama request failed (HTTP %s): %s", exc.response.status_code, exc)
            raise OllamaUnavailable() from exc
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("Ollama request failed: %s", exc.__class__.__name__)
        raise OllamaUnavailable() from exc

    # Strip reasoning blocks before returning
    if "content" in msg and msg["content"]:
        msg = {**msg, "content": _strip_think(msg["content"])}
    return msg


def chat_completion_stream(messages: list[dict]) -> Iterator[str]:
    """Streaming call to Ollama /api/chat (no tools — final answer only).

    Yields decoded token strings as they arrive.
    <think>…</think> tokens are suppressed so reasoning traces never reach the user.
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
            in_think = False
            buffer = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                token = (chunk.get("message") or {}).get("content") or ""
                if token:
                    buffer += token
                    # Suppress <think> blocks: buffer until </think> seen
                    while True:
                        if in_think:
                            end = buffer.find("</think>")
                            if end == -1:
                                break  # still inside think block — discard and wait
                            buffer = buffer[end + len("</think>"):]
                            in_think = False
                        else:
                            start = buffer.find("<think>")
                            if start == -1:
                                # No think block — yield everything in buffer
                                yield buffer
                                buffer = ""
                                break
                            # Yield content before the think block
                            if start > 0:
                                yield buffer[:start]
                            buffer = buffer[start + len("<think>"):]
                            in_think = True
                if chunk.get("done"):
                    # Yield any remaining buffered content (outside think)
                    if buffer and not in_think:
                        yield buffer
                    break
    except httpx.HTTPError as exc:
        log.warning("Ollama stream failed: %s", exc.__class__.__name__)
        raise OllamaUnavailable() from exc
