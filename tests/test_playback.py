"""Playback reconstruction and independent persona rendering."""
import asyncio
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from matchos.schemas import PipelineResult, VerifiedMoment, ViewerProfile
from matchos.session import MatchSession
from matchos.simulator import MatchSimulator
from matchos import server


def configure(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_MODEL", "local")


def test_seek_replays_same_physics_and_restores_evidence(monkeypatch):
    configure(monkeypatch)

    async def run():
        session = MatchSession("balanced", 7, 1, ViewerProfile())
        expected = MatchSimulator(seed=7, scenario_id="balanced")
        expected.collect_warmup()
        events = []
        for _ in range(600):
            tick = expected.step(100)
            events.extend(tick.events)
        frame = await session.seek_to(60_000)
        assert frame == tick.frame
        assert list(session.live_state_engine.events) == events
        assert session.live_state_engine.state == session.services.state_engine.state
        assert session.live_state_engine.state.timestamp_ms == 60_000
        assert session._model_task is None
        assert session.active is None
        await session.close()

    asyncio.run(run())


def test_seek_clamps_to_scenario_start(monkeypatch):
    configure(monkeypatch)

    async def run():
        session = MatchSession("press_collapse", 7, 1, ViewerProfile())
        frame = await session.seek_to(-100)
        assert frame.timestamp_ms == 50 * 60_000
        assert session.live_state_engine.state.score_red == frame.score_red
        assert session.live_state_engine.events
        await session.close()

    asyncio.run(run())


def test_comparison_templates_and_fast_persona_flow_while_other_waits(monkeypatch):
    configure(monkeypatch)
    moment = VerifiedMoment(
        id="shared", type="PRESS_BREAKDOWN", frame="PRESS_BREAKDOWN", team_id="RED",
        importance=1, confidence=.9, facts={"defendingTeam": "RED", "attackingTeam": "BLUE",
        "pressBypassBefore": .2, "pressBypassNow": .7, "defensiveLineDeltaM": -5,
        "opponentFinalThirdEntries": 8, "finalThirdEntriesBefore": 2},
        evidence=[], evidence_detail=[], valid_from_ms=0, expires_at_ms=10**9)

    async def run():
        queue, sent = asyncio.Queue(), asyncio.Queue()
        waiting, cancelled = asyncio.Event(), asyncio.Event()

        class DemoSession(MatchSession):
            async def warmup(self):
                self.first = True

            async def tick(self):
                tick = self.simulator.step(100)
                outputs = [PipelineResult(kind="moment", moment=moment)] if self.first else []
                self.first = False
                return tick.frame, tick.events, outputs

        class Socket:
            async def accept(self):
                pass

            async def send_json(self, message):
                await sent.put(message)

            async def receive_json(self):
                message = await queue.get()
                if message is None:
                    raise WebSocketDisconnect()
                return message

        async def personalize(agent, incoming, viewer):
            assert incoming is moment
            if viewer.persona == "fan":
                waiting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return "Analyst response"

        monkeypatch.setattr(server, "MatchSession", DemoSession)
        monkeypatch.setattr(server, "personalize", personalize)
        task = asyncio.create_task(server.match_socket(Socket()))
        await queue.put({"type": "start", "scenario": "balanced", "views": [
            {"persona": "fan"}, {"persona": "analyst"}]})
        messages = []
        try:
            while True:
                message = await asyncio.wait_for(sent.get(), 2)
                messages.append(message)
                if message["type"] == "comparison_enrichment":
                    assert message["index"] == 1
                    assert message["momentId"] == moment.id
                    break
            template = next(m for m in messages if m["type"] == "comparison")
            assert [o["persona"] for o in template["overlays"]] == ["fan", "analyst"]
            assert template["overlays"][0]["body"] != template["overlays"][1]["body"]
            assert all(o["moment_id"] == moment.id for o in template["overlays"])
            assert waiting.is_set() and not cancelled.is_set()
            while (await asyncio.wait_for(sent.get(), 1))["type"] != "frame":
                pass
            await queue.put({"type": "control", "action": "pause"})
            while (await asyncio.wait_for(sent.get(), 1))["type"] != "status":
                pass
            await queue.put({"type": "seek", "timestamp_ms": 1000})
            while True:
                message = await asyncio.wait_for(sent.get(), 2)
                if message["type"] == "seeked":
                    assert message["frame"]["timestamp_ms"] == 1000
                    assert message["state"]["timestamp_ms"] == 1000
                    assert message["running"] is False
                    break
            assert cancelled.is_set()
        finally:
            await queue.put(None)
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
