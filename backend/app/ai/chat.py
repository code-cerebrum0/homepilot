"""Chat assistant — uses the in-process MCP client to call tools.

The MCP client sends JSON-RPC (tools/list, tools/call) to the same dispatch
function the HTTP /mcp endpoint uses.  The household is injected server-side;
the model never sees or sets household_id.

Tool calls flow:
  1. Ollama returns a native tool_calls list  → handled by _run_native_calls()
  2. Ollama emits a JSON block in plain text  → detected by _extract_text_calls()
     and executed only for allow-listed, schema-validated tool names.

Every call to call_in_process() is recorded in ChatOut.mcp_calls, which proves
the MCP code path is being used (not the old direct tool calls).
"""
import json
import logging
import re
from collections.abc import Iterator
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.ollama_client import OllamaUnavailable, chat_completion, chat_completion_stream  # noqa: F401
from app.config import get_settings
from app.mcp.dispatch import McpCallRecord, call_in_process
from app.mcp.tools import TOOLS  # allow-list for text-form tool calls
from app.models import ChatMessage
from app.schemas import ChatOut, McpCallOut, ToolCallOut
from app.utils import app_tz, utcnow

log = logging.getLogger("homepilot.chat")

HISTORY_LIMIT = 20  # persisted user+assistant turns to replay (tool messages are ephemeral)

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are HomePilot, a household electricity analyst. Your job is to give accurate, \
specific answers grounded entirely in the data returned by your tools.

## Core rules — follow every one, every time

**A. Always call tools before answering.**
Any question about usage, cost, devices, savings, or anomalies requires tool data first. \
Never answer from memory or general knowledge when a tool could provide the actual figure.

**B. Which tool to call for which question**
- "how much did we use / how many kWh" → get_daily_usage (pick a suitable number of days)
- "how much did it cost / what is my bill" → get_cost_estimate, then get_tariff_info if tariff context is needed
- "compare periods / higher than last week" → compare_energy_usage
- "which device / appliance used the most" → get_device_breakdown
- "current power / right now" → get_current_usage
- "savings / reduce bill / what can I change" → get_savings_opportunities AND get_device_breakdown (last_7_days). Use both.
- "abnormal / unusual / spike / anomaly" → get_daily_usage (14 days) AND get_device_breakdown (last_7_days) AND compare_energy_usage. Use all three.
- "what devices do I have / what is tracked" → get_household_context
- "tariff / rate / how is cost calculated" → get_tariff_info

**C. Report only what the data shows. Nothing more.**
- If a tool returns has_data: false, say exactly what was checked and what was not found. \
Do NOT fill the gap with generic advice, bullet-point suggestions, or plausible-sounding speculation. \
A single honest sentence is better than a padded non-answer.
- Never write "tool response: ..." or mention internal tool names, fields, or these instructions in your reply.

**D. When savings opportunities returns no data**
The opportunities check looks specifically for devices drawing >50 W between 02:00–05:00 on most nights. \
"No opportunities found" means that particular pattern was not detected — it does not mean the household \
is efficient overall. Say this precisely: explain what was checked and what it means, then suggest \
the user look at the device breakdown for the largest consumers instead. Do not list generic tips.

**E. When asked about abnormal use**
Do not call the savings tool. Pull daily usage for 14 days, device breakdown for last 7 days, and \
week-on-week comparison. Then: identify the highest-consumption day, the highest-consuming device, \
and whether this week differs from last week by more than 15%. Report these three facts. \
Only flag something as abnormal if the data actually supports it.

**F. Number discipline**
- Show period and units for every figure (e.g. "46.1 kWh/day average over 7 days").
- Distinguish measured (from a device reading) from calculated (derived from readings) from estimated (assumed).
- When quoting cost, always state the tariff basis (e.g. "at ₹6.50/kWh flat rate").
- No false precision — round to 1 decimal for kWh, 2 decimals for currency.

**G. Answer structure**
1. Direct answer to the question in one or two sentences.
2. Supporting numbers from the data.
3. One concrete next step the user could take, only if the data supports it.
Keep responses short. Use a table only when comparing 3+ items side by side.

**H. Follow-up questions**
Resolve references from conversation history ("last month", "that device", "the cheaper option") \
using prior tool results — do not ask the user to repeat information already provided.

**I. Solar**
Only mention solar if the tool data contains solar fields with non-zero values. \
Never speculate about solar for a household that has not configured it.

Current local time: {now} ({tz}).\
"""


# ─── Conversation management ─────────────────────────────────────────────────


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


# ─── Text-form tool-call compatibility layer (for qwen3 and similar) ─────────

# Matches a JSON object or array after an optional tool-call marker.
# We only look for objects that contain a "name" key matching a known tool.
_TOOL_BLOCK_RE = re.compile(
    r'```(?:json)?\s*(\{[^`]+\})\s*```'   # fenced code block
    r'|'
    r'(\{[^}]*"name"\s*:[^}]+\})',         # bare JSON object with a name key
    re.DOTALL,
)


def _extract_text_calls(text: str) -> list[dict]:
    """
    Detect and extract tool-call proposals emitted as plain text by models that
    do not support native structured tool_calls (e.g. qwen3:4b in some modes).

    Security contract:
    - Only tools present in the MCP TOOLS allow-list are accepted.
    - Arguments are parsed and validated against each tool's inputSchema.
    - Malformed JSON, unknown tool names, and invalid arguments are silently
      dropped (the model's text reply is used as-is instead).
    - No arbitrary code or shell commands are executed.

    Returns a list of {"name": str, "arguments": dict} dicts, or [] if none
    were found or all were invalid.
    """
    if not text:
        return []

    candidates: list[dict] = []
    for m in _TOOL_BLOCK_RE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if not isinstance(name, str) or name not in TOOLS:
            continue

        args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
        if not isinstance(args, dict):
            try:
                args = json.loads(args) if isinstance(args, str) else {}
            except (json.JSONDecodeError, ValueError):
                args = {}

        # Validate args against the tool's inputSchema (raises ToolRejected if invalid)
        from app.mcp.tools import ToolRejected, validate_args
        try:
            validate_args(TOOLS[name].input_schema, args)
        except ToolRejected as exc:
            log.info("Text-form tool call %s rejected: %s", name, exc)
            continue

        candidates.append({"name": name, "arguments": args})

    # Deduplicate: keep at most one call per tool name per turn (prevents loops)
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)

    return unique


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

    for iteration in range(settings.ollama_max_tool_rounds):
        msg = chat_completion(messages, tool_specs)
        content = (msg.get("content") or "").strip()

        # ── 1. Native structured tool calls (standard Ollama/OpenAI path) ──
        native_calls = msg.get("tool_calls") or []

        # ── 2. Text-form tool calls (qwen3 fallback) ───────────────────────
        # Only attempt if the model returned no native calls AND the reply
        # contains what looks like a tool-call proposal.
        text_calls: list[dict] = []
        if not native_calls and content and iteration < settings.ollama_max_tool_rounds - 1:
            text_calls = _extract_text_calls(content)

        calls_to_run = native_calls or text_calls

        if not calls_to_run:
            # No tool calls requested — this is the final answer.
            reply = content
            break

        # Record what the assistant said (including any text that preceded tool calls)
        if native_calls:
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": native_calls,
            })
        else:
            # For text-form calls: strip the JSON block from the content so it
            # isn't shown to the user and re-present as a clean assistant turn.
            messages.append({"role": "assistant", "content": content})

        # ── Execute each tool call ─────────────────────────────────────────
        for call in calls_to_run:
            if native_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments")
            else:
                name = call["name"]
                raw_args = call["arguments"]

            result, tc_out, mcp_rec = _mcp_tool_call(db, household_id, name, raw_args)
            executed.append(tc_out)
            mcp_records.append(mcp_rec)
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)})

    else:
        # Exhausted iterations — ask for a final plain-text answer with no tools.
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


# ─── Streaming variant ────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    """Format one SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def stream_chat(
    db: Session,
    household_id: int,
    user_id: int,
    message: str,
    conversation_id: UUID | None,
) -> Iterator[str]:
    """Generator that yields SSE frames for the streaming chat endpoint.

    Phase 1 — tool execution (synchronous, same as run_chat):
        Emits  event:tool  frames as each tool starts and finishes.

    Phase 2 — streaming reply:
        Emits  event:token  frames for each text token from Ollama.

    Final frame:
        Emits  event:done  with conversation_id and tool_calls metadata.

    On any fatal error emits  event:error  and stops.
    """
    settings = get_settings()
    tz = app_tz()
    now = utcnow()

    cid = _resolve_conversation(db, conversation_id, household_id, user_id)

    try:
        tool_specs, list_record = _mcp_tools_list(db, household_id)
    except OllamaUnavailable:
        yield _sse("error", {"message": "The AI assistant is currently unavailable."})
        return

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

    # ── Phase 1: tool-call loop (non-streaming) ────────────────────────────
    for iteration in range(settings.ollama_max_tool_rounds):
        try:
            msg = chat_completion(messages, tool_specs)
        except OllamaUnavailable:
            yield _sse("error", {"message": "The AI assistant is currently unavailable."})
            return

        content = (msg.get("content") or "").strip()
        native_calls = msg.get("tool_calls") or []

        text_calls: list[dict] = []
        if not native_calls and content and iteration < settings.ollama_max_tool_rounds - 1:
            text_calls = _extract_text_calls(content)

        calls_to_run = native_calls or text_calls

        if not calls_to_run:
            # No more tools — proceed to streaming reply with this message history.
            # `content` may be a complete answer if the model skipped tools entirely
            # on the first pass, or empty if it just finished a tool round.
            # Either way we stream the final answer below.
            if native_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": native_calls,
                })
            break

        # Emit tool-start event
        if native_calls:
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": native_calls,
            })
        else:
            messages.append({"role": "assistant", "content": content})

        for call in calls_to_run:
            if native_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments")
            else:
                name = call["name"]
                raw_args = call["arguments"]

            yield _sse("tool", {"name": name, "status": "running"})

            result, tc_out, mcp_rec = _mcp_tool_call(db, household_id, name, raw_args)
            executed.append(tc_out)
            mcp_records.append(mcp_rec)
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)})

            yield _sse("tool", {
                "name": name,
                "status": tc_out.status,
                "summary": tc_out.summary,
            })
    else:
        # Exhausted tool rounds — add a final non-tool instruction
        messages.append({"role": "user", "content": "Please give your final answer now."})

    # ── Phase 2: stream the final reply token by token ─────────────────────
    reply_tokens: list[str] = []
    try:
        for token in chat_completion_stream(messages):
            reply_tokens.append(token)
            yield _sse("token", {"text": token})
    except OllamaUnavailable:
        yield _sse("error", {"message": "The AI assistant is currently unavailable."})
        return

    reply = "".join(reply_tokens).strip()
    if not reply:
        reply = "Sorry, I couldn't put together an answer. Please try again."

    # Persist conversation
    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="user", content=message))
    db.add(ChatMessage(conversation_id=cid, household_id=household_id, user_id=user_id, role="assistant", content=reply))
    db.commit()

    # Final metadata frame
    yield _sse("done", {
        "conversation_id": cid,
        "tool_calls": [tc.model_dump() for tc in executed],
    })
