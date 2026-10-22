import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.ollama_client import OllamaUnavailable, chat_completion  # noqa: F401 (re-exported)
from app.ai.tools import TOOL_SPECS, TOOLS, ToolContext, ToolRejected, validate_args
from app.config import get_settings
from app.models import ChatMessage
from app.schemas import ChatOut, ToolCallOut
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


def _parse_args(raw) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ToolRejected("arguments are not valid JSON")
    return raw if raw is not None else {}


def _run_tool(ctx: ToolContext, name: str, raw_args) -> tuple[dict, ToolCallOut]:
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool '{name}'"}, ToolCallOut(name=name, status="error", summary="unknown tool")
    try:
        args = validate_args(tool.parameters, _parse_args(raw_args))
    except ToolRejected as exc:
        log.info("Tool %s rejected: %s", name, exc)
        return {"error": f"rejected: {exc}"}, ToolCallOut(name=name, status="error", summary=f"rejected: {exc}")
    try:
        return tool.handler(ctx, args), ToolCallOut(name=name, status="success", summary=tool.summarize(args))
    except Exception:
        log.exception("Tool %s failed", name)
        ctx.db.rollback()
        return {"error": "the tool failed to run"}, ToolCallOut(name=name, status="error", summary="tool failed")


def run_chat(db: Session, household_id: int, user_id: int, message: str, conversation_id: UUID | None) -> ChatOut:
    settings = get_settings()
    tz = app_tz()
    now = utcnow()
    ctx = ToolContext(db=db, household_id=household_id, tz=tz, now=now)

    cid = _resolve_conversation(db, conversation_id, household_id, user_id)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(now=now.astimezone(tz).strftime("%A %d %B %Y, %H:%M"), tz=settings.app_timezone)},
        *_history(db, cid, household_id, user_id),
        {"role": "user", "content": message},
    ]

    executed: list[ToolCallOut] = []
    reply = ""
    for _ in range(settings.ollama_max_tool_rounds):
        msg = chat_completion(messages, TOOL_SPECS)
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            result, record = _run_tool(ctx, name, fn.get("arguments"))
            executed.append(record)
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)})
    else:
        # Tool budget exhausted: ask for a final answer from what was gathered, without tools.
        final = chat_completion(messages, [])
        reply = (final.get("content") or "").strip()

    if not reply:
        reply = "Sorry, I couldn't put together an answer. Please try again."

    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="user", content=message))
    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="assistant", content=reply))
    db.commit()
    return ChatOut(conversation_id=cid, reply=reply, tool_calls=executed, provider="ollama")
