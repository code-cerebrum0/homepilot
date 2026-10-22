from datetime import timedelta
from uuid import uuid4

from app.utils import iso_z, utcnow
from tests.conftest import csrf, make_client, register

API = "/api/v1"


def make_device(c, hid, name="Living room AC", source="smart_plug"):
    r = c.post(f"{API}/devices", json={"household_id": hid, "name": name, "source": source}, headers=csrf(c))
    assert r.status_code == 201, r.text
    return r.json()


def ingest(c, dev, power=1200.0, secs=50, at=None, key=None):
    at = at or utcnow()
    body = {
        "device_id": dev["id"],
        "measured_at": iso_z(at),
        "power_w": power,
        "energy_kwh": power * secs / 3.6e6,
        "interval_seconds": secs,
        "idempotency_key": key or str(uuid4()),
    }
    return c.post(f"{API}/readings", json=body, headers={"Authorization": f"Bearer {dev['ingestion_secret']}"})


def test_register_me_and_errors(client):
    me = register(client)
    assert me["memberships"][0]["role"] == "owner"
    assert client.get(f"{API}/users/me").json()["id"] == me["id"]
    assert {"access_token", "refresh_token", "csrf_token"} <= set(client.cookies.keys())

    dup = client.post(f"{API}/auth/register", json={"name": "A", "email": client.get(f"{API}/users/me").json()["email"].upper(), "password": "correct-horse-1"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"

    short = client.post(f"{API}/auth/register", json={"name": "A", "email": "x@example.com", "password": "short"})
    assert short.status_code == 422
    body = short.json()
    assert body["error"]["code"] == "validation_error" and "short" not in body["error"]["message"]


def test_login_and_unauthenticated(client):
    me = register(client)
    anon = make_client()
    assert anon.get(f"{API}/users/me").status_code == 401
    bad = anon.post(f"{API}/auth/login", json={"email": me["email"], "password": "wrong-password"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    unknown = anon.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"})
    assert unknown.json() == bad.json()
    ok = anon.post(f"{API}/auth/login", json={"email": me["email"], "password": "correct-horse-1"})
    assert ok.status_code == 200


def test_csrf_enforced(client):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    r = client.post(f"{API}/devices", json={"household_id": hid, "name": "x", "source": "mqtt"})
    assert r.status_code == 403
    r = client.post(f"{API}/devices", json={"household_id": hid, "name": "x", "source": "mqtt"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_refresh_rotation_and_reuse_detection(client):
    register(client)
    old = client.cookies.get("refresh_token", path="/api/v1/auth")
    r = client.post(f"{API}/auth/refresh", headers=csrf(client))
    assert r.status_code == 200
    new = client.cookies.get("refresh_token", path="/api/v1/auth")
    assert new and new != old

    # replay the old (already rotated) token from another client => whole family revoked
    thief = make_client()
    thief.cookies.set("refresh_token", old, path="/api/v1/auth")
    thief.cookies.set("csrf_token", "abc")
    r = thief.post(f"{API}/auth/refresh", headers={"X-CSRF-Token": "abc"})
    assert r.status_code == 401
    r = client.post(f"{API}/auth/refresh", headers=csrf(client))
    assert r.status_code == 401


def test_logout_all(client):
    register(client)
    assert client.post(f"{API}/auth/logout-all", headers=csrf(client)).status_code == 204


def test_devices_readings_dashboard_tariff(client):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = make_device(client, hid)
    assert dev["ingestion_secret"].startswith("hp_")

    assert ingest(client, dev, key="k1").status_code == 201
    r1 = ingest(client, dev, key="dup")
    r2 = ingest(client, dev, key="dup")
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]

    bad = client.post(f"{API}/readings", json={"device_id": dev["id"], "measured_at": iso_z(utcnow()), "power_w": 100, "energy_kwh": 5, "interval_seconds": 50},
                      headers={"Authorization": f"Bearer {dev['ingestion_secret']}"})
    assert bad.status_code == 422
    future = ingest(client, dev, at=utcnow() + timedelta(hours=1))
    assert future.status_code == 422
    assert client.post(f"{API}/readings", json={"device_id": dev["id"], "measured_at": iso_z(utcnow()), "power_w": 1, "energy_kwh": 0, "interval_seconds": 50},
                       headers={"Authorization": "Bearer nope"}).status_code == 401

    lst = client.get(f"{API}/readings?device_id={dev['id']}&limit=1").json()
    assert len(lst["items"]) == 1 and lst["next_cursor"]
    page2 = client.get(f"{API}/readings?device_id={dev['id']}&limit=5&cursor={lst['next_cursor']}").json()
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None
    assert lst["items"][0]["measured_at"].endswith("Z")

    # no tariff yet
    d = client.get(f"{API}/households/{hid}/dashboard?days=7").json()
    assert d["cost"]["basis"] == "No tariff configured" and d["cost"]["today"] == 0
    assert d["freshness"] == "live" and d["current_power_w"] == 1200.0
    assert len(d["daily"]) == 7 and d["devices"][0]["share_pct"] == 100.0
    assert d["today_kwh"] > 0 and d["has_simulated_data"] is False
    assert client.get(f"{API}/households/{hid}/dashboard?days=5").status_code == 422
    assert client.get(f"{API}/households/{hid}/tariff").status_code == 404

    t = {"name": "Home tariff", "currency": "INR", "type": "flat", "flat_rate_per_kwh": 6.5,
         "fixed_monthly_charge": 120.0, "tax_percent": 5.0, "slabs": None, "tou_periods": None}
    saved = client.put(f"{API}/households/{hid}/tariff", json=t, headers=csrf(client))
    assert saved.status_code == 200 and saved.json()["id"]
    d = client.get(f"{API}/households/{hid}/dashboard?days=30").json()
    kwh = d["month"]["kwh_this_period"]
    assert len(d["daily"]) == 30
    assert abs(d["cost"]["month_to_date"] - round((kwh * 6.5 + 120) * 1.05, 2)) < 0.02
    assert d["cost"]["is_estimate"] is True and "flat rate 6.50/kWh" in d["cost"]["basis"]

    # slab + time-of-use validation / calculation
    slab = {**t, "type": "slab", "slabs": [{"up_to_kwh": 100, "rate_per_kwh": 3}, {"up_to_kwh": None, "rate_per_kwh": 6}]}
    assert client.put(f"{API}/households/{hid}/tariff", json=slab, headers=csrf(client)).status_code == 200
    badslab = {**slab, "slabs": [{"up_to_kwh": 100, "rate_per_kwh": 3}]}
    assert client.put(f"{API}/households/{hid}/tariff", json=badslab, headers=csrf(client)).status_code == 422
    tou = {**t, "type": "time_of_use", "tou_periods": [{"start": "00:00", "end": "23:59", "rate_per_kwh": 4}]}
    assert client.put(f"{API}/households/{hid}/tariff", json=tou, headers=csrf(client)).status_code == 200
    assert client.get(f"{API}/households/{hid}/dashboard").status_code == 200

    # patch / rotate / disable / delete
    assert client.patch(f"{API}/devices/{dev['id']}", json={}, headers=csrf(client)).status_code == 422
    assert client.patch(f"{API}/devices/{dev['id']}", json={"status": "disabled"}, headers=csrf(client)).json()["status"] == "disabled"
    assert ingest(client, dev).status_code == 403
    new = client.post(f"{API}/devices/{dev['id']}/rotate-secret", headers=csrf(client)).json()["ingestion_secret"]
    assert new != dev["ingestion_secret"]
    assert client.delete(f"{API}/devices/{dev['id']}", headers=csrf(client)).status_code == 204
    assert client.get(f"{API}/devices?household_id={hid}").json()["items"] == []


def test_tenant_isolation_and_roles(client):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = make_device(client, hid)

    other = make_client()
    other.__enter__()
    try:
        register(other, "Ben")
        assert other.get(f"{API}/households/{hid}/dashboard").status_code == 404
        assert other.get(f"{API}/devices?household_id={hid}").status_code == 404
        assert other.get(f"{API}/readings?device_id={dev['id']}").status_code == 404
        assert other.patch(f"{API}/devices/{dev['id']}", json={"name": "x"}, headers=csrf(other)).status_code == 404
        assert other.post(f"{API}/households/{hid}/chat", json={"message": "hi"}, headers=csrf(other)).status_code == 404
    finally:
        other.__exit__(None, None, None)


def test_member_role_cannot_admin(client):
    from app.db import SessionLocal
    from app.models import Membership

    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = make_device(client, hid)
    with SessionLocal() as db:
        m = db.query(Membership).filter_by(user_id=me["id"], household_id=hid).one()
        m.role = "member"
        db.commit()
    assert client.get(f"{API}/devices?household_id={hid}").status_code == 200
    assert client.post(f"{API}/devices", json={"household_id": hid, "name": "n", "source": "mqtt"}, headers=csrf(client)).status_code == 403
    assert client.put(f"{API}/households/{hid}/tariff", json={"currency": "INR", "type": "flat", "flat_rate_per_kwh": 1}, headers=csrf(client)).status_code == 403
    assert client.delete(f"{API}/devices/{dev['id']}", headers=csrf(client)).status_code == 403


def test_chat_tool_loop(client, monkeypatch):
    me = register(client)
    hid = me["memberships"][0]["household_id"]
    dev = make_device(client, hid)
    ingest(client, dev, key="c1")

    calls = {"n": 0}

    def fake(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "compare_energy_usage", "arguments": {"comparison": "today_vs_yesterday"}}},
                {"function": {"name": "get_current_usage", "arguments": {"household_id": 999}}},
                {"function": {"name": "nope", "arguments": {}}},
            ]}
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if calls["n"] > 2:  # second request in the same conversation: history must be replayed
            assert any(m["role"] == "assistant" and m["content"] == "You used some energy today." for m in messages)
            return {"role": "assistant", "content": "ok"}
        assert len(tool_msgs) == 3 and "rejected" in tool_msgs[1]["content"]
        return {"role": "assistant", "content": "You used some energy today."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake)
    r = client.post(f"{API}/households/{hid}/chat", json={"message": "Compare today with yesterday"}, headers=csrf(client))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["provider"] == "ollama" and j["reply"] == "You used some energy today."
    assert [c["status"] for c in j["tool_calls"]] == ["success", "error", "error"]
    assert j["tool_calls"][0]["summary"] == "today vs yesterday"

    r2 = client.post(f"{API}/households/{hid}/chat", json={"message": "again", "conversation_id": j["conversation_id"]}, headers=csrf(client))
    assert r2.json()["conversation_id"] == j["conversation_id"]
    assert client.post(f"{API}/households/{hid}/chat", json={"message": "x" * 1001}, headers=csrf(client)).status_code == 422


def test_chat_503_when_ollama_down(client, monkeypatch):
    from app.ai.ollama_client import OllamaUnavailable

    me = register(client)
    hid = me["memberships"][0]["household_id"]

    def boom(messages, tools):
        raise OllamaUnavailable()

    monkeypatch.setattr("app.ai.chat.chat_completion", boom)
    r = client.post(f"{API}/households/{hid}/chat", json={"message": "hi"}, headers=csrf(client))
    assert r.status_code == 503 and r.json()["error"]["code"] == "server_error"


def test_health(client):
    assert client.get(f"{API}/health").json() == {"status": "ok"}
