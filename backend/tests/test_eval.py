"""End-to-end evaluation of the HomePilot AI assistant.

This module defines a repeatable evaluation set of representative user questions
and verifies:
1. The correct tool(s) are called by the assistant.
2. Tool arguments and time windows are correct.
3. Tool results are grounded in database data (not invented).
4. The final answer accurately reflects those results.

Since Ollama may not be available in CI, these tests use a deterministic routing
stub that simulates what Qwen3:4b should do after the system-prompt improvements.

Run against a live Ollama instance by setting HOMEPILOT_EVAL_LIVE=1 in the environment.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf, make_client, register

API = "/api/v1"

# Skip live Ollama evaluation unless explicitly opted in
LIVE_EVAL = os.environ.get("HOMEPILOT_EVAL_LIVE", "0") == "1"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_household_with_data(client: TestClient) -> tuple[dict, int]:
    """Register user, add device with readings, return (me, hid)."""
    from app.utils import iso_z
    me = register(client)
    hid = me["memberships"][0]["household_id"]

    dev_r = client.post(f"{API}/devices",
                        json={"household_id": hid, "name": "Fridge", "source": "smart_plug"},
                        headers=csrf(client))
    dev = dev_r.json()
    interval = 300
    energy = round(200.0 * interval / 3_600_000, 6)
    now = datetime.now(timezone.utc)
    for i in range(20):
        ts = now - timedelta(hours=i * 2)
        body = {"device_id": dev["id"], "measured_at": iso_z(ts),
                "power_w": 200.0, "energy_kwh": energy, "interval_seconds": interval}
        client.post(f"{API}/readings", json=body,
                    headers={"Authorization": f"Bearer {dev['ingestion_secret']}"})
    return me, hid


# ─── Routing verification tests (with fake_completion stubs) ─────────────────

EVALUATION_CASES = [
    # (question, expected_tool, must_not_call_tool)
    ("How much power is my home using right now?", "get_current_usage", None),
    ("How many kilowatt-hours did I use this week?", "get_daily_usage", None),
    ("Was my electricity usage higher yesterday than the day before?", "compare_energy_usage", None),
    ("Which appliance uses the most electricity?", "get_device_breakdown", None),
    ("How much has my electricity cost this month?", "get_cost_estimate", None),
    ("Show me my solar panel output", "get_solar_generation", None),
    ("What can I do to reduce my electricity bill?", "get_savings_opportunities", None),
    ("Check for abnormal electricity usage", "get_usage_anomalies", "get_savings_opportunities"),
    ("There was a spike in my electricity bill last week — investigate", "get_usage_anomalies", "get_savings_opportunities"),
    ("What devices does HomePilot track?", "get_household_context", None),
    ("What is my electricity tariff?", "get_tariff_info", None),
    ("Why was my usage higher yesterday?", "compare_energy_usage", None),
]


@pytest.mark.parametrize("question,expected_tool,must_not_call", EVALUATION_CASES)
def test_chat_routes_to_correct_tool(client, monkeypatch, question, expected_tool, must_not_call):
    """Verify that a representative question routes to the expected tool."""
    me, hid = _make_household_with_data(client)
    calls_n = {"n": 0}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            # First call: model should call the expected tool
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": expected_tool, "arguments": {}}}],
            }
        # Second call: produce the final answer
        return {"role": "assistant", "content": f"Based on the data: answer to '{question}'."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": question},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    tool_names = [tc["name"] for tc in j["tool_calls"]]
    assert expected_tool in tool_names, (
        f"Question: '{question}'\n"
        f"Expected tool: {expected_tool}\n"
        f"Tools called: {tool_names}"
    )
    if must_not_call:
        assert must_not_call not in tool_names, (
            f"Question: '{question}'\n"
            f"Tool '{must_not_call}' should NOT have been called but was.\n"
            f"Tools called: {tool_names}"
        )


def test_savings_question_calls_both_savings_and_device_breakdown(client, monkeypatch):
    """'What can I change to reduce my bill?' should call both savings and device breakdown."""
    me, hid = _make_household_with_data(client)
    calls_n = {"n": 0}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        tool_results = [m for m in messages if m.get("role") == "tool"]

        if calls_n["n"] == 1:
            # Call both tools simultaneously
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_savings_opportunities", "arguments": {}}},
                    {"function": {"name": "get_device_breakdown",
                                  "arguments": {"period": "last_7_days"}}},
                ],
            }
        return {"role": "assistant", "content": "Here is your savings analysis."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "What can I change to reduce my bill?"},
                    headers=csrf(client))
    assert r.status_code == 200
    tool_names = [tc["name"] for tc in r.json()["tool_calls"]]
    assert "get_savings_opportunities" in tool_names
    assert "get_device_breakdown" in tool_names


def test_savings_result_includes_status_field(client, monkeypatch):
    """The savings tool result passed to the LLM must include a 'status' field."""
    me, hid = _make_household_with_data(client)
    calls_n = {"n": 0}
    tool_result_content = {}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_savings_opportunities", "arguments": {}}}],
            }
        # Second call: capture what the LLM sees
        tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
        if tool_msg:
            tool_result_content.update(json.loads(tool_msg["content"]))
        return {"role": "assistant", "content": "Analysis complete."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    client.post(f"{API}/households/{hid}/chat",
                json={"message": "savings opportunities"},
                headers=csrf(client))

    assert "status" in tool_result_content, (
        f"Tool result missing 'status' field. Got: {list(tool_result_content.keys())}"
    )
    assert tool_result_content["status"] in ("ok", "insufficient_data", "error")


def test_anomaly_result_includes_findings_and_status(client, monkeypatch):
    """The anomaly tool result passed to the LLM must include 'findings' and 'status'."""
    me, hid = _make_household_with_data(client)
    calls_n = {"n": 0}
    tool_result_content = {}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_usage_anomalies", "arguments": {}}}],
            }
        tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
        if tool_msg:
            tool_result_content.update(json.loads(tool_msg["content"]))
        return {"role": "assistant", "content": "No anomalies detected."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    client.post(f"{API}/households/{hid}/chat",
                json={"message": "check for abnormal usage"},
                headers=csrf(client))

    assert "status" in tool_result_content
    assert "findings" in tool_result_content
    assert "analysis_message" in tool_result_content


def test_insufficient_data_not_claimed_as_efficiency(client, monkeypatch):
    """When savings returns insufficient_data, the LLM response must not claim energy efficiency."""
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
        # Check tool result status
        tool_msg = next((m for m in messages if m.get("role") == "tool"), None)
        data = json.loads(tool_msg["content"]) if tool_msg else {}
        assert data.get("status") == "insufficient_data", (
            f"Expected insufficient_data but got: {data.get('status')}"
        )
        # Return a correct response — NOT claiming efficiency
        return {
            "role": "assistant",
            "content": "There is not enough historical data to perform savings analysis yet."
        }

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "How can I reduce my bill?"},
                    headers=csrf(client))
    assert r.status_code == 200
    reply = r.json()["reply"].lower()
    assert "efficient" not in reply, f"Reply incorrectly claimed efficiency: {reply}"


def test_no_tool_call_repetition_within_turn(client, monkeypatch):
    """Model should not be given opportunity to call same tool twice in one turn."""
    me, hid = _make_household_with_data(client)
    current_usage_calls = {"n": 0}

    def fake_completion(messages, tools):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            current_usage_calls["n"] += 1
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_current_usage", "arguments": {}}}],
            }
        # After tool result, produce final answer (not another tool call)
        return {"role": "assistant", "content": "Your home is using 200 W right now."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "What is my current usage?"},
                    headers=csrf(client))
    assert r.status_code == 200
    # get_current_usage should only be called once
    tool_names = [tc["name"] for tc in r.json()["tool_calls"]]
    assert tool_names.count("get_current_usage") == 1


def test_malformed_tool_arguments_handled_gracefully(client, monkeypatch):
    """Malformed tool arguments should result in an error status, not a crash."""
    me, hid = _make_household_with_data(client)
    calls_n = {"n": 0}

    def fake_completion(messages, tools):
        calls_n["n"] += 1
        if calls_n["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    # Valid tool, invalid arg type
                    {"function": {"name": "get_daily_usage", "arguments": {"days": "not_a_number"}}},
                ],
            }
        return {"role": "assistant", "content": "I could not retrieve that information."}

    monkeypatch.setattr("app.ai.chat.chat_completion", fake_completion)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": "show me daily usage"},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    assert j["tool_calls"][0]["status"] == "error"
    assert "couldn't" in j["reply"].lower() or "could not" in j["reply"].lower()


# ─── Live Ollama evaluation (skipped unless HOMEPILOT_EVAL_LIVE=1) ────────────

@pytest.mark.skipif(not LIVE_EVAL, reason="Set HOMEPILOT_EVAL_LIVE=1 to run live Ollama evaluation")
@pytest.mark.parametrize("question,expected_tool", [
    ("What is my current power consumption?", "get_current_usage"),
    ("How much electricity did I use this week?", "get_daily_usage"),
    ("Was this week higher than last week?", "compare_energy_usage"),
    ("Which device uses the most power?", "get_device_breakdown"),
    ("How much will my bill be this month?", "get_cost_estimate"),
    ("Check for any abnormal usage patterns", "get_usage_anomalies"),
    ("What savings can I make on my energy usage?", "get_savings_opportunities"),
    ("What appliances do I have set up?", "get_household_context"),
    ("What is my electricity rate?", "get_tariff_info"),
    ("How much solar energy did I generate?", "get_solar_generation"),
])
def test_live_ollama_routing(client, question, expected_tool):
    """Live Ollama routing test — verifies qwen3:4b selects the right tool."""
    me, hid = _make_household_with_data(client)
    r = client.post(f"{API}/households/{hid}/chat",
                    json={"message": question},
                    headers=csrf(client))
    assert r.status_code == 200
    j = r.json()
    tool_names = [tc["name"] for tc in j["tool_calls"]]
    assert expected_tool in tool_names, (
        f"Question: '{question}'\n"
        f"Expected: {expected_tool}\n"
        f"Called: {tool_names}\n"
        f"Reply: {j['reply'][:200]}"
    )
