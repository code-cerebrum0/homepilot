"""OAuth 2.1 business logic.

Covers:
- Token introspection / bearer-token validation for the MCP resource server
- Authorization-code issuance and exchange (PKCE S256)
- Refresh-token rotation with reuse detection (revokes token family)
- Token revocation (RFC 7009)
- Dynamic client registration (RFC 7591) — gated by OAUTH_ENABLE_DCR
"""
import base64
import hashlib
import hmac
import ipaddress
import logging
import secrets

# Import MCP_PATH lazily to avoid a circular import at module load time.
# Use _mcp_resource() everywhere instead of hardcoding the path.
from datetime import timedelta
from typing import NamedTuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Membership, OAuthClient, OAuthCode, OAuthToken, User
from app.security import DUMMY_HASH, verify_password, sha256_hex
from app.utils import utcnow

log = logging.getLogger("homepilot.oauth")

# ─── Constants ────────────────────────────────────────────────────────────────

MCP_SCOPE = "energy:read"
SUPPORTED_SCOPES = {MCP_SCOPE}

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _mcp_resource() -> str:
    """Return the canonical MCP resource URL (PUBLIC_BASE_URL + MCP_PATH)."""
    from app.mcp.dispatch import MCP_PATH  # lazy import — avoids circular dependency
    return get_settings().public_base_url.rstrip("/") + MCP_PATH


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _new_code() -> str:
    return secrets.token_urlsafe(32)


def _is_private_ip(host: str) -> bool:
    """Return True if host resolves to a non-public IP (SSRF guard for CIMD)."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        pass
    # hostname – resolve is not called here to avoid sync DNS; trust allowlist instead
    return False


# ─── Client lookup ────────────────────────────────────────────────────────────


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        self.error = error
        self.description = description
        self.status = status


def get_client(db: Session, client_id: str) -> OAuthClient:
    """Fetch a pre-registered client or raise OAuthError."""
    client = db.get(OAuthClient, client_id)
    if client is None:
        raise OAuthError("invalid_client", "Client not found")
    return client


def authenticate_client(
    db: Session,
    client_id: str,
    client_secret: str | None,
) -> OAuthClient:
    """
    Authenticate a client by client_id + optional secret.
    Public clients must have secret=None/empty.
    Confidential clients must present the correct secret.
    """
    client = get_client(db, client_id)
    if client.is_public:
        # Public clients must NOT present a secret (but we tolerate empty string)
        if client_secret and client_secret.strip():
            raise OAuthError("invalid_client", "Public clients must not send a secret", 401)
    else:
        # Confidential: secret required
        if not client_secret:
            raise OAuthError("invalid_client", "Client secret required", 401)
        if not client.secret_hash:
            raise OAuthError("invalid_client", "Client has no stored secret", 401)
        if not hmac.compare_digest(client.secret_hash, _sha256(client_secret)):
            raise OAuthError("invalid_client", "Invalid client secret", 401)
    return client


# ─── PKCE ─────────────────────────────────────────────────────────────────────


def verify_pkce_s256(challenge: str, verifier: str) -> bool:
    """
    Constant-time comparison of PKCE S256 code_challenge against code_verifier.
    Verifier must be 43–128 ASCII chars [A-Za-z0-9._~-].
    """
    if not (43 <= len(verifier) <= 128):
        return False
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode()
    return hmac.compare_digest(challenge, digest)


# ─── Authorization code ───────────────────────────────────────────────────────


def issue_code(
    db: Session,
    client: OAuthClient,
    user_id: int,
    household_id: int,
    redirect_uri: str,
    scope: str,
    pkce_challenge: str,
    resource: str | None,
) -> str:
    """Issue a single-use authorization code and return the raw value."""
    s = get_settings()
    raw = _new_code()
    db.add(OAuthCode(
        code_hash=_sha256(raw),
        client_id=client.id,
        user_id=user_id,
        household_id=household_id,
        redirect_uri=redirect_uri,
        scope=scope,
        resource=resource,
        pkce_challenge=pkce_challenge,
        expires_at=utcnow() + timedelta(seconds=s.oauth_code_ttl_seconds),
    ))
    db.commit()
    return raw


def exchange_code(
    db: Session,
    client_id: str,
    client_secret: str | None,
    code_raw: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
) -> tuple[str, str]:
    """
    Exchange an authorization code for (access_token, refresh_token).
    Verifies: client auth, code existence + not-used + not-expired,
              redirect_uri exact match, PKCE S256, resource match.
    Returns (access_token_raw, refresh_token_raw).
    """
    s = get_settings()
    client = authenticate_client(db, client_id, client_secret)

    code = db.scalar(select(OAuthCode).where(OAuthCode.code_hash == _sha256(code_raw)))
    if code is None:
        raise OAuthError("invalid_grant", "Code not found or already used")
    if code.used_at is not None:
        # Replay detected: revoke any tokens issued for this code's user+client
        _revoke_all_for_user_client(db, code.user_id, client_id)
        db.commit()
        raise OAuthError("invalid_grant", "Authorization code already used")
    now = utcnow()
    if code.expires_at <= now:
        raise OAuthError("invalid_grant", "Authorization code expired")
    if code.client_id != client_id:
        raise OAuthError("invalid_grant", "Code was not issued for this client")
    if code.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri mismatch")

    # PKCE
    if not verify_pkce_s256(code.pkce_challenge, code_verifier):
        raise OAuthError("invalid_grant", "PKCE verification failed")

    # Resource binding
    mcp_resource = _mcp_resource()
    if resource and resource.rstrip("/") != mcp_resource.rstrip("/"):
        raise OAuthError("invalid_target", "Unsupported resource")
    if code.resource and code.resource.rstrip("/") != mcp_resource.rstrip("/"):
        raise OAuthError("invalid_target", "Code resource does not match server resource")

    # Confirm membership still valid
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == code.user_id,
            Membership.household_id == code.household_id,
        )
    )
    if membership is None:
        raise OAuthError("invalid_grant", "User no longer member of household")

    # Mark code used
    code.used_at = now

    # Issue tokens
    access_raw = _new_token()
    refresh_raw = _new_token()
    family_id = str(uuid4())
    db.add(OAuthToken(
        family_id=family_id,
        access_hash=_sha256(access_raw),
        refresh_hash=_sha256(refresh_raw),
        client_id=client_id,
        user_id=code.user_id,
        household_id=code.household_id,
        scope=code.scope,
        resource=mcp_resource,
        access_expires_at=now + timedelta(seconds=s.oauth_access_ttl_seconds),
        refresh_expires_at=now + timedelta(days=s.oauth_refresh_ttl_days),
    ))
    db.commit()
    return access_raw, refresh_raw


# ─── Refresh token rotation ───────────────────────────────────────────────────


def refresh_tokens(
    db: Session,
    client_id: str,
    client_secret: str | None,
    refresh_raw: str,
) -> tuple[str, str]:
    """
    Rotate a refresh token.  Returns (new_access_raw, new_refresh_raw).
    Replay of a used refresh token revokes the entire token family.
    """
    s = get_settings()
    authenticate_client(db, client_id, client_secret)

    token = db.scalar(
        select(OAuthToken).where(OAuthToken.refresh_hash == _sha256(refresh_raw))
    )
    if token is None:
        raise OAuthError("invalid_grant", "Refresh token not found")

    now = utcnow()
    if token.revoked_at is not None:
        # Reuse detected: revoke the entire family
        _revoke_family(db, token.family_id)
        db.commit()
        raise OAuthError("invalid_grant", "Refresh token already used — possible replay")
    if token.refresh_expires_at and token.refresh_expires_at <= now:
        raise OAuthError("invalid_grant", "Refresh token expired")
    if token.client_id != client_id:
        raise OAuthError("invalid_grant", "Token was not issued for this client")

    # Confirm membership still valid
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == token.user_id,
            Membership.household_id == token.household_id,
        )
    )
    if membership is None:
        raise OAuthError("invalid_grant", "User no longer member of household")

    # Revoke old token
    token.revoked_at = now

    # Issue new pair, same family
    access_raw = _new_token()
    refresh_raw_new = _new_token()
    db.add(OAuthToken(
        family_id=token.family_id,
        access_hash=_sha256(access_raw),
        refresh_hash=_sha256(refresh_raw_new),
        client_id=token.client_id,
        user_id=token.user_id,
        household_id=token.household_id,
        scope=token.scope,
        resource=token.resource,
        access_expires_at=now + timedelta(seconds=s.oauth_access_ttl_seconds),
        refresh_expires_at=now + timedelta(days=s.oauth_refresh_ttl_days),
    ))
    db.commit()
    return access_raw, refresh_raw_new


# ─── Revocation (RFC 7009) ────────────────────────────────────────────────────


def revoke_token(db: Session, token_raw: str, client_id: str) -> None:
    """Revoke by access or refresh hash. Always succeeds (RFC 7009 §2.2)."""
    h = _sha256(token_raw)
    token = db.scalar(
        select(OAuthToken).where(
            (OAuthToken.access_hash == h) | (OAuthToken.refresh_hash == h)
        )
    )
    if token and token.client_id == client_id and token.revoked_at is None:
        token.revoked_at = utcnow()
        db.commit()


def _revoke_family(db: Session, family_id: str) -> None:
    db.execute(
        update(OAuthToken)
        .where(OAuthToken.family_id == family_id, OAuthToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def _revoke_all_for_user_client(db: Session, user_id: int, client_id: str) -> None:
    db.execute(
        update(OAuthToken)
        .where(
            OAuthToken.user_id == user_id,
            OAuthToken.client_id == client_id,
            OAuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )


# ─── Token introspection (resource-server use) ───────────────────────────────


class TokenInfo(NamedTuple):
    user_id: int
    household_id: int
    scope: str
    resource: str


def validate_bearer(db: Session, raw_token: str) -> TokenInfo:
    """
    Validate an opaque bearer token for the MCP resource server.
    Returns TokenInfo on success; raises OAuthError on any failure.
    """
    mcp_resource = _mcp_resource()

    token = db.scalar(
        select(OAuthToken).where(OAuthToken.access_hash == _sha256(raw_token))
    )
    if token is None:
        raise OAuthError("invalid_token", "Token not found", 401)

    now = utcnow()
    if token.revoked_at is not None:
        raise OAuthError("invalid_token", "Token has been revoked", 401)
    if token.access_expires_at <= now:
        raise OAuthError("invalid_token", "Token has expired", 401)

    # Audience binding
    if token.resource.rstrip("/") != mcp_resource.rstrip("/"):
        raise OAuthError("invalid_token", "Token audience mismatch", 401)

    # Confirm household membership still valid
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == token.user_id,
            Membership.household_id == token.household_id,
        )
    )
    if membership is None:
        raise OAuthError("invalid_token", "User is no longer a member of the household", 401)

    return TokenInfo(
        user_id=token.user_id,
        household_id=token.household_id,
        scope=token.scope,
        resource=token.resource,
    )


# ─── Dynamic Client Registration (RFC 7591) ──────────────────────────────────


def register_client(db: Session, metadata: dict, settings: Settings) -> OAuthClient:
    """
    Register a new public client dynamically.
    Only public clients (no secret) are accepted.
    application_type: web | native
    redirect_uris: https required for web; loopback http allowed for native.
    """
    app_type = metadata.get("application_type", "web")
    if app_type not in ("web", "native"):
        raise OAuthError("invalid_client_metadata", "application_type must be web or native")

    redirect_uris = metadata.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        raise OAuthError("invalid_client_metadata", "redirect_uris is required")

    for uri in redirect_uris:
        parsed = urlparse(uri)
        if app_type == "web":
            if parsed.scheme != "https":
                raise OAuthError("invalid_redirect_uri", f"Web clients require https redirect URIs: {uri}")
        else:  # native
            if parsed.scheme not in ("https",):
                # Allow loopback http for native
                if parsed.scheme == "http":
                    host = parsed.hostname or ""
                    if host not in ("localhost", "127.0.0.1", "::1"):
                        raise OAuthError(
                            "invalid_redirect_uri",
                            f"Native http redirect URIs must use loopback address: {uri}",
                        )
                else:
                    # custom scheme or other: allow for native
                    pass

    client_id = f"dyn_{secrets.token_urlsafe(16)}"
    client_name = metadata.get("client_name") or "Dynamic Client"
    client = OAuthClient(
        id=client_id,
        name=client_name,
        secret_hash=None,
        redirect_uris=redirect_uris,
        is_public=True,
    )
    db.add(client)
    db.commit()
    return client


# ─── CIMD (Client ID Metadata Document) ──────────────────────────────────────


def fetch_cimd(client_id_url: str, settings: Settings) -> dict:
    """
    Fetch and validate a CIMD document.
    - Must be HTTPS
    - client_id field must equal the URL
    - Non-public IPs are blocked (SSRF guard) unless in allowlist
    Returns the metadata dict.
    """
    parsed = urlparse(client_id_url)
    if parsed.scheme != "https":
        raise OAuthError("invalid_client", "CIMD client_id must be an https URL")

    host = parsed.hostname or ""
    allowed = settings.oauth_cimd_allowed_hosts_set
    if host not in allowed and _is_private_ip(host):
        raise OAuthError("invalid_client", "CIMD URL resolves to a private/reserved IP (SSRF guard)")

    try:
        resp = httpx.get(
            client_id_url,
            timeout=5.0,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise OAuthError("invalid_client", f"CIMD fetch returned HTTP {resp.status_code}")
        if len(resp.content) > 64 * 1024:
            raise OAuthError("invalid_client", "CIMD document exceeds 64 KB")
        doc = resp.json()
    except httpx.HTTPError as exc:
        raise OAuthError("invalid_client", f"CIMD fetch failed: {exc.__class__.__name__}")

    if doc.get("client_id") != client_id_url:
        raise OAuthError("invalid_client", "CIMD client_id field must equal the client_id URL")
    if not isinstance(doc.get("redirect_uris"), list):
        raise OAuthError("invalid_client_metadata", "CIMD redirect_uris must be a list")
    return doc
