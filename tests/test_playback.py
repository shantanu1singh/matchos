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


def test_independent_viewer_choices_flow_while_other_model_waits(monkeypatch):
    configure(monkeypatch)
    from test_editorial_selection import setup_candidates, answers
    from matchos.decision_client import _parse_selection
    from matchos.copy import render_overlay
    from matchos.schemas import VerifiedCandidates, ProducerDecision
    import matchos.perspective as perspective_module
    engine, candidates = setup_candidates()
    batch = PipelineResult(kind="candidates", candidates=VerifiedCandidates(candidates=candidates), state=engine.state.model_copy(deep=True))
    analyst_story = next(candidate for candidate in candidates if candidate.frame == "MOMENTUM_SWING").model_copy(update={"importance": 1, "confidence": .9})

    async def run():
        queue, sent = asyncio.Queue(), asyncio.Queue()
        model_waiting, release, language_waiting, cancelled = (asyncio.Event() for _ in range(4))
        selections = []
        class Selector:
            provider = "test"
            async def select(self, state, offered, viewer, context):
                selections.append((viewer.persona, [candidate.id for candidate in offered]))
                if viewer.persona == "fan":
                    model_waiting.set()
                    await release.wait()
                    chosen = next(candidate.id for candidate in offered if candidate.frame == "PLAYER_IMPACT")
                else:
                    chosen = "NONE"
                return _parse_selection(answers(chosen, offered), offered, "test", 1)
        class DemoSession(MatchSession):
            async def warmup(self):
                self.first = True
                self.services.decisions = Selector()
            async def tick(self):
                tick = self.simulator.step(100)
                outputs = []
                if self.first:
                    self.latest_candidates = batch
                    outputs = [batch, PipelineResult(kind="moment", state=batch.state, candidates=batch.candidates,
                        moment=analyst_story, overlay=render_overlay(analyst_story, self.viewer, rich=False, provider="template"),
                        producer=ProducerDecision(action="AIR", reason="Primary selection"))]
                self.first = False
                return tick.frame, tick.events, outputs
        class Socket:
            async def accept(self): pass
            async def send_json(self, message): await sent.put(message)
            async def receive_json(self):
                message = await queue.get()
                if message is None: raise WebSocketDisconnect()
                return message
        async def personalize(agent, incoming, viewer):
            language_waiting.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        monkeypatch.setattr(server, "MatchSession", DemoSession)
        monkeypatch.setattr(perspective_module, "personalize", personalize)
        task = asyncio.create_task(server.match_socket(Socket()))
        await queue.put({"type": "start", "scenario": "balanced", "viewer": {"persona":"analyst"},
                         "views": [{"persona":"fan", "player_id":"blue_9"}, {"persona":"casual"}]})
        messages = []
        async def until(predicate):
            for _ in range(40):
                message = await asyncio.wait_for(sent.get(), 2)
                messages.append(message)
                if predicate(message): return message
            raise AssertionError("Expected viewer update did not arrive")
        try:
            quiet = await until(lambda item: item["type"] == "viewer_update" and item["index"] == 2)
            assert quiet["explanation"]["status"] == "no_interruption"
            assert quiet["result"]["moment"] is None
            assert model_waiting.is_set()
            await until(lambda item: item["type"] == "frame")
            release.set()
            fan = await until(lambda item: item["type"] == "viewer_update" and item["index"] == 1)
            assert fan["result"]["moment"]["frame"] == "PLAYER_IMPACT"
            primary = next(item for item in messages if item["type"] == "viewer_update" and item["index"] == 0)
            assert primary["result"]["moment"]["frame"] == "MOMENTUM_SWING"
            assert all(ids == [candidate.id for candidate in candidates] for _, ids in selections)
            await asyncio.wait_for(language_waiting.wait(), 1)
            await queue.put({"type": "control", "action": "pause"})
            await until(lambda item: item["type"] == "status" and not item["running"])
            await queue.put({"type": "seek", "timestamp_ms": 1000})
            seeked = await until(lambda item: item["type"] == "seeked")
            assert seeked["running"] is False and seeked["frame"]["timestamp_ms"] == 1000
            assert cancelled.is_set()
        finally:
            await queue.put(None)
            await asyncio.wait_for(task, 2)
    asyncio.run(run())
