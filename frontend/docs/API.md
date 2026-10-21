# HomePilot — API Contract (frontend ⇄ FastAPI)

Every endpoint below is called by this frontend. Base URL is set by `NEXT_PUBLIC_API_BASE_URL`. All paths are prefixed `/api/v1`. JSON in, JSON out, timestamps are ISO‑8601 UTC (`2026-10-21T08:30:00Z`).

## 1. Conventions

**Auth transport.** Browser sessions use cookies set by the backend:
- `access_token` — HttpOnly, Secure, SameSite=Lax, short‑lived (about 15 min)
- `refresh_token` — HttpOnly, Secure, SameSite=Lax, `Path=/api/v1/auth`, rotated on every refresh
- `csrf_token` — **readable by JS** (not HttpOnly). The frontend copies it into the `X-CSRF-Token` header on every non‑GET request. Backend must reject mismatches with `403`.

**CORS.** Allow only the configured frontend origin, with `allow_credentials=True`, and allow header `X-CSRF-Token`.

**401 handling.** On `401` from any non‑auth route the frontend calls `POST /auth/refresh` once, then retries the original request. If that fails the user is sent to `/login`.

**Error shape (all non‑2xx):**
```json
{ "error": { "code": "validation_error", "message": "Human readable text" } }
```
Suggested codes: `validation_error` (422), `invalid_credentials` (401), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429), `server_error` (500). Never put stack traces or secrets in `message`; the frontend displays it verbatim.

**Authorization.** Every route that touches a household, device or reading must check the caller's membership in that household server‑side. Return `404` (not `403`) for resources in households the user doesn't belong to, so IDs can't be probed.

**Roles.** `owner` (everything) · `admin` (devices, tariff, settings) · `member` (read data, use assistant). The UI hides admin controls from members, but the backend is the enforcer.

---

## 2. Auth

### POST `/auth/register`  (public)
Creates the user and a default household, sets session cookies.
Request: `{ "name": "Asha", "email": "asha@example.com", "password": "min 10 chars" }`
Response `201`: `Me` object (see `/users/me`). Errors: `409` email taken, `422` validation.
Work: validate, Argon2id hash, create `users` row, create household named e.g. "Asha's Home" with the user as `owner`, create refresh session, set cookies.

### POST `/auth/login`  (public, rate‑limited)
Request: `{ "email", "password" }` → `200` `Me`, cookies set.
Errors: `401 invalid_credentials` (same message for unknown email and wrong password), `429` after repeated failures.

### POST `/auth/refresh`  (refresh cookie)
Rotates refresh token, issues new access token and CSRF cookie. `200 {}` or `401`. Reuse of an old refresh token should revoke that whole session family.

### POST `/auth/logout`
Revokes the current refresh session, clears cookies. `204`.

### POST `/auth/logout-all`
Revokes every refresh session of the user. `204`. (Used by "Log out of all devices".)

---

## 3. Users

### GET `/users/me`
```json
{
  "id": 1, "name": "Asha", "email": "asha@example.com",
  "memberships": [ { "household_id": 7, "household_name": "Asha's Home", "role": "owner" } ]
}
```
The frontend uses `memberships[0]` as the active household.

### PATCH `/users/me`
Request: `{ "name": "New name" }` → `200` `Me`. Email changes are not exposed in the UI.

---

## 4. Dashboard (single aggregated call)

### GET `/households/{household_id}/dashboard?days=7`
`days` ∈ {7, 30, 90}; controls the `daily` series. Polled every 30 s.

```json
{
  "as_of": "2026-10-21T08:30:00Z",
  "last_reading_at": "2026-10-21T08:29:30Z",
  "freshness": "live",
  "current_power_w": 1840.0,
  "today_kwh": 6.42,
  "yesterday_kwh": 9.10,
  "cost": {
    "today": 41.20, "month_to_date": 1260.75, "currency": "INR",
    "is_estimate": true, "basis": "flat rate 6.50/kWh + fixed charge + 5% tax"
  },
  "month": { "kwh_this_period": 182.4, "kwh_previous_period": 171.0 },
  "devices": [
    { "device_id": 3, "name": "Living room AC", "kwh_today": 3.1, "share_pct": 48.3,
      "is_simulated": false, "status": "active", "last_seen_at": "2026-10-21T08:29:30Z" }
  ],
  "daily": [ { "date": "2026-10-15", "kwh": 8.7 } ],
  "opportunities": [
    { "title": "AC runs overnight", "detail": "Living room AC averaged 410 W between 02:00–05:00 on 5 of 7 nights.",
      "estimated_monthly_saving_kwh": 14.2 }
  ],
  "has_simulated_data": true
}
```
Rules:
- `freshness`: `live` ≤ 5 min since last reading, `recent` ≤ 1 h, `stale` older, `none` no readings.
- `devices` sorted by `kwh_today` descending; `share_pct` sums to ≈100 over devices with usage.
- `month.kwh_previous_period` is the same elapsed span of the previous month (like‑for‑like).
- `current_power_w` is `null` when there is no recent data. `opportunities` may be `[]`; `estimated_monthly_saving_kwh` may be `null`. Only return opportunities derived from real readings.
- If no tariff exists, return `cost` values of `0` with `basis: "No tariff configured"`.
- Costs are estimates; `is_estimate` is always `true`.
- All sums computed from the caller's household only.

---

## 5. Devices

Device object:
```json
{ "id": 3, "household_id": 7, "name": "Living room AC",
  "source": "simulated | smart_plug | home_assistant | mqtt | local_api",
  "status": "active | disabled", "last_seen_at": "…|null", "created_at": "…" }
```

| Method & path | Role | Purpose |
|---|---|---|
| GET `/devices?household_id=7` | any member | List devices → `{ "items": [Device], "next_cursor": null }` |
| POST `/devices` | owner/admin | Body `{ "household_id", "name", "source" }` → `201` Device **plus** `ingestion_secret` (plaintext, returned only this once; store only a hash) |
| PATCH `/devices/{id}` | owner/admin | Body any of `{ "name", "status" }` → `200` Device |
| POST `/devices/{id}/rotate-secret` | owner/admin | Invalidates old secret → `{ "ingestion_secret": "…" }` |
| DELETE `/devices/{id}` | owner/admin | Remove device (soft delete recommended) → `204` |

`last_seen_at` = latest accepted reading ingestion for that device.

---

## 6. Readings

### GET `/readings?device_id=3&limit=10&cursor=…`  (any member)
Newest first, cursor pagination. Device must belong to a household the caller is in.
```json
{ "items": [ { "id": 991, "device_id": 3, "measured_at": "…", "ingested_at": "…",
               "power_w": 1210.0, "energy_kwh": 0.0168, "interval_seconds": 50, "is_simulated": false } ],
  "next_cursor": "opaque|null" }
```
`limit` 1–100, default 20.

### POST `/readings`  (device authentication — NOT called by this frontend)
Documented here because the dashboard depends on it. Auth via device credential (e.g. `Authorization: Bearer <ingestion_secret>` or `X-Device-Id` + secret). Body:
```json
{ "device_id": 3, "measured_at": "2026-10-21T08:29:30Z", "power_w": 1210.0,
  "energy_kwh": 0.0168, "interval_seconds": 50, "idempotency_key": "uuid-or-hash" }
```
Validate: timestamp not meaningfully in the future, non‑negative values, plausible `energy_kwh ≈ power_w × interval / 3.6e6`, device active and owned. Duplicate `idempotency_key` → return the original result with `200`, not a second row. Rate‑limit per device.

---

## 7. Tariff

### GET `/households/{household_id}/tariff`  (any member)
```json
{ "id": 1, "name": "Home tariff", "currency": "INR", "type": "flat",
  "flat_rate_per_kwh": 6.5, "fixed_monthly_charge": 120.0, "tax_percent": 5.0,
  "slabs": null, "tou_periods": null }
```
`type`: `flat` | `slab` (`slabs: [{ "up_to_kwh": 100, "rate_per_kwh": 3 }, { "up_to_kwh": null, "rate_per_kwh": 6 }]`) | `time_of_use` (`tou_periods: [{ "start": "22:00", "end": "06:00", "rate_per_kwh": 4 }]`). `404` when none configured (UI shows a notice).

### PUT `/households/{household_id}/tariff`  (owner/admin)
Full replacement with the same shape (minus `id`) → `200` saved tariff. The current UI only edits flat‑rate fields and sends back the full object it loaded.

---

## 8. AI assistant

### POST `/households/{household_id}/chat`  (any member)
Request: `{ "message": "Compare this week with last week.", "conversation_id": "uuid|null" }` (message ≤ 1000 chars)
Response:
```json
{
  "conversation_id": "uuid",
  "reply": "This week you used 61.2 kWh versus 54.8 kWh last week (+11.7%) …",
  "tool_calls": [
    { "name": "compare_energy_usage", "status": "success", "summary": "this week vs last week" }
  ],
  "provider": "ollama"
}
```
Work: backend runs the Ollama (or Bedrock‑with‑fallback) tool loop against the MCP server, with the household id injected **server‑side** from the URL/membership (never from the model). `tool_calls[].status` must reflect real execution outcome (`error` if the tool failed or was rejected). If no data exists or a tool fails, `reply` must say so rather than invent numbers. Non‑streaming. Errors: `404` (not a member), `429`, `503` when no AI provider is reachable.

---

## 9. Endpoint summary

| # | Method | Path | Used by |
|---|---|---|---|
| 1 | POST | /auth/register | Register page |
| 2 | POST | /auth/login | Login page |
| 3 | POST | /auth/refresh | Automatic on 401 |
| 4 | POST | /auth/logout | Settings |
| 5 | POST | /auth/logout-all | Settings |
| 6 | GET | /users/me | App bootstrap, nav |
| 7 | PATCH | /users/me | Settings |
| 8 | GET | /households/{id}/dashboard | Dashboard |
| 9 | GET | /devices | Devices |
| 10 | POST | /devices | Devices |
| 11 | PATCH | /devices/{id} | Devices (rename, enable/disable) |
| 12 | POST | /devices/{id}/rotate-secret | Devices |
| 13 | DELETE | /devices/{id} | Devices |
| 14 | GET | /readings | Devices (recent readings) |
| 15 | GET | /households/{id}/tariff | Settings |
| 16 | PUT | /households/{id}/tariff | Settings |
| 17 | POST | /households/{id}/chat | Assistant |
| — | GET | /health | not used by UI (public liveness) |
| — | POST | /readings | devices/collectors only |

Not yet in the UI (backend may add later): household member invites, role changes, MCP/anomaly views.
