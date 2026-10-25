"""Playback reconstruction and independent persona rendering."""

import asyncio

from fastapi import WebSocketDisconnect

from matchos import server
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.session import MatchSession
from matchos.simulation import MatchSimulator


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


def test_frames_pause_and_seek_continue_while_viewer_text_is_pending(monkeypatch):
    configure(monkeypatch)
    from test_editorial_selection import answers, setup_candidates

    import matchos.language_agent as language_module
    from matchos.copy import render_overlay
    from matchos.decisions.schema import _parse_selection
    from matchos.schemas import VerifiedCandidates

    engine, candidates = setup_candidates()
    batch = PipelineResult(
        kind="candidates",
        candidates=VerifiedCandidates(candidates=candidates),
        state=engine.state.model_copy(deep=True),
    )

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
                    chosen = (
                        next(candidate.id for candidate in offered if candidate.frame == "MOMENTUM_SWING")
                        if viewer.persona == "analyst"
                        else "NONE"
                    )
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
                    outputs = [batch]
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
            if viewer.persona != "fan":
                return render_overlay(incoming, viewer, rich=True, provider="test").body
            language_waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(server, "MatchSession", DemoSession)
        monkeypatch.setattr(language_module, "generate_text", personalize)
        task = asyncio.create_task(server.match_socket(Socket()))
        await queue.put(
            {
                "type": "start",
                "scenario": "balanced",
                "viewer": {"persona": "analyst"},
                "views": [{"persona": "fan", "player_id": "blue_9"}, {"persona": "casual"}],
            }
        )
        messages = []

        async def until(predicate):
            for _ in range(40):
                message = await asyncio.wait_for(sent.get(), 2)
                messages.append(message)
                if predicate(message):
                    return message
            raise AssertionError("Expected viewer update did not arrive")

        try:
            quiet = await until(lambda item: item["type"] == "viewer_update" and item["index"] == 2)
            assert quiet["explanation"]["status"] == "no_interruption"
            assert quiet["result"]["moment"] is None
            assert model_waiting.is_set()
            await until(lambda item: item["type"] == "frame")
            release.set()
            await asyncio.wait_for(language_waiting.wait(), 1)
            await until(lambda item: item["type"] == "frame")
            assert not any(item["type"] == "viewer_update" and item["index"] == 1 for item in messages)
            primary = next(
                item for item in messages if item["type"] == "viewer_update" and item["index"] == 0
            )
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

    async def initialized():
        # Socket handlers require the same startup state as the deployed app.
        async with server.app.router.lifespan_context(server.app):
            await run()

    asyncio.run(initialized())
