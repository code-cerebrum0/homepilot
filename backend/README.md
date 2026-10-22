# HomePilot API

FastAPI backend implementing the HomePilot frontend ⇄ API contract (all 17 endpoints + `/health` + device `POST /readings`). 

**Stack:** Python 3.10+ · FastAPI · SQLAlchemy 2 · MySQL (PyMySQL) · Argon2id · JWT access cookies · Ollama for the assistant.

## Run

```bash
# 1. MySQL (8.x recommended)
mysql -u root -p -e "CREATE DATABASE homepilot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
                     CREATE USER 'homepilot'@'%' IDENTIFIED BY 'change-me';
                     GRANT ALL ON homepilot.* TO 'homepilot'@'%';"

# 2. Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env        # set DATABASE_URL and SECRET_KEY (min 32 chars)

# 4. Start (tables are created on startup when AUTO_CREATE_TABLES=true)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive docs: `http://localhost:8000/docs`. Frontend: set `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` and `FRONTEND_ORIGIN` here to the frontend's origin.

Assistant: `ollama serve` and `ollama pull <model>` (default model `llama3.1`; use one that supports tool calling). Change with `OLLAMA_MODEL`.

Tests (use SQLite, no MySQL needed): `pip install -r requirements-dev.txt && pytest`

## Layout

```
app/main.py            app, CORS, CSRF middleware, router wiring
app/routers/           auth, users, dashboard, devices, readings, tariff, chat
app/services/          dashboard aggregation, tariff/cost maths, opportunities, simulator
app/ai/                Ollama client, tool definitions, tool loop
app/deps.py            auth + household membership/role checks
```

## Behaviour worth knowing

- **Cookies/CSRF:** `access_token` (HttpOnly, 15 min), `refresh_token` (HttpOnly, `Path=/api/v1/auth`, rotated each refresh; reusing an old one revokes the whole session family), `csrf_token` (JS-readable). Non-GET requests that carry a session cookie must send a matching `X-CSRF-Token`, else `403`. Login, register and `POST /readings` are exempt.
- **Cookie `Secure`:** `COOKIE_SECURE=true` by default as specified. Set `false` only if you serve the API over plain HTTP on a non-localhost host.
- **Authorization:** non-members get `404`; members lacking the role get `403`. Owner/admin: devices, tariff. Roles are enforced server-side.
- **Ingestion:** `POST /readings` with `Authorization: Bearer <ingestion_secret>` (optionally `X-Device-Id`). Secrets are stored as SHA-256 hashes. Duplicate `idempotency_key` returns the original row with `200`. Energy must match `power_w × interval / 3.6e6` within 25 % (or 0.0005 kWh).
- **Time zone:** "today", daily buckets and the billing month use `APP_TIMEZONE` (default `Asia/Kolkata`). Timestamps in responses are UTC with `Z`.
- **Cost:** `today` = marginal energy charge for today + 1/days-in-month of the fixed charge, with tax; `month_to_date` = energy + full fixed charge, with tax. Slab tariffs are applied to cumulative monthly kWh. For time-of-use, hours outside every period use `flat_rate_per_kwh` (0 if unset).
- **Opportunities:** one rule — a device drawing ≥ 50 W between 02:00–05:00 on ≥ 3 of the last 7 nights. Only non-simulated readings are used.
- **Simulated devices:** devices with `source: "simulated"` get readings only if `SIMULATOR_ENABLED=true` (a background task writing one reading per `SIMULATOR_INTERVAL_SECONDS`).
- **Assistant:** the household id is injected server-side; tools reject any argument not in their schema (including `household_id`), and `tool_calls[].status` reflects the real outcome. Tools run in-process (`app/ai/tools.py`), not over an MCP transport — the contract didn't include an MCP server. Replace `_run_tool` if you add one. Unreachable Ollama → `503`.
- **Rate limits** (login failures, per-device ingestion, chat) are in-memory per process; use a shared store if you run multiple workers.
- **Schema changes:** tables are created with `create_all`; there are no Alembic migrations yet.
