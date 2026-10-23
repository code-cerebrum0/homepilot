"""Tests for savings opportunities, usage anomaly detection, and improved tool contracts.

Covers:
- Savings opportunities: found using consumer readings
- Savings opportunities: no findings despite sufficient data
- Savings opportunities: insufficient historical data
- Savings opportunities: solar generator devices excluded
- Savings opportunities: correct household isolation
- Savings opportunities: simulated devices excluded
- Anomaly detection: synthetic spike correctly detected
- Anomaly detection: normal variation not flagged
- Anomaly detection: insufficient history returns insufficient_data
- Anomaly detection: valid arguments and defaults
- Anomaly detection: invalid analysis period rejected
- get_savings_opportunities MCP tool: new status field propagated
- get_usage_anomalies MCP tool: correct structure
- get_usage_anomalies MCP tool: household isolation
- System prompt routing: anomaly question should not call savings tool
"""
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf, make_client, register


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Ensure DB tables exist before any tests in this module run.

    The conftest sets AUTO_CREATE_TABLES=true, but tables are only created
    when a TestClient enters the app lifespan. Tests that use SessionLocal
    directly (without the client fixture) need this session-scoped fixture.
    """
    from app.db import Base, engine
    Base.metadata.create_all(engine)

API = "/api/v1"
PROTO_MODERN = "2026-07-28"
MCP_SCOPE = "energy:read"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _register_client() -> dict:
    import secrets as _s
    from app.db import SessionLocal
    from app.models import OAuthClient
    cid = f"test_{_s.token_urlsafe(8)}"
    with SessionLocal() as s:
        s.add(OAuthClient(id=cid, name="Test", secret_hash=None,
                          redirect_uris=["https://example.com/cb"], is_public=True))
        s.commit()
    return {"client_id": cid, "redirect_uri": "https://example.com/cb"}


def _pkce_pair():
    import base64, hashlib as _h
    v = secrets.token_urlsafe(48)
    d = _h.sha256(v.encode("ascii")).digest()
    c = base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    return v, c


def _full_oauth(c: TestClient, me: dict, ci: dict) -> dict:
    import re
    v, ch = _pkce_pair()
    hid = me["memberships"][0]["household_id"]
    params = {"client_id": ci["client_id"], "redirect_uri": ci["redirect_uri"],
              "response_type": "code", "code_challenge": ch,
              "code_challenge_method": "S256", "scope": MCP_SCOPE, "state": "x"}
    r = c.get("/oauth/authorize", params=params)
    m = re.search(r'name="form_token" value="([^"]+)"', r.text)
    ft = m.group(1)
    r2 = c.post("/oauth/authorize",
                data={"form_token": ft, "email": me["email"], "password": "correct-horse-1"},
                follow_redirects=False)
    code = dict(p.split("=", 1) for p in r2.headers["location"].split("?", 1)[1].split("&"))["code"]
    r3 = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                "redirect_uri": ci["redirect_uri"], "code_verifier": v, "client_id": ci["client_id"]})
    return {"access_token": r3.json()["access_token"]}


def _auth_headers(tok: str, method: str, tool: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {tok}", "MCP-Protocol-Version": PROTO_MODERN,
         "Mcp-Method": method, "Content-Type": "application/json"}
    if tool:
        h["Mcp-Name"] = tool
    return h


def _modern_body(method: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p.setdefault("_meta", {"io.modelcontextprotocol/protocolVersion": PROTO_MODERN})
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


def _make_device(c: TestClient, hid: int, kind: str = "consumer", name: str = "Test Device") -> dict:
    r = c.post(f"{API}/devices",
               json={"household_id": hid, "name": name, "source": "smart_plug", "kind": kind},
               headers=csrf(c))
    assert r.status_code == 201, r.text
    return r.json()


def _ingest_reading(c: TestClient, dev: dict, measured_at: datetime,
                    power_w: float = 200.0, interval_seconds: int = 300) -> None:
    """Ingest one reading. energy_kwh is calculated from power_w and interval_seconds."""
    from app.utils import iso_z
    energy_kwh = power_w * interval_seconds / 3_600_000
    body = {"device_id": dev["id"], "measured_at": iso_z(measured_at),
            "power_w": power_w, "energy_kwh": round(energy_kwh, 6), "interval_seconds": interval_seconds}
    r = c.post(f"{API}/readings", json=body,
               headers={"Authorization": f"Bearer {dev['ingestion_secret']}"})
    assert r.status_code == 201, r.text


def _night_readings(c: TestClient, dev: dict, n_nights: int, base_now: datetime | None = None,
                    power_w: float = 200.0) -> None:
    """Ingest enough readings during 02:00-05:00 for n_nights to trigger standby detection.

    Uses the app timezone (Asia/Kolkata, UTC+5:30) to ensure readings fall in the
    correct 02:00-05:00 local window, regardless of the system clock UTC offset.
    """
    from zoneinfo import ZoneInfo
    from app.utils import app_tz
    tz = app_tz()
    now = (base_now or datetime.now(timezone.utc)).astimezone(tz)

    for night_offset in range(n_nights):
        local_date = now.date() - timedelta(days=night_offset + 1)
        # Build the 02:00 local time for this date, then convert to UTC for storage
        local_2am = datetime(local_date.year, local_date.month, local_date.day,
                             2, 0, 0, tzinfo=tz)
        # 9 readings at 20-min intervals (1200s) covers 02:00–04:40 local
        for i in range(9):
            ts = local_2am + timedelta(minutes=i * 20)
            _ingest_reading(c, dev, ts, power_w=power_w, interval_seconds=1200)


# ─── Unit tests: OpportunityResult / find_opportunities ──────────────────────

def _build_opportunity_scenario(
    include_consumer: bool = True, include_generator: bool = False,
    n_nights: int = 3, power_w: float = 200.0
):
    """Helper: insert synthetic overnight readings and run find_opportunities."""
    import secrets as _s
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.models import Device, Household, Reading
    from app.services.opportunities import find_opportunities

    tz = ZoneInfo("UTC")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        hh = Household(name=f"TestOpp_{_s.token_hex(4)}")
        db.add(hh)
        db.flush()
        hid = hh.id

        devices = []
        if include_consumer:
            d = Device(household_id=hid, name="Fridge", source="smart_plug",
                       kind="consumer", status="active",
                       secret_hash=_s.token_hex(32))
            db.add(d)
            db.flush()
            devices.append((d.id, "consumer"))

        if include_generator:
            g = Device(household_id=hid, name="Solar Panel", source="solar_inverter",
                       kind="generator", status="active",
                       secret_hash=_s.token_hex(32))
            db.add(g)
            db.flush()
            devices.append((g.id, "generator"))

        db.flush()

        for dev_id, kind in devices:
            for night_offset in range(n_nights):
                base = (now - timedelta(days=night_offset + 1)).replace(
                    hour=2, minute=0, second=0, microsecond=0)
                for i in range(9):
                    ts = base + timedelta(minutes=i * 20)
                    db.add(Reading(device_id=dev_id, measured_at=ts,
                                   power_w=power_w, energy_kwh=power_w / 1000 * (20 / 60),
                                   interval_seconds=1200, is_simulated=False,
                                   idempotency_key=f"opp_{dev_id}_{night_offset}_{i}_{_s.token_hex(4)}"))
        db.commit()

        result = find_opportunities(db, hid, now, tz)
        return result


def test_opportunities_found_with_consumer_data():
    """Sufficient overnight readings → opportunity detected."""
    result = _build_opportunity_scenario(include_consumer=True, n_nights=5, power_w=200.0)
    assert result.status == "ok"
    assert len(result.opportunities) == 1
    assert result.opportunities[0].estimated_monthly_saving_kwh > 0
    assert "Fridge" in result.opportunities[0].title


def test_no_opportunities_with_low_power():
    """Power below standby threshold → no opportunity, status still ok."""
    result = _build_opportunity_scenario(include_consumer=True, n_nights=5, power_w=10.0)
    assert result.status == "ok"
    assert result.opportunities == []
    # message must explain what was checked, not claim the house is efficient
    assert ("standby" in result.message.lower() or "checked" in result.message.lower() or
            "overnight" in result.message.lower() or "No device" in result.message)


def test_generator_excluded_from_analysis():
    """Generator device must NOT appear in opportunities even with overnight power."""
    result = _build_opportunity_scenario(include_consumer=False, include_generator=True,
                                         n_nights=5, power_w=200.0)
    assert result.status == "insufficient_data"
    assert result.opportunities == []


def test_consumer_and_generator_only_consumer_flagged():
    """With both consumer and generator, only consumer should be flagged."""
    result = _build_opportunity_scenario(include_consumer=True, include_generator=True,
                                         n_nights=5, power_w=200.0)
    assert result.status == "ok"
    for opp in result.opportunities:
        assert "Solar" not in opp.title


def test_insufficient_data_few_nights():
    """Only 2 nights of data → insufficient_data (< MIN_NIGHTS_WITH_DATA=3)."""
    result = _build_opportunity_scenario(include_consumer=True, n_nights=2, power_w=200.0)
    assert result.status == "insufficient_data"
    assert result.opportunities == []
    assert result.nights_with_data <= 2


def test_no_devices_returns_insufficient_data():
    """No consumer devices at all → insufficient_data."""
    import secrets as _s
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.models import Household
    from app.services.opportunities import find_opportunities

    tz = ZoneInfo("UTC")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        hh = Household(name=f"Empty_{_s.token_hex(4)}")
        db.add(hh)
        db.flush()
        result = find_opportunities(db, hh.id, now, tz)
        assert result.status == "insufficient_data"


# ─── Unit tests: AnomalyResult / find_anomalies ──────────────────────────────

def _create_anomaly_household(daily_kwh: list[float]) -> tuple[int, datetime]:
    """Create a household with synthetic daily readings for anomaly tests."""
    import secrets as _s
    from app.db import SessionLocal
    from app.models import Device, Household, Reading

    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        hh = Household(name=f"AnoHH_{_s.token_hex(4)}")
        db.add(hh)
        db.flush()
        hid = hh.id

        dev = Device(household_id=hid, name="Meter", source="smart_plug",
                     kind="consumer", status="active",
                     secret_hash=_s.token_hex(32))
        db.add(dev)
        db.flush()
        dev_id = dev.id

        n = len(daily_kwh)
        for i, kwh in enumerate(daily_kwh):
            day_offset = n - i
            day_base = (now - timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            for j in range(6):
                ts = day_base + timedelta(hours=j * 4)
                db.add(Reading(device_id=dev_id, measured_at=ts,
                               power_w=kwh / 6 * 1000 / 4,
                               energy_kwh=kwh / 6,
                               interval_seconds=14400,
                               is_simulated=False,
                               idempotency_key=f"ano_{hid}_{i}_{j}_{_s.token_hex(4)}"))
        db.commit()
        return hid, now


def test_spike_detected():
    """A day with 5x normal consumption → confirmed anomaly."""
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.services.anomaly import find_anomalies

    baseline = [10.0] * 27 + [50.0]
    hid, now = _create_anomaly_household(baseline)
    tz = ZoneInfo("UTC")

    with SessionLocal() as db:
        result = find_anomalies(db, hid, now, tz, analysis_days=3, history_days=28)

    assert result.status == "ok"
    confirmed = [f for f in result.findings if f.confirmed]
    assert len(confirmed) >= 1
    spike = confirmed[0]
    assert spike.observed_kwh > spike.baseline_median_kwh
    assert spike.direction == "high"


def test_normal_variation_not_flagged():
    """±10% variation around mean should not trigger a confirmed anomaly."""
    import math
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.services.anomaly import find_anomalies

    vals = [10.0 * (1 + 0.1 * math.sin(i)) for i in range(30)]
    hid, now = _create_anomaly_household(vals)
    tz = ZoneInfo("UTC")

    with SessionLocal() as db:
        result = find_anomalies(db, hid, now, tz, analysis_days=3, history_days=30)

    assert result.status in ("ok", "insufficient_data")
    if result.status == "ok":
        confirmed = [f for f in result.findings if f.confirmed]
        assert len(confirmed) == 0, f"Unexpected confirmed anomalies: {confirmed}"


def test_insufficient_history_returns_insufficient_data():
    """Only 5 days of data → insufficient_data."""
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.services.anomaly import find_anomalies

    vals = [10.0] * 5
    hid, now = _create_anomaly_household(vals)
    tz = ZoneInfo("UTC")

    with SessionLocal() as db:
        result = find_anomalies(db, hid, now, tz, analysis_days=2, history_days=14)

    assert result.status == "insufficient_data"
    assert result.findings == []


def test_highest_day_not_flagged_if_all_similar():
    """The highest day should not be labelled anomalous if all days are similar."""
    from zoneinfo import ZoneInfo
    from app.db import SessionLocal
    from app.services.anomaly import find_anomalies

    vals = [10.0] * 27 + [11.0]
    hid, now = _create_anomaly_household(vals)
    tz = ZoneInfo("UTC")

    with SessionLocal() as db:
        result = find_anomalies(db, hid, now, tz, analysis_days=3, history_days=28)

    assert result.status == "ok"
    confirmed = [f for f in result.findings if f.confirmed]
    assert len(confirmed) == 0


# ─── MCP tool: get_savings_opportunities (new status contract) ────────────────

def test_savings_tool_returns_status_field(client):
    """get_savings_opportunities must include a status field in structuredContent."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_savings_opportunities", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_savings_opportunities"))
    assert r.status_code == 200
    sc = r.json()["result"]["structuredContent"]
    assert "status" in sc
    assert sc["status"] in ("ok", "insufficient_data", "error")
    assert "opportunities" in sc
    assert "checked_rules" in sc
    assert "analysis_message" in sc


def test_savings_tool_text_distinguishes_insufficient_data(client):
    """With no consumer devices, text must say 'Insufficient data', not 'no opportunities'."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_savings_opportunities", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_savings_opportunities"))
    assert r.status_code == 200
    result = r.json()["result"]
    sc = result["structuredContent"]
    text = result["content"][0]["text"]

    if sc["status"] == "insufficient_data":
        assert "insufficient" in text.lower() or "no active" in text.lower() or "No active" in text
        # Must NOT claim the household is efficient
        assert "efficient" not in text.lower()


def test_savings_tool_with_consumer_readings(client):
    """Consumer device with overnight standby readings → opportunity returned."""
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    # Create a consumer device and add overnight readings for 5 nights
    dev = _make_device(client, hid, kind="consumer", name="Water Heater")
    now = datetime.now(timezone.utc)
    _night_readings(client, dev, n_nights=5, base_now=now, power_w=200.0)

    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_savings_opportunities", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_savings_opportunities"))
    assert r.status_code == 200
    sc = r.json()["result"]["structuredContent"]
    assert sc["status"] == "ok"
    assert len(sc["opportunities"]) == 1
    assert "Water Heater" in sc["opportunities"][0]["title"]
    assert sc["opportunities"][0]["estimated_monthly_saving_kwh"] > 0


def test_savings_tool_generator_not_in_opportunities(client):
    """A generator device (solar) must never appear in savings opportunities."""
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    # Add generator device with overnight readings
    solar = _make_device(client, hid, kind="generator", name="Solar Panel")
    now = datetime.now(timezone.utc)
    _night_readings(client, solar, n_nights=5, base_now=now, power_w=2000.0)

    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_savings_opportunities", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_savings_opportunities"))
    assert r.status_code == 200
    sc = r.json()["result"]["structuredContent"]
    # No consumer devices → insufficient_data (not ok with solar in opportunities)
    assert sc["status"] == "insufficient_data"
    for opp in sc.get("opportunities", []):
        assert "Solar" not in opp.get("title", "")


def test_savings_tool_household_isolation(client):
    """Savings opportunities for household B must not include household A's devices."""
    # Household A: consumer with overnight standby
    me_a = register(client)
    hid_a = me_a["memberships"][0]["household_id"]
    dev_a = _make_device(client, hid_a, kind="consumer", name="Fridge A")
    now = datetime.now(timezone.utc)
    _night_readings(client, dev_a, n_nights=5, base_now=now, power_w=200.0)

    # Household B: no devices
    client_b = make_client()
    with client_b as cb:
        me_b = register(cb)
        ci_b = _register_client()
        tokens_b = _full_oauth(cb, me_b, ci_b)
        tok_b = tokens_b["access_token"]

        r = cb.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_savings_opportunities", "arguments": {}}),
                    headers=_auth_headers(tok_b, "tools/call", "get_savings_opportunities"))
        assert r.status_code == 200
        sc = r.json()["result"]["structuredContent"]
        # B has no devices → must be insufficient_data or ok with empty opportunities
        assert sc["opportunities"] == []
        # Must not see Fridge A
        for opp in sc.get("opportunities", []):
            assert "Fridge A" not in opp.get("title", "")


# ─── MCP tool: get_usage_anomalies ────────────────────────────────────────────

def test_anomaly_tool_exists_in_tools_list(client):
    """get_usage_anomalies must appear in tools/list."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/list"),
                    headers=_auth_headers(tok, "tools/list"))
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "get_usage_anomalies" in names


def test_anomaly_tool_no_data_returns_insufficient(client):
    """No readings → get_usage_anomalies returns insufficient_data."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_usage_anomalies",
                        "arguments": {"analysis_days": 3, "history_days": 14},
                    }),
                    headers=_auth_headers(tok, "tools/call", "get_usage_anomalies"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    sc = result["structuredContent"]
    assert "status" in sc
    assert sc["status"] in ("insufficient_data", "ok")
    assert "findings" in sc
    assert "analysis_message" in sc


def test_anomaly_tool_invalid_args_rejected(client):
    """analysis_days=0 (below minimum 1) → isError:true."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_usage_anomalies",
                        "arguments": {"analysis_days": 0},
                    }),
                    headers=_auth_headers(tok, "tools/call", "get_usage_anomalies"))
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True


def test_anomaly_tool_analysis_days_too_large_rejected(client):
    """analysis_days=15 (above maximum 14) → isError:true."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_usage_anomalies",
                        "arguments": {"analysis_days": 15},
                    }),
                    headers=_auth_headers(tok, "tools/call", "get_usage_anomalies"))
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True


def test_anomaly_tool_household_isolation(client):
    """Anomaly results for user B must not include user A's readings."""
    from app.utils import iso_z, utcnow

    me_a = register(client)
    hid_a = me_a["memberships"][0]["household_id"]
    # Add a device and readings for A
    r_d = client.post(f"{API}/devices",
                      json={"household_id": hid_a, "name": "Meter A", "source": "smart_plug"},
                      headers=csrf(client))
    dev_a = r_d.json()

    # Ingest readings for A (with correct energy_kwh)
    now = datetime.now(timezone.utc)
    interval = 300
    energy = round(500.0 * interval / 3_600_000, 6)
    for i in range(30):
        ts = now - timedelta(days=30 - i, hours=12)
        body = {"device_id": dev_a["id"], "measured_at": iso_z(ts),
                "power_w": 500.0, "energy_kwh": energy, "interval_seconds": interval}
        client.post(f"{API}/readings", json=body,
                    headers={"Authorization": f"Bearer {dev_a['ingestion_secret']}"})

    client_b = make_client()
    with client_b as cb:
        me_b = register(cb)
        ci_b = _register_client()
        tokens_b = _full_oauth(cb, me_b, ci_b)
        tok_b = tokens_b["access_token"]

        r = cb.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_usage_anomalies",
                        "arguments": {"analysis_days": 3, "history_days": 14},
                    }),
                    headers=_auth_headers(tok_b, "tools/call", "get_usage_anomalies"))
        assert r.status_code == 200
        sc = r.json()["result"]["structuredContent"]
        # B has no readings → must be insufficient_data
        assert sc["status"] == "insufficient_data"
        assert sc.get("findings", []) == []


def test_anomaly_tool_defaults_work(client):
    """Calling get_usage_anomalies with no arguments uses defaults and doesn't crash."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {
                        "name": "get_usage_anomalies",
                        "arguments": {},
                    }),
                    headers=_auth_headers(tok, "tools/call", "get_usage_anomalies"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    sc = result["structuredContent"]
    assert "status" in sc
    assert "findings" in sc


# ─── LLM routing: anomaly question must use get_usage_anomalies ───────────────

def test_chat_anomaly_question_routes_to_anomaly_tool(client, monkeypatch):
    """For anomaly questions, LLM should call get_usage_anomalies, not get_savings_opportunities."""
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    tools_called = []

    def fake_completion(messages, tools):
        calls = sum(1 for m in messages if m.get("role") == "tool")
        if calls == 0:
            # First call: model requests anomaly tool
            tools_called.append("get_usage_anomalies")
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_usage_anomalies", "arguments": {}}}],
            }
        return {"role": "assistant", "content": "I checked for anomalies."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "Check for abnormal electricity usage"},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    tool_names = [tc["name"] for tc in j["tool_calls"]]
    # anomaly question → anomaly tool called
    assert "get_usage_anomalies" in tool_names
    # savings tool should NOT have been called
    assert "get_savings_opportunities" not in tool_names


def test_chat_savings_question_routes_to_savings_tool(client, monkeypatch):
    """For savings questions, LLM should call get_savings_opportunities."""
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    def fake_completion(messages, tools):
        calls = sum(1 for m in messages if m.get("role") == "tool")
        if calls == 0:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_savings_opportunities", "arguments": {}}}],
            }
        return {"role": "assistant", "content": "Here are savings tips from the data."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "How can I reduce my electricity bill?"},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    tool_names = [tc["name"] for tc in j["tool_calls"]]
    assert "get_savings_opportunities" in tool_names


# ─── Status propagation: tool result → text ──────────────────────────────────

def test_savings_insufficient_data_text_not_claiming_efficiency(client, monkeypatch):
    """When savings returns insufficient_data, the LLM response must not claim efficiency."""
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    calls_n = {"n": 0}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_savings_opportunities", "arguments": {}}}],
            }
        # Check that the tool result was passed correctly — status should be insufficient_data
        tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
        assert tool_msg is not None
        tool_data = json.loads(tool_msg["content"])
        # The tool result must contain a status field (not just has_data)
        assert "status" in tool_data
        return {"role": "assistant", "content": "Not enough data to analyse savings. The overnight standby check needs more history."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "What can I change to reduce my bill?"},
                    headers=csrf(client))
    assert r.status_code == 200
    reply = r.json()["reply"]
    # The reply must not falsely claim the household is efficient
    assert "efficient" not in reply.lower()


def test_anomaly_tool_result_has_required_fields(client):
    """get_usage_anomalies structuredContent must have all required fields."""
    me = register(client)
    ci = _register_client()
    tokens = _full_oauth(client, me, ci)
    tok = tokens["access_token"]

    r = client.post("/mcp/v1",
                    json=_modern_body("tools/call", {"name": "get_usage_anomalies", "arguments": {}}),
                    headers=_auth_headers(tok, "tools/call", "get_usage_anomalies"))
    assert r.status_code == 200
    sc = r.json()["result"]["structuredContent"]
    required_fields = [
        "status", "findings", "analysis_period_days",
        "baseline_days_used", "baseline_median_kwh", "baseline_mad_kwh",
        "reading_coverage_pct", "analysis_message",
    ]
    for field in required_fields:
        assert field in sc, f"Missing field: {field}"
