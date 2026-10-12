import asyncio
import json
import pytest
from agent_framework import Message
from matchos.language_agent import prompt_payload, faithful_text, render_from_messages
from matchos.schemas import VerifiedMoment, ViewerProfile
from matchos.telemetry import Telemetry, record, quiet
from matchos.model_logging import ModelCall


def moment():
    return VerifiedMoment(id="percent-test", type="TACTICAL_SHIFT", frame="MOMENTUM_SWING",
        importance=.6, confidence=.44, team_id="RED", facts={"leadingTeam": "Red",
        "possessionLast5": .639, "finalThirdEntriesBefore": 5, "finalThirdEntries": 17, "trailingTeam": "Blue"},
        evidence=[], evidence_detail=[], valid_from_ms=3000000, expires_at_ms=3040000)


def test_percent_prompt_preserves_canonical_facts_and_local_fallback():
    original = moment()
    payload = prompt_payload(original, ViewerProfile(persona="analyst"))
    assert json.loads(payload)["moment"]["facts"]["possessionLast5"] == "64%"
    assert "0.639" not in payload
    assert original.facts["possessionLast5"] == .639
    assert "64%" in render_from_messages([Message("user", [payload])])


@pytest.mark.parametrize("value", ["0.639", "0.64"])
def test_fractional_possession_is_rejected(value):
    text = f"Red held {value} possession."
    checked = faithful_text(text, moment(), ViewerProfile(persona="analyst"))
    assert checked != text
    assert "64%" in checked
    assert faithful_text("Red held 64% possession.", moment(), ViewerProfile()) == "Red held 64% possession."


def test_quiet_ticks_do_not_evict_meaningful_history():
    store = Telemetry(capacity=2)
    with store.capture("important", {}):
        record("verifier", {"raw": .639}, {"verified": True})
    for _ in range(10):
        with store.capture("tick", {}):
            record("trigger", {}, {"fired": False})
            quiet()
    snapshot = store.snapshot()
    assert len(snapshot["runs"]) == 2
    assert snapshot["runs"][0]["label"] == "important"
    assert snapshot["runs"][1]["quiet"]
    assert store.snapshot(snapshot["revision"])["runs"] == []


def test_concurrent_model_traces_are_isolated_and_redacted():
    async def run():
        first, second = Telemetry(), Telemetry()
        async def call(store, name):
            with store.capture(name, {}):
                model = ModelCall("reasoning", name, {"api_key": "hidden", "prompt": name}, secrets=("private-secret",))
                await asyncio.sleep(.01)
                model.result({"text": "private-secret"}, phase="FALLBACK")
        await asyncio.gather(call(first, "fan"), call(second, "analyst"))
        for store, name in [(first,"fan"),(second,"analyst")]:
            payload = store.snapshot()
            assert len(payload["runs"]) == 1
            steps = payload["runs"][0]["steps"]
            assert steps[0]["input"]["prompt"] == name
            assert len(steps) == 1
            assert steps[0]["call_id"]
            assert steps[0]["status"] == "fallback"
            assert steps[0]["responses"][0]["phase"] == "fallback"
            assert "private-secret" not in json.dumps(payload)
            assert "hidden" not in json.dumps(payload)
    asyncio.run(run())


def test_running_and_cancelled_calls_visible():
    store = Telemetry()
    with pytest.raises(asyncio.CancelledError):
        with store.capture("inflight", {}):
            record("provider", {}, None, status="request")
            assert store.snapshot()["runs"][0]["status"] == "running"
            raise asyncio.CancelledError()
    assert store.snapshot()["runs"][0]["status"] == "cancelled"


def test_telemetry_websocket_on_paused_match(monkeypatch):
    from fastapi.testclient import TestClient
    from matchos.server import app
    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_MODEL", "local")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type":"start", "scenario":"balanced", "seed":7, "speed":1})
        ws.send_json({"type":"pause"})
        ws.send_json({"type":"telemetry", "since":0})
        while True:
            message = ws.receive_json()
            if message["type"] == "telemetry":
                assert message["session"]
                assert "runs" in message
                break


def test_native_otel_export_is_opt_in_and_loads_env_first(monkeypatch):
    from fastapi.testclient import TestClient
    import agent_framework.observability as observation
    import matchos.server as server
    calls = []
    monkeypatch.delenv("MATCHOS_OTEL_ENABLED", raising=False)
    def load():
        monkeypatch.setenv("MATCHOS_OTEL_ENABLED", "1")
    monkeypatch.setattr(server, "load_env_files", load)
    monkeypatch.setattr(observation, "configure_otel_providers", lambda **kwargs: calls.append(kwargs))
    with TestClient(server.app):
        pass
    assert calls == [{"service_name": "matchos", "enable_sensitive_data": False}]
