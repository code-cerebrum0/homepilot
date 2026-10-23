"""MCP HTTP router — POST /mcp/v1 (Streamable HTTP, stateless, dual-era).

Authentication is Bearer token (OAuth 2.1).
CSRF exemption is declared in csrf.py (path starts with /mcp).
Origin validation, rate limiting, and body parsing live here.
"""
import base64
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.mcp.dispatch import (
    MCP_PATH,
    PROTO_MODERN,
    SUPPORTED_VERSIONS,
    HEADER_MISMATCH,
    UNSUPPORTED_VERSION,
    dispatch,
)
from app.rate_limit import SlidingWindowLimiter
from app.services.oauth import MCP_SCOPE, OAuthError, validate_bearer

log = logging.getLogger("homepilot.mcp.router")

router = APIRouter(tags=["mcp"])

_mcp_limiter = SlidingWindowLimiter()

MAX_BODY_BYTES = 256 * 1024  # 256 KB


# ─── WWW-Authenticate header builder ─────────────────────────────────────────


def _www_auth(error: str | None = None) -> str:
    s = get_settings()
    # resource_metadata points to the protected-resource discovery doc for this endpoint
    rm_url = f"{s.public_base_url.rstrip('/')}/.well-known/oauth-protected-resource"
    hdr = f'Bearer resource_metadata="{rm_url}", scope="{MCP_SCOPE}"'
    if error:
        hdr += f', error="{error}"'
    return hdr


def _401(msg: str, error_token: str | None = "invalid_token") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": msg},
        headers={"WWW-Authenticate": _www_auth(error_token)},
    )


def _403(msg: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "forbidden", "message": msg})


def _400_rpc(code: int, message: str, req_id=None) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
    )


# ─── Origin validation ────────────────────────────────────────────────────────


def _check_origin(request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin is None:
        return None  # non-browser clients omit Origin
    s = get_settings()
    allowed = s.mcp_allowed_origins_set
    if origin.rstrip("/") not in {o.rstrip("/") for o in allowed}:
        log.warning("MCP: blocked Origin %s", origin)
        return _403("Origin not allowed")
    return None


# ─── Decode MCP-Name base64 sentinel ─────────────────────────────────────────


def _decode_mcp_name(raw: str) -> str:
    """Decode the =?base64?...?= sentinel format if present; otherwise return as-is."""
    raw = raw.strip()
    if raw.startswith("=?base64?") and raw.endswith("?="):
        encoded = raw[len("=?base64?"):-2]
        try:
            return base64.b64decode(encoded + "==").decode("utf-8")
        except Exception:
            return raw
    return raw


# ─── Main endpoint ─────────────────────────────────────────────────────────────


@router.api_route(MCP_PATH, methods=["GET", "POST", "DELETE"])
async def mcp_endpoint(request: Request, db: Session = Depends(get_db)):
    # GET / DELETE → 405
    if request.method in ("GET", "DELETE"):
        return Response(status_code=405, headers={"Allow": "POST"})

    # Origin check
    if (err := _check_origin(request)) is not None:
        return err

    # ── Authentication (bearer token) ─────────────────────────────────────
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return _401("Bearer token required", error_token=None)

    raw_token = auth_header[7:].strip()
    try:
        token_info = validate_bearer(db, raw_token)
    except OAuthError as exc:
        return _401(exc.description, "invalid_token")

    # Scope check
    if MCP_SCOPE not in token_info.scope.split():
        return JSONResponse(
            status_code=403,
            content={"error": "insufficient_scope", "message": "Scope energy:read required"},
            headers={"WWW-Authenticate": _www_auth("insufficient_scope")},
        )

    # Rate limit per token
    s = get_settings()
    rate_key = f"mcp:{token_info.user_id}"
    if not _mcp_limiter.allow(rate_key, s.mcp_rate_limit_per_minute, 60.0):
        return JSONResponse(status_code=429, content={"error": "rate_limited", "message": "Too many requests"})

    # ── Body parsing ──────────────────────────────────────────────────────
    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_BYTES:
        return _400_rpc(-32600, "Request body exceeds 256 KB limit")

    try:
        rpc = json.loads(body_bytes)
    except json.JSONDecodeError:
        return _400_rpc(-32700, "Parse error: body is not valid JSON")

    if isinstance(rpc, list):
        return _400_rpc(-32600, "JSON-RPC batching is not supported")

    if not isinstance(rpc, dict) or "method" not in rpc:
        return _400_rpc(-32600, "Invalid Request: must be a JSON-RPC object with a method")

    method = rpc.get("method", "")
    params = rpc.get("params") or {}
    req_id = rpc.get("id")  # None for notifications

    # ── Era detection ─────────────────────────────────────────────────────
    # Modern era: _meta has io.modelcontextprotocol/protocolVersion
    meta = params.get("_meta") if isinstance(params, dict) else None
    meta_version = (meta or {}).get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
    header_version = request.headers.get("mcp-protocol-version")

    is_modern = bool(meta_version or header_version == PROTO_MODERN)

    # Determine effective protocol version
    if is_modern:
        proto_version = meta_version or header_version
    else:
        proto_version = header_version

    # ── Modern-era header validation ─────────────────────────────────────
    if is_modern and req_id is not None:  # skip for notifications
        # 1. MCP-Protocol-Version header required and must match _meta value
        if not header_version:
            return _400_rpc(HEADER_MISMATCH, "Missing MCP-Protocol-Version header", req_id)
        if meta_version and header_version != meta_version:
            return _400_rpc(
                HEADER_MISMATCH,
                f"MCP-Protocol-Version header ({header_version}) does not match _meta version ({meta_version})",
                req_id,
            )

        # 2. Unsupported version
        if header_version not in SUPPORTED_VERSIONS:
            return _400_rpc(
                UNSUPPORTED_VERSION,
                "Unsupported protocol version",
                req_id,
            )

        # 3. Mcp-Method header required and must match method
        mcp_method_header = request.headers.get("mcp-method")
        if not mcp_method_header:
            return _400_rpc(HEADER_MISMATCH, "Missing Mcp-Method header", req_id)
        if mcp_method_header != method:
            return _400_rpc(
                HEADER_MISMATCH,
                f"Mcp-Method header ({mcp_method_header!r}) does not match method ({method!r})",
                req_id,
            )

        # 4. For tools/call: Mcp-Name header required and must match params.name
        if method == "tools/call":
            mcp_name_header = request.headers.get("mcp-name")
            if not mcp_name_header:
                return _400_rpc(HEADER_MISMATCH, "Missing Mcp-Name header for tools/call", req_id)
            decoded_name = _decode_mcp_name(mcp_name_header)
            param_name = (params.get("name") or "") if isinstance(params, dict) else ""
            if decoded_name != param_name:
                return _400_rpc(
                    HEADER_MISMATCH,
                    f"Mcp-Name header ({decoded_name!r}) does not match params.name ({param_name!r})",
                    req_id,
                )

    # ── Legacy-era version checks ─────────────────────────────────────────
    if not is_modern and method not in ("initialize", "notifications/initialized") and req_id is not None:
        if not header_version:
            return _400_rpc(HEADER_MISMATCH, "Missing MCP-Protocol-Version header", req_id)
        if header_version not in SUPPORTED_VERSIONS:
            return _400_rpc(
                UNSUPPORTED_VERSION,
                "Unsupported protocol version",
                req_id,
            )

    # ── Dispatch ──────────────────────────────────────────────────────────
    result = dispatch(
        rpc,
        db,
        token_info.household_id,
        is_modern=is_modern,
        proto_version=proto_version,
    )

    if result.is_notification:
        return Response(status_code=202)

    return JSONResponse(status_code=result.http_status, content=result.body)
