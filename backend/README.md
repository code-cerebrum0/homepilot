# HomePilot API

FastAPI backend implementing the HomePilot frontend ⇄ API contract (all 17 endpoints + `/health` + device `POST /readings`), plus a spec-compliant **MCP server over Streamable HTTP** with **OAuth 2.1**.

**Stack:** Python 3.10+ · FastAPI · SQLAlchemy 2 · MySQL (PyMySQL) · Argon2id · JWT access cookies · Ollama for the assistant · no `mcp` SDK (protocol implemented directly).

---

## Run

```bash
# 1. MySQL (8.x recommended)
mysql -u root -p -e "
  CREATE DATABASE homepilot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER 'homepilot'@'%' IDENTIFIED BY 'change-me';
  GRANT ALL ON homepilot.* TO 'homepilot'@'%';"

# 2. Install
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env   # set DATABASE_URL, SECRET_KEY, PUBLIC_BASE_URL

# 4. Start (tables are created on startup when AUTO_CREATE_TABLES=true)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive docs: `http://localhost:8000/docs`  
Assistant: `ollama serve` and `ollama pull llama3.1` (needs tool-call support). Change with `OLLAMA_MODEL`.  
Tests (SQLite, no MySQL needed): `pip install -r requirements-dev.txt && pytest`

---

## Layout

```
app/main.py              app, CORS, CSRF middleware, router wiring
app/routers/             auth, users, dashboard, devices, readings, tariff, chat
app/routers/mcp.py       POST /mcp/v1  (Streamable HTTP, dual-era)
app/routers/oauth.py     /oauth/* + /.well-known/* discovery docs
app/mcp/tools.py         MCP tool definitions (moved from app/ai/tools.py)
app/mcp/dispatch.py      stateless JSON-RPC dispatcher, dual-era logic
app/services/oauth.py    OAuth 2.1 business logic (codes, tokens, PKCE, DCR)
app/services/            dashboard, tariff/cost, opportunities, simulator
app/ai/                  Ollama client + chat loop (uses in-process MCP client)
app/deps.py              cookie-auth + household membership/role checks
scripts/create_oauth_client.py  register a client from the CLI
```

---

## MCP Server

### What is exposed

Six read-only energy tools, all requiring scope `energy:read`:

| Tool | Args | Description |
|---|---|---|
| `get_current_usage` | — | Current watts + today's kWh |
| `get_daily_usage` | `days` 1–90 | Per-day kWh series |
| `compare_energy_usage` | `comparison` enum | Period-over-period diff |
| `get_device_breakdown` | `period` enum | Per-device kWh + share |
| `get_cost_estimate` | — | Today + MTD cost (estimate) |
| `get_savings_opportunities` | — | Detected waste |

The household is **bound to the access token** — tools have no `household_id` parameter and will reject it as an error if supplied.

### Endpoint

```
POST /mcp/v1
```

Dual-era, stateless, JSON-only (no SSE). Supports:

- **Modern era** `2026-07-28` — `server/discover`, `tools/list`, `tools/call`
- **Legacy era** `2025-11-25` / `2025-06-18` — `initialize`, `ping`, `tools/list`, `tools/call`

### Setting `PUBLIC_BASE_URL`

This **must be your real public HTTPS URL** in production — it is used as the OAuth issuer, the MCP resource audience, and in all discovery documents.

```env
PUBLIC_BASE_URL=https://homepilot.example.com
```

For local development `http://localhost:8000` works, but OAuth clients and Alexa+ require a public HTTPS URL.

### Register an OAuth client

```bash
# Public client (SPA / mobile / MCP Inspector)
python scripts/create_oauth_client.py --name "MCP Inspector" --redirect-uri https://inspector.modelcontextprotocol.io/callback --public

# Confidential client (server-side app) — secret printed once
python scripts/create_oauth_client.py --name "My Server App" --redirect-uri https://myapp.example.com/callback
```

### Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

1. Set **Transport** → `Streamable HTTP`
2. Set **URL** → `https://homepilot.example.com/mcp`
3. Set **OAuth** → enter the `client_id` from the registration step above, redirect URI `https://inspector.modelcontextprotocol.io/callback`
4. Click **Connect** — the Inspector will open `/oauth/authorize` in a browser; sign in with your HomePilot credentials.
5. After consent, tools appear in the **Tools** tab.

### Discovery documents

| URL | RFC | Description |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 | Points to auth server + scope |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Auth server metadata |
| `GET /.well-known/openid-configuration` | OIDC alias | Same as above |

### Dynamic Client Registration (off by default)

Set `OAUTH_ENABLE_DCR=true` to enable `POST /oauth/register` (RFC 7591). Public clients only; web clients require `https` redirect URIs; native clients may use loopback `http`.

---

## Behaviour worth knowing

- **Cookies/CSRF:** `access_token` (HttpOnly, 15 min), `refresh_token` (HttpOnly, rotated each refresh; reusing a rotated token revokes the whole family), `csrf_token` (JS-readable). Non-GET requests with a session cookie need `X-CSRF-Token`. The `/mcp`, `/oauth/*` and `/.well-known/*` paths are **exempt** from CSRF (they use Bearer tokens or a signed form token).
- **MCP auth:** Bearer token (SHA-256 hashed, stored in `oauth_tokens`). Tokens are audience-bound to `<PUBLIC_BASE_URL>/mcp`; wrong audience → 401. On every request membership is re-confirmed.
- **OAuth PKCE:** S256 only, verifier 43–128 chars. Code TTL 5 min, single use; replay revokes the entire token family.
- **Refresh rotation:** every `refresh_token` grant issues a new pair and revokes the old one. Replaying a used refresh token revokes the whole family.
- **Rate limits:** login failures, device ingestion, chat, MCP calls, and DCR registrations are all in-memory per process. Use a shared store (e.g. Redis) for multi-worker deployments.
- **Schema changes:** tables are created with `create_all`; there are no Alembic migrations yet.

---

## What is NOT implemented

- **SSE streaming / sessions** — the endpoint is stateless JSON-only; no `Mcp-Session-Id` is issued.
- **Server-initiated requests** — the server never sends unsolicited messages to clients.
- **Multi-household selection at consent** — the authorization page always binds the token to the user's first household membership. A future consent UI could let the user choose.
- **Client ID Metadata Documents (CIMD)** — available but off by default (`OAUTH_ENABLE_CIMD=true`). Note: CIMD is deprecated in the 2026-07-28 spec; pre-registered clients are the primary path.
- **Dynamic Client Registration** — available but off by default (`OAUTH_ENABLE_DCR=true`).
- **Write tools** — all six tools are read-only (`energy:read` scope). No energy control, device management, or tariff editing via MCP.
