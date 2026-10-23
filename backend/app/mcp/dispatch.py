"""MCP JSON-RPC dispatcher (stateless, dual-era).

Supports:
- Modern era:  protocol 2026-07-28  (server/discover, tools/list, tools/call)
- Legacy era:  protocol 2025-11-25 and 2025-06-18
               (initialize, notifications/initialized, ping, tools/list, tools/call)

Called both by the HTTP router and directly by the in-process chat client.
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.mcp.tools import TOOLS, ToolContext, ToolRejected, render_text, validate_args
from app.utils import app_tz, utcnow

log = logging.getLogger("homepilot.mcp")

# ─── Path constants (single source of truth) ─────────────────────────────────
# Change MCP_PATH here to move the entire MCP server to a new URL.
# Every router, discovery doc, OAuth audience, and CORS rule imports from here.

MCP_PATH = "/mcp/v1"          # HTTP endpoint:  POST /mcp/v1
MCP_WELL_KNOWN_ALIAS = "/.well-known/oauth-protected-resource/mcp"  # discovery alias

# ─── Protocol constants ───────────────────────────────────────────────────────

PROTO_MODERN = "2026-07-28"
PROTO_LEGACY_1 = "2025-11-25"
PROTO_LEGACY_2 = "2025-06-18"
SUPPORTED_VERSIONS = [PROTO_MODERN, PROTO_LEGACY_1, PROTO_LEGACY_2]

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

SERVER_INFO = {"name": "HomePilot MCP", "version": "1.0.0"}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _mcp_resource_url() -> str:
    """Canonical MCP resource URL: PUBLIC_BASE_URL + MCP_PATH."""
    return get_settings().public_base_url.rstrip("/") + MCP_PATH


def _server_meta() -> dict:
    return {
        "io.modelcontextprotocol/serverInfo": {
            **SERVER_INFO,
            "resource": _mcp_resource_url(),
        }
    }


def _tool_list_result(is_modern: bool) -> dict:
    result: dict = {
        "tools": [t.mcp_spec() for t in TOOLS.values()],
    }
    if is_modern:
        result["resultType"] = "complete"
        result["ttlMs"] = 300_000
        result["cacheScope"] = "global"
        result["_meta"] = _server_meta()
    return result


def _discovery_result() -> dict:
    s = get_settings()
    return {
        "resultType": "complete",
        "supportedVersions": SUPPORTED_VERSIONS,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "HomePilot exposes read-only energy data for a single household. "
            "All tools require scope 'energy:read'. "
            "The household is bound to the access token; do not pass household_id."
        ),
        "ttlMs": 3_600_000,
        "cacheScope": "global",
        "_meta": {
            **_server_meta(),
            "io.modelcontextprotocol/resource": _mcp_resource_url(),
        },
    }


# ─── Dispatch ─────────────────────────────────────────────────────────────────

@dataclass
class DispatchResult:
    """Holds the JSON-RPC result dict and whether to respond at all (202 for notifications)."""
    body: dict | None           # None => notification, respond 202 empty
    is_notification: bool = False
    http_status: int = 200


def dispatch(
    rpc: dict,
    db: Session,
    household_id: int,
    *,
    is_modern: bool,
    proto_version: str | None = None,
) -> DispatchResult:
    """
    Core dispatcher. Called after auth + body parsing.

    is_modern:     True when the request is modern-era (2026-07-28).
    proto_version: The protocol version string from the header / _meta.
    """
    req_id = rpc.get("id")
    method = rpc.get("method", "")
    params = rpc.get("params") or {}

    # Notification (no id) → 202, no body
    if req_id is None:
        log.debug("MCP notification: %s", method)
        return DispatchResult(body=None, is_notification=True, http_status=202)

    ctx = ToolContext(db=db, household_id=household_id, tz=app_tz(), now=utcnow())

    # ── Modern era ──────────────────────────────────────────────────────────
    if is_modern:
        if method == "server/discover":
            result = _ok(req_id, _discovery_result())
            return DispatchResult(body=result)

        if method == "tools/list":
            return DispatchResult(body=_ok(req_id, _tool_list_result(is_modern=True)))

        if method == "tools/call":
            return DispatchResult(body=_ok(req_id, _execute_tool(req_id, params, ctx, is_modern=True)))

        # Unknown method → 404 in modern era
        return DispatchResult(
            body=_err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}"),
            http_status=404,
        )

    # ── Legacy era ──────────────────────────────────────────────────────────
    if method == "initialize":
        requested = (params.get("protocolVersion") or "")
        echo_version = requested if requested in SUPPORTED_VERSIONS else PROTO_LEGACY_1
        result = _ok(req_id, {
            "protocolVersion": echo_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "HomePilot exposes read-only energy data. "
                "All tools require scope 'energy:read'."
            ),
        })
        return DispatchResult(body=result)

    if method == "ping":
        return DispatchResult(body=_ok(req_id, {}))

    if method == "tools/list":
        return DispatchResult(body=_ok(req_id, _tool_list_result(is_modern=False)))

    if method == "tools/call":
        return DispatchResult(body=_ok(req_id, _execute_tool(req_id, params, ctx, is_modern=False)))

    # Unknown method → 200 with -32601 in legacy era
    return DispatchResult(
        body=_err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}"),
        http_status=200,
    )


# ─── Tool execution ───────────────────────────────────────────────────────────

def _parse_args(raw: Any) -> object:
    """Coerce raw tool arguments to a dict. Handles both dict and JSON-string forms."""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ToolRejected("arguments are not valid JSON")
    return raw if raw is not None else {}


def _execute_tool(req_id: Any, params: dict, ctx: ToolContext, *, is_modern: bool) -> dict:
    """Run a tool and return the MCP tool-result dict (isError may be true)."""
    name = params.get("name", "")
    raw_args = params.get("arguments") or {}

    tool = TOOLS.get(name)
    if tool is None:
        # Unknown tool → JSON-RPC error -32602
        return _error_result(f"Unknown tool: {name}", is_modern)

    try:
        args = validate_args(tool.input_schema, _parse_args(raw_args))
    except ToolRejected as exc:
        log.info("Tool %s args rejected: %s", name, exc)
        return _error_result(str(exc), is_modern)

    try:
        data = tool.handler(ctx, args)
    except Exception:
        log.exception("Tool %s raised an exception", name)
        ctx.db.rollback()
        return _error_result("The tool encountered an internal error.", is_modern)

    text = render_text(name, data)
    label = tool.label_fn(args)

    result: dict = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": False,
    }
    if is_modern:
        result["resultType"] = "complete"
        result["_meta"] = {
            **_server_meta(),
            "homepilot/label": label,
        }
    else:
        result["_meta"] = {"homepilot/label": label}
    return result


def _error_result(message: str, is_modern: bool) -> dict:
    result: dict = {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {},
        "isError": True,
    }
    if is_modern:
        result["resultType"] = "complete"
        result["_meta"] = _server_meta()
    return result


# ─── In-process client (used by chat.py) ─────────────────────────────────────

class McpCallRecord:
    """Timing + outcome record for one in-process MCP call."""
    __slots__ = ("method", "tool_name", "protocol", "duration_ms", "ok")

    def __init__(self, method: str, tool_name: str | None, protocol: str, duration_ms: float, ok: bool):
        self.method = method
        self.tool_name = tool_name
        self.protocol = protocol
        self.duration_ms = duration_ms
        self.ok = ok


def call_in_process(
    db: Session,
    household_id: int,
    method: str,
    params: dict | None = None,
) -> tuple[dict, McpCallRecord]:
    """
    Send a JSON-RPC call to the in-process MCP server.

    Always uses modern era (2026-07-28) with _meta version tagging.
    Returns (result_dict, McpCallRecord).
    Raises RuntimeError on JSON-RPC error (still records timing before raising).
    """
    params = params or {}
    if "_meta" not in params:
        params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": PROTO_MODERN}}

    tool_name: str | None = params.get("name") if method == "tools/call" else None

    rpc = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

    t0 = time.perf_counter()
    dr = dispatch(rpc, db, household_id, is_modern=True, proto_version=PROTO_MODERN)
    duration_ms = (time.perf_counter() - t0) * 1000

    body = dr.body or {}
    ok = "error" not in body

    # Determine if the tool itself returned isError (not a protocol error)
    if ok and method == "tools/call":
        ok = not (body.get("result") or {}).get("isError", False)

    record = McpCallRecord(
        method=method,
        tool_name=tool_name,
        protocol=PROTO_MODERN,
        duration_ms=round(duration_ms, 2),
        ok=ok,
    )

    log.info(
        "[MCP in-process] method=%s tool=%s proto=%s duration_ms=%.1f ok=%s",
        method,
        tool_name or "-",
        PROTO_MODERN,
        duration_ms,
        ok,
    )

    if not ok and "error" in body:
        raise RuntimeError(f"MCP error {body['error']['code']}: {body['error']['message']}")

    return body.get("result", {}), record
