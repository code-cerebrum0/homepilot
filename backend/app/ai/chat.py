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
You are HomePilot, a household electricity analyst. Give accurate, specific answers \
grounded entirely in the data returned by your tools. Never invent readings, costs, \
anomalies, or appliance behaviour.

## A. How to call a tool

When you need data, output a JSON code block on its own line and nothing else that turn:

```json
{{"name": "tool_name", "arguments": {{}}}}
```

Examples with arguments:
- get_daily_usage: {{"name": "get_daily_usage", "arguments": {{"days": 7}}}}
- compare_energy_usage: {{"name": "compare_energy_usage", "arguments": {{"comparison": "this_week_vs_last_week"}}}}
- get_device_breakdown: {{"name": "get_device_breakdown", "arguments": {{"period": "last_7_days"}}}}
- get_solar_generation: {{"name": "get_solar_generation", "arguments": {{"days": 7}}}}
- get_usage_anomalies: {{"name": "get_usage_anomalies", "arguments": {{}}}}

After receiving the tool result, give your final answer in plain prose. \
Never output a JSON tool-call block and a prose answer in the same turn.

## B. Always call the right tool first

Any question about usage, cost, devices, savings, or anomalies requires tool data. \
Never answer from memory or general knowledge.

Tool routing — match the question to the correct tool:
- Current power / right now → get_current_usage
- Historical usage / how much per day → get_daily_usage
- Compare two periods → compare_energy_usage
- Which device uses the most → get_device_breakdown
- Electricity cost / bill → get_cost_estimate (then get_tariff_info if tariff detail needed)
- Solar production → get_solar_generation
- Savings / reduce bill / wasteful devices → get_savings_opportunities AND get_device_breakdown (last_7_days)
- Abnormal / unusual / spike / anomaly → get_usage_anomalies (NOT get_savings_opportunities)
- What devices are tracked → get_household_context
- Tariff / rate → get_tariff_info

For compound questions call multiple tools. Stop when you have sufficient data.

## C. Interpreting tool results honestly

Status field rules:
- status "insufficient_data" → explain what is missing. Do NOT claim the household is efficient.
- status "error" → say the analysis failed. Do NOT invent a result.
- has_data false → say what was checked and what was not found. Do NOT pad with generic advice.
- Empty opportunities list → the overnight standby check found nothing; it does NOT mean the \
household is energy-efficient overall. Explain the scope of the check.
- Empty findings from get_usage_anomalies → no statistical anomaly detected in the window. \
Cite the baseline statistics (median, MAD, days used).

Anomaly tool rules:
- get_usage_anomalies returns confirmed true only when deviation is at least 3 MAD from median.
- confirmed false findings are borderline — describe as "worth monitoring", not as confirmed anomalies.
- Never call get_savings_opportunities to answer an anomaly question.

Savings tool rules:
- get_savings_opportunities checks overnight standby only (02:00–05:00, >50 W).
- If no opportunities found, explain what was checked, not that the home is efficient.
- After calling savings, also call get_device_breakdown for appliance-specific context.

## C. Evidence and accuracy rules

1. Never invent readings, costs, savings amounts, anomalies, or appliance behaviour.
2. Distinguish measured values from calculated values and estimates.
3. Never interpret has_data false, an empty result, or a tool failure as proof of normal usage.
4. If analysis is inconclusive, explain exactly what is missing.
5. Only report normal or abnormal usage when the analysis actually supports it.
6. Do not present generic energy-saving advice as if derived from measurements.
7. Do not claim a tool succeeded if it returned an error status.
8. Never repeat a tool call if you already have a sufficient result this turn.
9. Keep responses concise and understandable to non-technical household users.

## D. Number discipline

- Show period and units for every figure (e.g. "46.1 kWh/day average over 7 days").
- Distinguish measured from calculated from estimated.
- When quoting cost, state the tariff basis (e.g. "at 6.50 INR/kWh flat rate").
- Round kWh to 1 decimal, currency to 2 decimals.

## E. Answer structure

1. Direct answer in one or two sentences.
2. Supporting numbers from the tool data.
3. One concrete next step, only if the data supports it.
Keep responses short. Use a table only when comparing 3 or more items.

## F. Solar

Only mention solar if tool data contains solar fields with non-zero values. \
Never speculate about solar for a household that has not configured it. \
Consumption, solar generation, and net grid draw are separate metrics — never conflate them.

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
