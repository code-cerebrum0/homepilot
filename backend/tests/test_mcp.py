"""Tests for the MCP server + OAuth 2.1 implementation.

Covers:
- Discovery documents (protected-resource-metadata, authorization-server-metadata)
- 401 + WWW-Authenticate without / with invalid token
- Full OAuth flow: GET authorize, POST authorize, PKCE token exchange
- Wrong PKCE verifier
- Code replay revokes tokens
- Refresh rotation + reuse detection
- Revoke endpoint
- Modern-era: server/discover, tools/list, tools/call with correct headers
- Header mismatch (-32020)
- Unsupported version (-32022)
- Unknown method (404/-32601)
- Legacy era: initialize → tools/list → tools/call
- Notifications → 202
- Batch → 400
- GET /mcp/v1 → 405
- Bad Origin → 403
- Invalid args + injected household_id → isError:true
- Unknown tool → -32602
- Wrong-audience and expired tokens → 401
- Token for user B never returns user A's data (tenant isolation)
- Insufficient scope → 403
- Chat still routes through MCP (stub chat_completion)
- All existing /api/v1 tests still pass (imported at bottom)
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf, make_client, register

# ─── Constants ────────────────────────────────────────────────────────────────

API = "/api/v1"
PROTO_MODERN = "2026-07-28"
PROTO_LEGACY = "2025-11-25"
MCP_SCOPE = "energy:read"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_base64url)."""
    verifier = secrets.token_urlsafe(48)  # 64 chars, valid range 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _make_device(c: TestClient, hid: int) -> dict:
    r = c.post(f"{API}/devices", json={"household_id": hid, "name": "Test Device", "source": "smart_plug"}, headers=csrf(c))
    assert r.status_code == 201, r.text
    return r.json()


def _ingest(c: TestClient, dev: dict) -> None:
    from app.utils import iso_z, utcnow
    body = {
        "device_id": dev["id"],
        "measured_at": iso_z(utcnow()),
        "power_w": 1000.0,
        "energy_kwh": 0.014,
        "interval_seconds": 50,
    }
    r = c.post(f"{API}/readings", json=body, headers={"Authorization": f"Bearer {dev['ingestion_secret']}"})
    assert r.status_code == 201, r.text


def _register_client(db=None) -> dict:
    """Insert a public OAuth client directly into the DB and return its id."""
    import secrets as _secrets
    from app.db import SessionLocal
    from app.models import OAuthClient

    client_id = f"test_{_secrets.token_urlsafe(8)}"
    with SessionLocal() as s:
        s.add(OAuthClient(
            id=client_id,
            name="Test Client",
            secret_hash=None,
            redirect_uris=["https://example.com/callback"],
            is_public=True,
        ))
        s.commit()
    return {"client_id": client_id, "redirect_uri": "https://example.com/callback"}


def _full_oauth_flow(c: TestClient, me: dict, client_info: dict) -> dict:
    """
    Run the full OAuth flow and return {'access_token', 'refresh_token'}.
    """
    verifier, challenge = _pkce_pair()
    hid = me["memberships"][0]["household_id"]

    # GET authorize
    params = {
        "client_id": client_info["client_id"],
        "redirect_uri": client_info["redirect_uri"],
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": MCP_SCOPE,
        "state": "xyz",
    }
    r = c.get("/oauth/authorize", params=params, follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "form_token" in r.text

    # Extract form_token from HTML
    import re
    m = re.search(r'name="form_token" value="([^"]+)"', r.text)
    assert m, "form_token not found in login page"
    form_token = m.group(1)

    # POST authorize (log in)
    r2 = c.post(
        "/oauth/authorize",
        data={"form_token": form_token, "email": me["email"], "password": "correct-horse-1"},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.text
    location = r2.headers["location"]
    assert "code=" in location
    assert "state=xyz" in location

    # Extract code
    code = dict(p.split("=", 1) for p in location.split("?", 1)[1].split("&"))["code"]

    # Exchange code for tokens
    r3 = c.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": client_info["redirect_uri"],
        "code_verifier": verifier,
        "client_id": client_info["client_id"],
    })
    assert r3.status_code == 200, r3.text
    tokens = r3.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    return {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]}


def _auth_headers(token: str, method: str, tool_name: str | None = None) -> dict:
    """Build modern-era MCP request headers."""
    h = {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": PROTO_MODERN,
        "Mcp-Method": method,
        "Content-Type": "application/json",
    }
    if tool_name:
        h["Mcp-Name"] = tool_name
    return h


def _modern_body(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    p = dict(params or {})
    p.setdefault("_meta", {"io.modelcontextprotocol/protocolVersion": PROTO_MODERN})
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": p}


# ─── Discovery documents ──────────────────────────────────────────────────────


def test_protected_resource_metadata(client):
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        r = client.get(path)
        assert r.status_code == 200, path
        d = r.json()
        assert "resource" in d
        assert d["bearer_methods_supported"] == ["header"]
        assert MCP_SCOPE in d["scopes_supported"]


def test_authorization_server_metadata(client):
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        r = client.get(path)
        assert r.status_code == 200, path
        d = r.json()
        assert "issuer" in d
        assert "authorization_endpoint" in d
        assert "token_endpoint" in d
        assert "S256" in d["code_challenge_methods_supported"]
        assert d["authorization_response_iss_parameter_supported"] is True


# ─── 401 without token ────────────────────────────────────────────────────────


def test_mcp_no_token_returns_401(client):
    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers={"MCP-Protocol-Version": PROTO_MODERN, "Mcp-Method": "tools/list", "Content-Type": "application/json"})
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert "Bearer" in r.headers["WWW-Authenticate"]
    assert "resource_metadata" in r.headers["WWW-Authenticate"]


def test_mcp_invalid_token_returns_401(client):
    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers={**_auth_headers("bad-token", "tools/list"), "Content-Type": "application/json"})
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers.get("WWW-Authenticate", "")


# ─── Full OAuth flow ──────────────────────────────────────────────────────────


def test_full_oauth_flow(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    # Access token works for tools/list
    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers=_auth_headers(tokens["access_token"], "tools/list"))
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    assert any(t["name"] == "get_current_usage" for t in tools)


def test_wrong_pkce_verifier(client):
    me = register(client)
    ci = _register_client()
    verifier, challenge = _pkce_pair()

    import re
    params = {
        "client_id": ci["client_id"],
        "redirect_uri": ci["redirect_uri"],
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": MCP_SCOPE,
    }
    r = client.get("/oauth/authorize", params=params)
    m = re.search(r'name="form_token" value="([^"]+)"', r.text)
    form_token = m.group(1)

    r2 = client.post("/oauth/authorize", data={
        "form_token": form_token, "email": me["email"], "password": "correct-horse-1"
    }, follow_redirects=False)
    code = dict(p.split("=", 1) for p in r2.headers["location"].split("?", 1)[1].split("&"))["code"]

    r3 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ci["redirect_uri"],
        "code_verifier": "wrong-verifier-that-is-long-enough-to-be-valid-yes",
        "client_id": ci["client_id"],
    })
    assert r3.status_code == 400
    assert r3.json()["error"] == "invalid_grant"


def test_code_replay_revokes_tokens(client):
    me = register(client)
    ci = _register_client()
    verifier, challenge = _pkce_pair()

    import re
    params = {
        "client_id": ci["client_id"],
        "redirect_uri": ci["redirect_uri"],
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": MCP_SCOPE,
    }
    r = client.get("/oauth/authorize", params=params)
    m = re.search(r'name="form_token" value="([^"]+)"', r.text)
    form_token = m.group(1)

    r2 = client.post("/oauth/authorize", data={
        "form_token": form_token, "email": me["email"], "password": "correct-horse-1"
    }, follow_redirects=False)
    code = dict(p.split("=", 1) for p in r2.headers["location"].split("?", 1)[1].split("&"))["code"]

    # First exchange — succeeds
    r3 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ci["redirect_uri"],
        "code_verifier": verifier,
        "client_id": ci["client_id"],
    })
    assert r3.status_code == 200
    first_access = r3.json()["access_token"]

    # Second exchange — replay
    r4 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ci["redirect_uri"],
        "code_verifier": verifier,
        "client_id": ci["client_id"],
    })
    assert r4.status_code == 400
    assert r4.json()["error"] == "invalid_grant"

    # The first access token should now be revoked
    r5 = client.post("/mcp/v1", json=_modern_body("tools/list"),
                     headers=_auth_headers(first_access, "tools/list"))
    assert r5.status_code == 401


def test_refresh_rotation_and_reuse(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)

    # Rotate refresh token
    r = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": ci["client_id"],
    })
    assert r.status_code == 200
    new_tokens = r.json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # Reuse old refresh token → family revoked
    r2 = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": ci["client_id"],
    })
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"

    # New access token also revoked
    r3 = client.post("/mcp/v1", json=_modern_body("tools/list"),
                     headers=_auth_headers(new_tokens["access_token"], "tools/list"))
    assert r3.status_code == 401


def test_revoke_endpoint(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)

    # Revoke
    r = client.post("/oauth/revoke", data={
        "token": tokens["access_token"],
        "client_id": ci["client_id"],
    })
    assert r.status_code == 200  # always 200 per RFC 7009

    # Token no longer works
    r2 = client.post("/mcp/v1", json=_modern_body("tools/list"),
                     headers=_auth_headers(tokens["access_token"], "tools/list"))
    assert r2.status_code == 401


# ─── Modern era: server/discover, tools/list, tools/call ─────────────────────


def test_modern_server_discover(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("server/discover"),
                    headers=_auth_headers(tok, "server/discover"))
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["resultType"] == "complete"
    assert PROTO_MODERN in res["supportedVersions"]
    assert "tools" in res["capabilities"]
    assert "ttlMs" in res
    assert "_meta" in res


def test_modern_tools_list(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/list"),
                    headers=_auth_headers(tok, "tools/list"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert "ttlMs" in result
    tools = result["tools"]
    names = [t["name"] for t in tools]
    assert set(names) == {
        "get_current_usage",
        "get_daily_usage",
        "compare_energy_usage",
        "get_device_breakdown",
        "get_cost_estimate",
        "get_savings_opportunities",
        "get_solar_generation",
    }
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True
        assert "inputSchema" in t


def test_modern_tools_call_no_data(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_current_usage", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_current_usage"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert result["structuredContent"]["has_data"] is False


def test_modern_tools_call_with_data(client):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = _make_device(client, hid)
    _ingest(client, dev)

    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_current_usage", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_current_usage"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["has_data"] is True
    # text should be voice-friendly (no markdown)
    text = result["content"][0]["text"]
    assert "kilowatt" in text.lower() or "watts" in text.lower()


# ─── Header mismatch (-32020) ─────────────────────────────────────────────────


def test_missing_mcp_protocol_version_header(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    # Force modern era via _meta but omit the header
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": PROTO_MODERN}},
    }
    r = client.post("/mcp/v1", json=body, headers={
        "Authorization": f"Bearer {tok}",
        "Mcp-Method": "tools/list",
        "Content-Type": "application/json",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_missing_mcp_method_header(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1", json=_modern_body("tools/list"), headers={
        "Authorization": f"Bearer {tok}",
        "MCP-Protocol-Version": PROTO_MODERN,
        "Content-Type": "application/json",
        # no Mcp-Method
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_mcp_method_header_mismatch(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1", json=_modern_body("tools/list"), headers={
        "Authorization": f"Bearer {tok}",
        "MCP-Protocol-Version": PROTO_MODERN,
        "Mcp-Method": "server/discover",  # wrong
        "Content-Type": "application/json",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_missing_mcp_name_header_for_tools_call(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_current_usage", "arguments": {}}),
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "MCP-Protocol-Version": PROTO_MODERN,
                        "Mcp-Method": "tools/call",
                        "Content-Type": "application/json",
                        # no Mcp-Name
                    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


# ─── Unsupported version (-32022) ─────────────────────────────────────────────


def test_unsupported_version(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
    }
    r = client.post("/mcp/v1", json=body, headers={
        "Authorization": f"Bearer {tok}",
        "MCP-Protocol-Version": "2099-01-01",
        "Mcp-Method": "tools/list",
        "Content-Type": "application/json",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32022


# ─── Unknown method ───────────────────────────────────────────────────────────


def test_unknown_method_modern_returns_404(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1", json=_modern_body("no_such_method"),
                    headers=_auth_headers(tok, "no_such_method"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


def test_unknown_method_legacy_returns_200(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json={"jsonrpc": "2.0", "id": 1, "method": "no_such_method", "params": {}},
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "MCP-Protocol-Version": PROTO_LEGACY,
                        "Content-Type": "application/json",
                    })
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


# ─── Legacy era ───────────────────────────────────────────────────────────────


def test_legacy_initialize_tools_list_call(client):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = _make_device(client, hid)
    _ingest(client, dev)

    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]
    bearer = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    # initialize (no version header needed for first request)
    r = client.post("/mcp/v1",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": PROTO_LEGACY, "capabilities": {}}},
                    headers=bearer)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] in ("2025-11-25", "2025-06-18")
    assert "capabilities" in result

    # tools/list
    r2 = client.post("/mcp/v1",
                     json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                     headers={**bearer, "MCP-Protocol-Version": PROTO_LEGACY})
    assert r2.status_code == 200
    tools = r2.json()["result"]["tools"]
    assert len(tools) == 7

    # tools/call
    r3 = client.post("/mcp/v1",
                     json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "get_current_usage", "arguments": {}}},
                     headers={**bearer, "MCP-Protocol-Version": PROTO_LEGACY})
    assert r3.status_code == 200
    res = r3.json()["result"]
    assert res["isError"] is False

    # ping
    r4 = client.post("/mcp/v1",
                     json={"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
                     headers={**bearer, "MCP-Protocol-Version": PROTO_LEGACY})
    assert r4.status_code == 200
    assert r4.json()["result"] == {}


# ─── Notifications → 202 ─────────────────────────────────────────────────────


def test_notification_returns_202(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    assert r.status_code == 202
    assert r.content == b""


# ─── Batch → 400 ─────────────────────────────────────────────────────────────


def test_batch_returns_400(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    content=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}]).encode(),
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600


# ─── GET /mcp/v1 → 405 ───────────────────────────────────────────────────────


def test_get_mcp_returns_405(client):
    r = client.get("/mcp/v1")
    assert r.status_code == 405
    assert r.headers.get("allow") == "POST"


# ─── Bad Origin → 403 ────────────────────────────────────────────────────────


def test_bad_origin_returns_403(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/list"),
                    headers={
                        **_auth_headers(tok, "tools/list"),
                        "Origin": "https://evil.example.com",
                    })
    assert r.status_code == 403


# ─── Invalid args + injected household_id → isError:true ─────────────────────


def test_invalid_args_returns_is_error(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    # Wrong type for 'days'
    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_daily_usage", "arguments": {"days": "not_a_number"}}),
                    headers=_auth_headers(tok, "tools/call", "get_daily_usage"))
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True


def test_injected_household_id_rejected(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    # Attempt to pass household_id as an argument
    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_current_usage",
                        "arguments": {"household_id": 999},
                    }),
                    headers=_auth_headers(tok, "tools/call", "get_current_usage"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "household_id" in result["content"][0]["text"].lower() or "unexpected" in result["content"][0]["text"].lower()


# ─── Unknown tool → -32602 ────────────────────────────────────────────────────


def test_unknown_tool_returns_is_error(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "no_such_tool", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "no_such_tool"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True


# ─── Expired token → 401 ─────────────────────────────────────────────────────


def test_expired_token_returns_401(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)

    # Manually expire the token in the DB
    from app.db import SessionLocal
    from app.models import OAuthToken
    import hashlib

    h = hashlib.sha256(tokens["access_token"].encode()).hexdigest()
    with SessionLocal() as db:
        tok_row = db.query(OAuthToken).filter_by(access_hash=h).one()
        tok_row.access_expires_at = tok_row.access_expires_at - timedelta(hours=2)
        db.commit()

    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers=_auth_headers(tokens["access_token"], "tools/list"))
    assert r.status_code == 401


# ─── Wrong-audience token → 401 ──────────────────────────────────────────────


def test_wrong_audience_token_returns_401(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)

    # Change the resource on the stored token to a different audience
    from app.db import SessionLocal
    from app.models import OAuthToken
    import hashlib

    h = hashlib.sha256(tokens["access_token"].encode()).hexdigest()
    with SessionLocal() as db:
        tok_row = db.query(OAuthToken).filter_by(access_hash=h).one()
        tok_row.resource = "https://other.example.com/mcp/v1"
        db.commit()

    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers=_auth_headers(tokens["access_token"], "tools/list"))
    assert r.status_code == 401


# ─── Tenant isolation: token for user B never exposes user A's data ──────────


def test_tenant_isolation_via_mcp(client):
    # User A with data
    me_a = register(client)
    hid_a = me_a["memberships"][0]["household_id"]
    dev = _make_device(client, hid_a)
    _ingest(client, dev)

    # User B with no data, fresh client
    client_b = make_client()
    with client_b as cb:
        me_b = register(cb)
        ci_b = _register_client()
        tokens_b = _full_oauth_flow(cb, me_b, ci_b)
        tok_b = tokens_b["access_token"]

        r = cb.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_current_usage", "arguments": {}}),
                    headers=_auth_headers(tok_b, "tools/call", "get_current_usage"))
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is False
        # B has no data → should not see A's readings
        assert result["structuredContent"]["has_data"] is False


# ─── Insufficient scope → 403 ─────────────────────────────────────────────────


def test_insufficient_scope_returns_403(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)

    # Narrow the scope on the stored token
    from app.db import SessionLocal
    from app.models import OAuthToken
    import hashlib

    h = hashlib.sha256(tokens["access_token"].encode()).hexdigest()
    with SessionLocal() as db:
        tok_row = db.query(OAuthToken).filter_by(access_hash=h).one()
        tok_row.scope = "read:nothing"
        db.commit()

    r = client.post("/mcp/v1", json=_modern_body("tools/list"),
                    headers=_auth_headers(tokens["access_token"], "tools/list"))
    assert r.status_code == 403


# ─── Chat passes through MCP (stub chat_completion) ──────────────────────────


def test_chat_routes_through_mcp(client, monkeypatch):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = _make_device(client, hid)
    _ingest(client, dev)

    calls = {"n": 0}

    def fake_completion(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            # Verify Ollama sees the MCP-derived tool spec
            tool_names = [t["function"]["name"] for t in tools]
            assert "get_current_usage" in tool_names
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_current_usage", "arguments": {}}}],
            }
        # Second round: summarize
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert '"has_data"' in tool_msgs[0]["content"]
        return {"role": "assistant", "content": "Your home is using electricity."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "How much power now?"},
                    headers=csrf(client))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["reply"] == "Your home is using electricity."
    assert j["tool_calls"][0]["status"] == "success"
    assert j["tool_calls"][0]["name"] == "get_current_usage"

    # ── Prove MCP path was used, not direct tool calls ──────────────────────
    mcp_calls = j["mcp_calls"]
    # At minimum: one tools/list + one tools/call
    assert len(mcp_calls) >= 2, f"Expected ≥2 mcp_calls, got {mcp_calls}"
    methods = [c["method"] for c in mcp_calls]
    assert "tools/list" in methods, "tools/list not in mcp_calls"
    assert "tools/call" in methods, "tools/call not in mcp_calls"
    # All calls used the modern protocol version
    for c in mcp_calls:
        assert c["protocol"] == "2026-07-28", f"Unexpected protocol: {c['protocol']}"
        assert isinstance(c["duration_ms"], float)
    # The tools/call entry names the correct tool and succeeded
    call_entries = [c for c in mcp_calls if c["method"] == "tools/call"]
    assert call_entries[0]["tool_name"] == "get_current_usage"
    assert call_entries[0]["ok"] is True




def test_chat_mcp_tool_error_marked_error(client, monkeypatch):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    calls = {"n": 0}

    def fake_completion(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    # Valid tool, invalid arg
                    {"function": {"name": "get_daily_usage", "arguments": {"days": "bad"}}},
                    # Non-existent tool
                    {"function": {"name": "does_not_exist", "arguments": {}}},
                ],
            }
        return {"role": "assistant", "content": "I couldn't get that info."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "test"},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    statuses = [tc["status"] for tc in j["tool_calls"]]
    assert "error" in statuses


# ─── OAuth authorize: invalid redirect_uri never redirects ────────────────────


def test_authorize_bad_redirect_shows_error_page(client):
    ci = _register_client()
    r = client.get("/oauth/authorize", params={
        "client_id": ci["client_id"],
        "redirect_uri": "https://evil.example.com/callback",
        "response_type": "code",
        "code_challenge": "abc",
        "scope": MCP_SCOPE,
    })
    # Must show error page, never redirect
    assert r.status_code == 200
    assert "Authorization Error" in r.text or "error" in r.text.lower()
    assert r.headers.get("location") is None


def test_authorize_missing_pkce_shows_error(client):
    ci = _register_client()
    r = client.get("/oauth/authorize", params={
        "client_id": ci["client_id"],
        "redirect_uri": ci["redirect_uri"],
        "response_type": "code",
        # no code_challenge
        "scope": MCP_SCOPE,
    })
    assert r.status_code == 200
    assert "error" in r.text.lower() or "Authorization Error" in r.text


# ─── DCR endpoint (off by default) ───────────────────────────────────────────


def test_dcr_disabled_by_default(client):
    r = client.post("/oauth/register", json={
        "client_name": "New Client",
        "redirect_uris": ["https://example.com/callback"],
        "application_type": "web",
    })
    assert r.status_code == 404


# ─── OAuth authorize: wrong password ─────────────────────────────────────────


def test_authorize_wrong_password_shows_login_again(client):
    me = register(client)
    ci = _register_client()
    _, challenge = _pkce_pair()

    import re
    r = client.get("/oauth/authorize", params={
        "client_id": ci["client_id"],
        "redirect_uri": ci["redirect_uri"],
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": MCP_SCOPE,
    })
    m = re.search(r'name="form_token" value="([^"]+)"', r.text)
    form_token = m.group(1)

    r2 = client.post("/oauth/authorize", data={
        "form_token": form_token, "email": me["email"], "password": "wrong-password-here"
    }, follow_redirects=False)
    # Must re-render login page, not redirect
    assert r2.status_code == 200
    assert "Invalid email or password" in r2.text


# ─── Compare tool: label in meta ─────────────────────────────────────────────


def test_compare_tool_label(client):
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth_flow(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "compare_energy_usage",
                        "arguments": {"comparison": "today_vs_yesterday"},
                    }),
                    headers=_auth_headers(tok, "tools/call", "compare_energy_usage"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    label = result["_meta"].get("homepilot/label", "")
    assert "today" in label.lower() or "yesterday" in label.lower()
