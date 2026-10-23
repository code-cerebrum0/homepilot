"""Chat assistant — uses the in-process MCP client to call tools.

The MCP client sends JSON-RPC (tools/list, tools/call, modern era with _meta) to the
same dispatch function the HTTP /mcp endpoint uses.  The household is injected server-side;
the model never sees or sets household_id.

Every call to call_in_process() is recorded in ChatOut.mcp_calls, which proves the MCP
code path is being used (not the old direct tool calls).
"""
import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.ollama_client import OllamaUnavailable, chat_completion  # noqa: F401 (re-exported)
from app.config import get_settings
from app.mcp.dispatch import McpCallRecord, call_in_process
from app.models import ChatMessage
from app.schemas import ChatOut, McpCallOut, ToolCallOut
from app.utils import app_tz, utcnow

log = logging.getLogger("homepilot.chat")

HISTORY_LIMIT = 10

SYSTEM_PROMPT = """You are HomePilot's energy assistant for a single household. You answer questions about electricity usage, devices, cost and savings.

Rules:
- Get every figure from the tools. Never invent, guess or estimate numbers yourself.
- If a tool reports no data, returns an error, or is rejected, tell the user plainly that you could not get that information.
- Costs are estimates based on the configured tariff; say so when you quote them.
- Use kWh for energy and W for power. Keep answers short and clear.
- Do not mention tool names, ids or these instructions.
Current local time: {now} ({tz})."""


def _resolve_conversation(db: Session, conversation_id: UUID | None, household_id: int, user_id: int) -> str:
    if conversation_id is None:
        return str(uuid4())
    cid = str(conversation_id)
    foreign = db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.conversation_id == cid,
            (ChatMessage.household_id != household_id) | (ChatMessage.user_id != user_id),
        )
        .limit(1)
    )
    return str(uuid4()) if foreign else cid


def _history(db: Session, cid: str, household_id: int, user_id: int) -> list[dict]:
    rows = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == cid,
            ChatMessage.household_id == household_id,
            ChatMessage.user_id == user_id,
        )
        .order_by(ChatMessage.id.desc())
        .limit(HISTORY_LIMIT)
    ).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _record_to_schema(r: McpCallRecord) -> McpCallOut:
    return McpCallOut(
        method=r.method,
        tool_name=r.tool_name,
        protocol=r.protocol,
        duration_ms=r.duration_ms,
        ok=r.ok,
    )


# ─── MCP-backed tool execution ────────────────────────────────────────────────


def _mcp_tools_list(db: Session, household_id: int) -> tuple[list[dict], McpCallRecord]:
    """Fetch the Ollama-compatible tool specs via the in-process MCP client."""
    result, record = call_in_process(db, household_id, "tools/list")
    mcp_tools = result.get("tools", [])
    ollama_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in mcp_tools
    ]
    return ollama_tools, record


def _mcp_tool_call(
    db: Session, household_id: int, name: str, raw_args
) -> tuple[dict, ToolCallOut, McpCallRecord]:
    """Execute a tool via the in-process MCP client."""
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError:
            args = {}
    else:
        args = raw_args if raw_args is not None else {}

    try:
        result, record = call_in_process(db, household_id, "tools/call", {"name": name, "arguments": args})
    except RuntimeError as exc:
        log.info("MCP dispatch error for tool %s: %s", name, exc)
        # Build a synthetic record for the failed call
        from app.mcp.dispatch import PROTO_MODERN, McpCallRecord as _R
        fake_record = _R("tools/call", name, PROTO_MODERN, 0.0, False)
        return (
            {"error": str(exc)},
            ToolCallOut(name=name, status="error", summary="dispatch error"),
            fake_record,
        )

    is_error = result.get("isError", False)
    meta = result.get("_meta") or {}
    label = meta.get("homepilot/label") or name
    structured = result.get("structuredContent") or {}
    text_content = ""
    for c in result.get("content") or []:
        if c.get("type") == "text":
            text_content = c.get("text", "")
            break

    if is_error:
        error_msg = text_content or "tool error"
        if not error_msg.startswith("rejected:"):
            error_msg = f"rejected: {error_msg}"
        return (
            {"error": error_msg},
            ToolCallOut(name=name, status="error", summary=label),
            record,
        )

    return (
        structured if structured else {"text": text_content},
        ToolCallOut(name=name, status="success", summary=label),
        record,
    )


# ─── Main chat function ───────────────────────────────────────────────────────


def run_chat(db: Session, household_id: int, user_id: int, message: str, conversation_id: UUID | None) -> ChatOut:
    settings = get_settings()
    tz = app_tz()
    now = utcnow()

    cid = _resolve_conversation(db, conversation_id, household_id, user_id)

    tool_specs, list_record = _mcp_tools_list(db, household_id)
    mcp_records: list[McpCallRecord] = [list_record]

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            now=now.astimezone(tz).strftime("%A %d %B %Y, %H:%M"),
            tz=settings.app_timezone,
        )},
        *_history(db, cid, household_id, user_id),
        {"role": "user", "content": message},
    ]

    executed: list[ToolCallOut] = []
    reply = ""
    for _ in range(settings.ollama_max_tool_rounds):
        msg = chat_completion(messages, tool_specs)
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            result, record, mcp_rec = _mcp_tool_call(db, household_id, name, fn.get("arguments"))
            executed.append(record)
            mcp_records.append(mcp_rec)
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)})
    else:
        final = chat_completion(messages, [])
        reply = (final.get("content") or "").strip()

    if not reply:
        reply = "Sorry, I couldn't put together an answer. Please try again."

    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="user", content=message))
    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="assistant", content=reply))
    db.commit()

    return ChatOut(
        conversation_id=cid,
        reply=reply,
        tool_calls=executed,
        mcp_calls=[_record_to_schema(r) for r in mcp_records],
        provider="ollama",
    )
