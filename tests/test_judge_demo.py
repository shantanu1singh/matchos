"""Guided review freezes real replay evidence without inventing editorial choices."""
import asyncio
import threading

from fastapi.testclient import TestClient

from matchos import server
from matchos.demo import DEMO_STOPS, demo_viewers, review_checkpoint
from matchos.session import MatchSession


def offline(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_PROVIDER", "local")


def test_checkpoints_have_the_promised_real_evidence_and_do_not_advance_time(monkeypatch):
    offline(monkeypatch)
    async def run():
        for index, expected in enumerate(("PRESS_BREAKDOWN", "GOAL", "RED_CARD")):
            session = MatchSession("press_collapse", 47129, 1, demo_viewers()[0], running=False, demo_step=index)
            try:
                await session.seek_to(DEMO_STOPS[index]["timestamp_ms"])
                stamp = session.simulator.clock_ms
                batch = await review_checkpoint(session)
                assert "guided_demo_review" in batch.trigger.reasons
                story = next(moment for moment in batch.candidates.candidates if moment.frame == expected)
                assert all(check.passed for check in story.checks)
                assert story.evidence_detail
                assert session.simulator.clock_ms == batch.state.timestamp_ms == stamp
                assert session.running is False
                if index == 0:
                    assert story.facts["defendingTeam"] == "Manchester City"
                if index > 0:
                    assert (batch.state.score_red, batch.state.score_blue) == (2, 1)
                if index == 2:
                    assert batch.state.blue.players_on_pitch == 10
                run = session.telemetry.snapshot()["runs"][0]
                assert run["metadata"]["demoCheckpoint"] == index
                assert {step["name"] for step in run["steps"]} >= {"state", "trigger"}
            finally:
                await session.close()
    asyncio.run(run())


def receive_until(ws, kind):
    messages = []
    for _ in range(100):
        message = ws.receive_json()
        messages.append(message)
        if message["type"] == kind:
            return message, messages
    raise AssertionError(f"No {kind} message")


def test_websocket_demo_settles_all_viewers_and_retains_trace_history_across_steps(monkeypatch):
    offline(monkeypatch)
    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert len(hello["demoStops"]) == 3
        trace_session = None
        for step in range(3):
            ws.send_json({"type": "demo", "step": step, "requestId": step + 10})
            # Preparing and evaluating statuses arrive before the final ready status.
            messages = []
            while True:
                message = ws.receive_json()
                messages.append(message)
                if message["type"] == "demo_status" and message["phase"] in {"ready", "error"}:
                    break
            assert message["phase"] == "ready"
            assert message["requestId"] == step + 10
            review = next(item for item in messages if item["type"] == "demo_status" and item["phase"] == "evaluating")
            assert review["candidates"]["candidates"]  # Evidence remains inspectable even when every viewer holds.
            snapshot = next(item for item in messages if item["type"] == "demo_snapshot")
            assert snapshot["frame"]["timestamp_ms"] == DEMO_STOPS[step]["timestamp_ms"]
            results = message["results"]
            assert [item["index"] for item in results] == [0, 1, 2]
            assert [item["profile"]["persona"] for item in results] == ["broadcaster", "fan", "analyst"]
            ids = [tuple(candidate["id"] for candidate in item["result"]["candidates"]["candidates"]) for item in results]
            assert ids[0] == ids[1] == ids[2]
            assert all(item["result"]["state"]["timestamp_ms"] == snapshot["frame"]["timestamp_ms"] for item in results)
            assert not any(item["type"] == "frame" for item in messages)
            assert any(item["type"] in {"moment", "trace"} for item in messages)  # Producer receives primary decisions.
            ws.send_json({"type": "telemetry", "since": 0})
            traces, _ = receive_until(ws, "telemetry")
            if trace_session is None:
                trace_session = traces["session"]
            assert traces["session"] == trace_session
            assert len([run for run in traces["runs"] if run["label"] == "Guided checkpoint evidence"]) == step + 1
            assert {run["metadata"].get("viewIndex") for run in traces["runs"] if run["label"] == "Viewer editorial selection"} == {0, 1, 2}


def test_invalid_demo_checkpoint_is_recoverable(monkeypatch):
    offline(monkeypatch)
    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        for step in (-1, 3, "0", True):
            ws.send_json({"type": "demo", "step": step})
            message = ws.receive_json()
            assert message["type"] == "demo_status" and message["phase"] == "error"
        ws.send_json({"type": "demo", "step": 2})
        snapshot, _ = receive_until(ws, "demo_snapshot")
        assert snapshot["step"] == 2


def test_demo_wait_is_responsive_and_new_match_cancels_pending_viewers(monkeypatch):
    from matchos.decision_client import LocalDecisionClient
    offline(monkeypatch)
    started, cancelled = threading.Event(), threading.Event()
    async def slow_selection(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    monkeypatch.setattr(LocalDecisionClient, "select", slow_selection)
    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "demo", "step": 2})
        receive_until(ws, "demo_snapshot")
        assert started.wait(5)
        ws.send_json({"type": "telemetry", "since": 0})
        traces, _ = receive_until(ws, "telemetry")
        assert any(run["status"] == "running" for run in traces["runs"])
        ws.send_json({"type": "start", "scenario": "balanced", "seed": 7, "viewer": {"persona": "broadcaster"}})
        status, messages = receive_until(ws, "status")
        assert status["running"] is True
        assert cancelled.wait(2)
        assert not any(message["type"] == "demo_status" and message.get("phase") == "ready" for message in messages)
