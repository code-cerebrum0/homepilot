"""OAuth 2.1 + discovery endpoints.

Routes (all at site root, not under /api/v1):

Discovery:
  GET  /.well-known/oauth-protected-resource
  GET  /.well-known/oauth-protected-resource/mcp   (alias)
  GET  /.well-known/oauth-authorization-server
  GET  /.well-known/openid-configuration           (alias)

Authorization server:
  GET  /oauth/authorize
  POST /oauth/authorize
  POST /oauth/token
  POST /oauth/revoke
  POST /oauth/register   (DCR, gated by OAUTH_ENABLE_DCR)
"""
import logging
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.mcp.dispatch import MCP_PATH, MCP_WELL_KNOWN_ALIAS
from app.models import Membership, OAuthClient, User
from app.rate_limit import SlidingWindowLimiter, login_limiter
from app.security import DUMMY_HASH, verify_password
from app.services.oauth import (
    MCP_SCOPE,
    OAuthError,
    authenticate_client,
    exchange_code,
    fetch_cimd,
    get_client,
    issue_code,
    refresh_tokens,
    register_client,
    revoke_token,
)
from app.utils import utcnow

log = logging.getLogger("homepilot.oauth")

router = APIRouter(tags=["oauth"])

_dcr_limiter = SlidingWindowLimiter()

# ─── Security headers for auth pages ─────────────────────────────────────────

_AUTH_PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

# ─── JWT form token (oauth_txn) ───────────────────────────────────────────────


def _sign_form_token(payload: dict) -> str:
    """Sign a 10-minute form-state JWT (typ: oauth_txn)."""
    s = get_settings()
    now = utcnow()
    claims = {
        **payload,
        "typ": "oauth_txn",
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    return jwt.encode(claims, s.secret_key, algorithm="HS256")


def _decode_form_token(token: str) -> dict:
    """Decode and validate a form-state JWT. Raises ValueError on any failure."""
    s = get_settings()
    try:
        data = jwt.decode(token, s.secret_key, algorithms=["HS256"])
        if data.get("typ") != "oauth_txn":
            raise ValueError("wrong token type")
        return data
    except jwt.PyJWTError as exc:
        raise ValueError(str(exc)) from exc


# ─── HTML helpers ─────────────────────────────────────────────────────────────

_BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:420px;margin:80px auto;padding:0 1rem;color:#1f2328}}
h2{{margin-bottom:.5rem}}
p{{margin:.25rem 0;color:#57606a}}
form{{margin-top:1.5rem}}
label{{display:block;margin-bottom:.25rem;font-size:.9rem}}
input{{width:100%;box-sizing:border-box;padding:.5rem .75rem;border:1px solid #d0d7de;border-radius:6px;font-size:1rem;margin-bottom:.75rem}}
button{{width:100%;padding:.6rem;background:#3b82d4;color:#fff;border:none;border-radius:6px;font-size:1rem;cursor:pointer}}
button:hover{{background:#2563eb}}
.error{{background:#fee2e2;color:#991b1b;padding:.6rem .9rem;border-radius:6px;margin-bottom:1rem;font-size:.9rem}}
.info{{background:#eff6ff;color:#1e40af;padding:.6rem .9rem;border-radius:6px;margin-bottom:1rem;font-size:.9rem}}
.scope-box{{background:#f7f8fa;border:1px solid #e5e7eb;border-radius:6px;padding:.75rem 1rem;margin-bottom:1rem}}
.scope-box ul{{margin:.25rem 0;padding-left:1.2rem}}
</style>
</head>
<body>{body}</body>
</html>"""


def _error_page(title: str, detail: str) -> HTMLResponse:
    body = f"<h2>{title}</h2><p>{detail}</p>"
    return HTMLResponse(_BASE_HTML.format(title=title, body=body), headers=_AUTH_PAGE_HEADERS)


def _login_page(
    form_token: str,
    client_name: str,
    scope: str,
    error: str | None = None,
) -> HTMLResponse:
    err_html = f'<div class="error">{error}</div>' if error else ""
    scope_items = "".join(f"<li>{s}</li>" for s in scope.split())
    body = f"""
<h2>Sign in to HomePilot</h2>
<p><strong>{client_name}</strong> is requesting access to your energy data.</p>
<div class="scope-box">Permissions requested:
<ul>{scope_items}</ul>
</div>
{err_html}
<form method="post">
  <input type="hidden" name="form_token" value="{form_token}">
  <label for="email">Email</label>
  <input type="email" id="email" name="email" autocomplete="username" required>
  <label for="password">Password</label>
  <input type="password" id="password" name="password" autocomplete="current-password" required>
  <button type="submit">Sign in and authorise</button>
</form>"""
    return HTMLResponse(_BASE_HTML.format(title="Sign in — HomePilot", body=body), headers=_AUTH_PAGE_HEADERS)


# ─── Discovery documents ──────────────────────────────────────────────────────


@router.get("/.well-known/oauth-protected-resource")
@router.get(MCP_WELL_KNOWN_ALIAS)
def protected_resource_metadata():
    """RFC 9728: OAuth Protected Resource Metadata."""
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    return JSONResponse({
        "resource": f"{base}{MCP_PATH}",
        "authorization_servers": [base],
        "scopes_supported": [MCP_SCOPE],
        "bearer_methods_supported": ["header"],
    })


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def authorization_server_metadata():
    """RFC 8414: OAuth Authorization Server Metadata."""
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    meta = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": [MCP_SCOPE],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_basic",
            "client_secret_post",
        ],
        "authorization_response_iss_parameter_supported": True,
    }
    if s.oauth_enable_dcr:
        meta["registration_endpoint"] = f"{base}/oauth/register"
    if s.oauth_enable_cimd:
        meta["client_id_metadata_document_supported"] = True
    return JSONResponse(meta)


# ─── Authorization endpoint ───────────────────────────────────────────────────


def _validate_authorize_params(
    db: Session,
    client_id: str,
    redirect_uri: str | None,
    response_type: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
    scope: str | None,
    resource: str | None,
) -> tuple[OAuthClient, str, str, str]:
    """
    Validate GET /oauth/authorize query parameters.
    Returns (client, validated_redirect_uri, validated_scope, validated_resource).
    On error raises OAuthError or returns an error page via exception.
    """
    s = get_settings()

    # Client lookup
    try:
        client = get_client(db, client_id)
    except OAuthError:
        raise OAuthError("invalid_client", "Unknown client_id")

    # redirect_uri: required, exact match
    if not redirect_uri:
        raise OAuthError("invalid_request", "redirect_uri is required")
    if redirect_uri not in client.redirect_uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uri does not match any registered URI")

    # response_type
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "Only response_type=code is supported")

    # PKCE
    if not code_challenge:
        raise OAuthError("invalid_request", "code_challenge is required (PKCE)")
    if code_challenge_method and code_challenge_method != "S256":
        raise OAuthError("invalid_request", "Only code_challenge_method=S256 is supported")

    # Scope
    requested_scope = (scope or MCP_SCOPE).strip()
    if requested_scope != MCP_SCOPE:
        raise OAuthError("invalid_scope", f"Only scope '{MCP_SCOPE}' is supported")

    # Resource (optional; must match if provided)
    mcp_resource = s.public_base_url.rstrip("/") + MCP_PATH
    if resource and resource.rstrip("/") != mcp_resource.rstrip("/"):
        raise OAuthError("invalid_target", f"Unsupported resource; must be {mcp_resource!r}")

    return client, redirect_uri, requested_scope, resource or mcp_resource


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_get(
    request: Request,
    db: Session = Depends(get_db),
    client_id: str = "",
    redirect_uri: str | None = None,
    response_type: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    scope: str | None = None,
    state: str | None = None,
    resource: str | None = None,
):
    # Validate params — errors that cannot redirect must show an error page
    try:
        client, uri, valid_scope, valid_resource = _validate_authorize_params(
            db, client_id, redirect_uri, response_type, code_challenge, code_challenge_method, scope, resource
        )
    except OAuthError as exc:
        return _error_page("Authorization Error", exc.description)

    # Sign validated params into a form token
    form_token = _sign_form_token({
        "client_id": client.id,
        "redirect_uri": uri,
        "scope": valid_scope,
        "resource": valid_resource,
        "code_challenge": code_challenge,
        "state": state,
    })
    return _login_page(form_token, client.name, valid_scope)


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def authorize_post(
    request: Request,
    db: Session = Depends(get_db),
    form_token: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    # 1. Decode and verify the signed form token
    try:
        txn = _decode_form_token(form_token)
    except ValueError:
        return _error_page("Session Expired", "The authorization session has expired. Please start again.")

    client_id = txn["client_id"]
    redirect_uri = txn["redirect_uri"]
    scope = txn["scope"]
    resource = txn["resource"]
    code_challenge = txn["code_challenge"]
    state = txn.get("state")

    try:
        client = get_client(db, client_id)
    except OAuthError:
        return _error_page("Invalid Client", "The client is no longer registered.")

    # 2. Authenticate user (reuse existing Argon2 + rate limiter)
    ip = request.client.host if request.client else "unknown"
    s = get_settings()
    rate_key = f"oauth:{ip}:{email.lower()}"
    if login_limiter.count(rate_key, s.login_window_seconds) >= s.login_max_failures:
        return _login_page(form_token, client.name, scope, error="Too many failed attempts. Please try again later.")

    user = db.scalar(select(User).where(User.email == email.lower()))
    ok = verify_password(user.password_hash if user else DUMMY_HASH, password)
    if user is None or not ok:
        login_limiter.add(rate_key)
        return _login_page(form_token, client.name, scope, error="Invalid email or password.")

    login_limiter.clear(rate_key)

    # 3. Find user's first household membership
    membership = db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id)
        .order_by(Membership.id)
        .limit(1)
    )
    if membership is None:
        return _login_page(form_token, client.name, scope, error="Your account has no household. Please contact support.")

    # 4. Issue authorization code
    try:
        code_raw = issue_code(
            db=db,
            client=client,
            user_id=user.id,
            household_id=membership.household_id,
            redirect_uri=redirect_uri,
            scope=scope,
            pkce_challenge=code_challenge,
            resource=resource,
        )
    except Exception:
        log.exception("Failed to issue authorization code")
        return _error_page("Server Error", "Could not issue authorization code. Please try again.")

    # 5. Build redirect URL
    issuer = s.public_base_url.rstrip("/")
    qs = f"code={code_raw}&iss={issuer}"
    if state:
        qs += f"&state={state}"
    redirect_url = f"{redirect_uri}?{qs}" if "?" not in redirect_uri else f"{redirect_uri}&{qs}"
    resp = RedirectResponse(url=redirect_url, status_code=303)
    for k, v in _AUTH_PAGE_HEADERS.items():
        resp.headers[k] = v
    return resp


# ─── Token endpoint ───────────────────────────────────────────────────────────


def _token_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
        headers={"Cache-Control": "no-store"},
    )


def _extract_client_credentials(request: Request, form_client_id: str, form_secret: str | None):
    """Extract client_id/secret from Basic auth header or form params."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            cid, _, csec = decoded.partition(":")
            return cid.strip(), csec.strip() or None
        except Exception:
            pass
    return form_client_id, form_secret or None


@router.post("/oauth/token")
async def token_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    grant_type: Annotated[str, Form()] = "",
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str | None, Form()] = None,
    resource: Annotated[str | None, Form()] = None,
):
    cid, csec = _extract_client_credentials(request, client_id, client_secret)

    s = get_settings()

    try:
        if grant_type == "authorization_code":
            if not code:
                return _token_error("invalid_request", "code is required")
            if not redirect_uri:
                return _token_error("invalid_request", "redirect_uri is required")
            if not code_verifier:
                return _token_error("invalid_request", "code_verifier is required")
            access_raw, refresh_raw = exchange_code(
                db=db,
                client_id=cid,
                client_secret=csec,
                code_raw=code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                resource=resource,
            )
        elif grant_type == "refresh_token":
            if not refresh_token:
                return _token_error("invalid_request", "refresh_token is required")
            access_raw, refresh_raw = refresh_tokens(
                db=db,
                client_id=cid,
                client_secret=csec,
                refresh_raw=refresh_token,
            )
        else:
            return _token_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type!r}")
    except OAuthError as exc:
        return _token_error(exc.error, exc.description, exc.status)

    return JSONResponse(
        {
            "access_token": access_raw,
            "token_type": "Bearer",
            "expires_in": s.oauth_access_ttl_seconds,
            "refresh_token": refresh_raw,
            "scope": MCP_SCOPE,
        },
        headers={"Cache-Control": "no-store"},
    )


# ─── Revocation endpoint (RFC 7009) ──────────────────────────────────────────


@router.post("/oauth/revoke")
async def revoke_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    token: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str | None, Form()] = None,
):
    cid, csec = _extract_client_credentials(request, client_id, client_secret)
    try:
        authenticate_client(db, cid, csec)
    except OAuthError:
        pass  # RFC 7009: always 200
    if token:
        revoke_token(db, token, cid)
    return JSONResponse({}, headers={"Cache-Control": "no-store"})


# ─── Dynamic Client Registration (RFC 7591) ──────────────────────────────────


@router.post("/oauth/register", status_code=201)
async def dcr_endpoint(
    request: Request,
    db: Session = Depends(get_db),
):
    s = get_settings()
    if not s.oauth_enable_dcr:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    ip = request.client.host if request.client else "unknown"
    if not _dcr_limiter.allow(f"dcr:{ip}", 10, 60.0):
        return JSONResponse(status_code=429, content={"error": "rate_limited"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_client_metadata", "error_description": "Invalid JSON"})

    try:
        client = register_client(db, body, s)
    except OAuthError as exc:
        return JSONResponse(status_code=400, content={"error": exc.error, "error_description": exc.description})

    base = s.public_base_url.rstrip("/")
    return JSONResponse(
        status_code=201,
        content={
            "client_id": client.id,
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "registration_access_token": None,
            "registration_client_uri": None,
        },
    )
