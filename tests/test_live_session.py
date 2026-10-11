"""Model latency must not gate live frames or lose pending feed evidence."""

import asyncio
from types import SimpleNamespace

from matchos.schemas import PipelineResult, VerifiedMoment, ViewerProfile
from matchos.session import MatchSession
from matchos.server import _run


def _session(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_MODEL", "local")
    return MatchSession(scenario_id="balanced", seed=7, speed=1, viewer=ViewerProfile())


def test_frames_flow_while_decision_waits_and_pending_evidence_is_preserved(monkeypatch):
    async def run():
        session = _session(monkeypatch)
        started, release, second, cancelled = (asyncio.Event() for _ in range(4))
        calls = []

        class SlowWorkflow:
            async def run(self, incoming, stream):
                calls.append(incoming.tick)
                session.services.state_engine.ingest(incoming.tick)
                if len(calls) == 2:
                    second.set()
                started.set()
                try:
                    await release.wait()
                    yield SimpleNamespace(data=PipelineResult(kind="trace"))
                finally:
                    cancelled.set()

        session.workflow = SlowWorkflow()
        try:
            first, _, _ = await session.tick()
            await asyncio.wait_for(started.wait(), 1)
            events, samples = [], []
            for _ in range(6):
                frame, tick_events, _ = await asyncio.wait_for(session.tick(), .5)
                events.extend(tick_events)
                assert frame.timestamp_ms > first.timestamp_ms
                assert len(calls) == 1
                assert session.live_state_engine.state.timestamp_ms == frame.timestamp_ms
                assert session.services.state_engine.state.timestamp_ms == first.timestamp_ms
            pending = session._pending_tick
            samples = list(pending.samples)
            assert pending.frame.timestamp_ms == frame.timestamp_ms
            assert pending.events == events
            release.set()
            await asyncio.wait_for(second.wait(), 1)
            assert calls[1].events == events
            assert calls[1].samples == samples
            assert calls[1].frame.timestamp_ms == frame.timestamp_ms
        finally:
            await session.close()

    asyncio.run(run())


def test_template_is_available_before_language_enrichment_finishes(monkeypatch):
    async def run():
        session = _session(monkeypatch)
        waiting, release = asyncio.Event(), asyncio.Event()
        moment = VerifiedMoment(
            id="slow-language", type="GOAL", frame="GOAL", team_id="RED",
            importance=1, confidence=.9, facts={}, evidence=[], evidence_detail=[],
            valid_from_ms=0, expires_at_ms=10**9,
        )

        class SlowLanguageWorkflow:
            async def run(self, incoming, stream):
                yield SimpleNamespace(data=PipelineResult(kind="moment", moment=moment))
                waiting.set()
                await release.wait()
                yield SimpleNamespace(data=PipelineResult(kind="enrichment", moment=moment, moment_id=moment.id))

        session.workflow = SlowLanguageWorkflow()
        try:
            await session.tick()
            await asyncio.wait_for(waiting.wait(), 1)
            _, _, outputs = await asyncio.wait_for(session.tick(), .5)
            assert [output.kind for output in outputs] == ["moment"]
            assert not session._model_task.done()
            session.take_air(outputs[0])
            session._pending_tick = None
            release.set()
            await asyncio.wait_for(session._model_task, 1)
            _, _, outputs = await session.tick()
            assert [output.kind for output in outputs] == ["enrichment"]
        finally:
            await session.close()

    asyncio.run(run())


def test_server_sends_frames_and_pause_does_not_wait_for_models(monkeypatch):
    async def run():
        session = _session(monkeypatch)
        waiting, finished = asyncio.Event(), asyncio.Event()
        messages = []

        class WaitingWorkflow:
            async def run(self, incoming, stream):
                waiting.set()
                try:
                    await asyncio.Event().wait()
                    yield SimpleNamespace(data=PipelineResult(kind="trace"))
                finally:
                    finished.set()

        session.workflow = WaitingWorkflow()

        async def send(message):
            messages.append(message)
            if sum(item["type"] == "frame" for item in messages) == 4:
                session.running = False

        try:
            await asyncio.wait_for(_run(None, session, send), 1.5)
            assert waiting.is_set() and not finished.is_set()
            frames = [item for item in messages if item["type"] == "frame"]
            assert len(frames) == 4
            times = [item["frame"]["timestamp_ms"] for item in frames]
            assert times == sorted(set(times))
            assert all(item["state"]["timestamp_ms"] == item["frame"]["timestamp_ms"] for item in frames)
            await session.close()
            assert finished.is_set() and session._model_task is None
        finally:
            await session.close()

    asyncio.run(run())


def test_old_viewer_and_expired_moments_are_not_published(monkeypatch):
    async def run():
        session = _session(monkeypatch)

        class QuietWorkflow:
            async def run(self, incoming, stream):
                if False:
                    yield

        session.workflow = QuietWorkflow()
        expired = VerifiedMoment(
            id="expired", type="GOAL", frame="GOAL", importance=1, confidence=1,
            facts={}, evidence=[], evidence_detail=[], valid_from_ms=0, expires_at_ms=1,
        )
        await session._outputs.put((PipelineResult(kind="trace"), ViewerProfile(persona="analyst")))
        await session._outputs.put((PipelineResult(kind="moment", moment=expired), session.viewer.model_copy()))
        try:
            _, _, outputs = await session.tick()
            assert outputs == []
        finally:
            await session.close()

    asyncio.run(run())
