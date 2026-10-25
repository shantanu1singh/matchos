"""Run the real framework graph through publication, holds and concurrent viewers."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from test_editorial_selection import answers, setup_candidates

from matchos.decision_client import _parse_selection
from matchos.perspective import Perspective
from matchos.publication import PublicationPolicy
from matchos.schemas import PipelineResult, VerifiedCandidates, ViewerProfile
from matchos.telemetry import Telemetry


def setup(selector, *, viewer=None, agent=None):
    engine, candidates = setup_candidates()
    messages = []

    async def send(payload):
        messages.append(payload)

    session = SimpleNamespace(
        live_state_engine=engine,
        telemetry=Telemetry(),
        services=SimpleNamespace(
            decisions=selector, agent=agent, language_provider="test", reasoning_timeout_s=1
        ),
    )
    worker = Perspective(session, viewer or ViewerProfile(persona="broadcaster"), 0, send)
    batch = PipelineResult(
        kind="candidates",
        candidates=VerifiedCandidates(candidates=candidates),
        state=engine.state.model_copy(deep=True),
    )
    return worker, batch, engine, messages


class Select:
    def __init__(self, *, surface=0.9, importance=0.2, frame="MOMENTUM_SWING"):
        self.surface, self.importance, self.frame = surface, importance, frame

    async def select(self, state, offered, viewer, context):
        target = next(c for c in offered if c.frame == self.frame)
        reply = answers(target.id, offered)
        reply["importance"]["score"] = self.importance
        reply["shouldSurface"]["probability"] = self.surface
        return _parse_selection(reply, offered, "test", 1)


def test_framework_owns_stage_order_and_only_terminal_output_reaches_the_ui():
    async def run():
        worker, batch, _, messages = setup(Select())
        visited = []
        frozen = batch.model_dump()
        original = worker.workflow.run

        async def capture(message, *, stream):
            async for event in original(message, stream=stream):
                if event.type == "executor_invoked":
                    visited.append(event.executor_id)
                yield event

        worker.workflow.run = capture
        with worker.session.telemetry.capture(
            "Viewer editorial selection", {"viewer": worker.viewer.model_dump()}
        ):
            await worker.process(batch)
        assert visited == [
            "viewer_context",
            "editorial_selection",
            "publication_preflight",
            "persona_language",
            "copy_validation",
            "publication_commit",
            "viewer_outcome",
        ]
        assert len(messages) == 1 and messages[0]["result"]["kind"] == "moment"
        assert messages[0]["result"]["producer"]["action"] == "AIR"
        steps = worker.session.telemetry.snapshot()["runs"][0]["steps"]
        names = [s["name"] for s in steps]
        assert (
            names.index("publication preflight")
            < names.index("language fact guard")
            < names.index("publication commit")
        )
        assert worker.publication.last_air and worker.recent
        assert batch.model_dump() == frozen
        await worker.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "persona,frame",
    [("broadcaster", "MOMENTUM_SWING"), ("casual", "MOMENTUM_SWING"), ("analyst", "PLAYER_IMPACT")],
)
def test_model_selection_is_not_overridden_by_old_persona_or_importance_filters(persona, frame):
    async def run():
        worker, batch, _, messages = setup(Select(frame=frame), viewer=ViewerProfile(persona=persona))
        await worker.process(batch)
        assert len(messages) == 1
        assert messages[0]["result"]["producer"]["action"] == "AIR"
        assert messages[0]["result"]["moment"]["importance"] == 0.04
        await worker.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["NONE", "low_surface", "decision_expired", "duplicate", "cadence", "on_air"]
)
def test_hold_paths_do_not_call_language_or_commit_history(mode):
    async def run():
        class ForbiddenAgent:
            async def run(self, prompt):
                pytest.fail("a held story must not call language")

        class Selector(Select):
            async def select(self, state, offered, viewer, context):
                decision = await super().select(state, offered, viewer, context)
                if mode == "NONE":
                    decision.selected_candidate_id = None
                if mode == "low_surface":
                    decision.should_surface = 0.01
                if mode == "decision_expired":
                    engine.state.timestamp_ms = max(c.expires_at_ms for c in offered)
                return decision

        worker, batch, engine, messages = setup(Selector(), agent=ForbiddenAgent())
        moment = next(c for c in batch.candidates.candidates if c.frame == "MOMENTUM_SWING")
        if mode == "duplicate":
            worker.publication.last_frame[moment.frame] = time.monotonic()
        if mode == "cadence":
            worker.publication.last_air[worker.viewer.persona] = time.monotonic()
        if mode == "on_air":
            worker.active, worker.on_air_at = moment, time.monotonic()
        before = (dict(worker.publication.last_air), dict(worker.publication.last_frame))
        await worker.process(batch)
        assert len(messages) == 1 and messages[0]["result"]["producer"]["action"] == "SUPPRESS"
        assert messages[0]["result"]["overlay"] is None and worker.recent == []
        assert before == (worker.publication.last_air, worker.publication.last_frame)
        if mode in {"NONE", "low_surface"}:
            assert messages[0]["explanation"]["reasonSource"] == "model"
        await worker.close()

    asyncio.run(run())


def test_slow_viewer_does_not_delay_other_framework_workflows_or_live_frames(monkeypatch):
    from matchos.session import MatchSession

    async def run():
        monkeypatch.setenv("DECISION_PROVIDER", "local")
        monkeypatch.setenv("REASONING_PROVIDER", "local")
        session = MatchSession("balanced", 7, 1, ViewerProfile(persona="broadcaster"))
        engine, candidates = setup_candidates()
        batch = PipelineResult(
            kind="candidates", candidates=VerifiedCandidates(candidates=candidates), state=engine.state
        )
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Selector(Select):
            async def select(self, state, offered, viewer, context):
                if viewer.persona == "fan":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                return await super().select(state, offered, viewer, context)

        session.services.decisions = Selector()
        session.services.agent = None
        outputs = []

        async def send(payload):
            outputs.append(payload)

        workers = [
            Perspective(session, ViewerProfile(persona=role), i, send)
            for i, role in enumerate(("fan", "analyst", "broadcaster"))
        ]
        try:
            for worker in workers:
                worker.offer(batch)
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(asyncio.gather(workers[1].task, workers[2].task), 1)
            assert {message["profile"]["persona"] for message in outputs} == {"analyst", "broadcaster"}
            first, _, _ = await session.tick()
            second, _, _ = await session.tick()
            assert second.timestamp_ms > first.timestamp_ms and not workers[0].task.done()
            await workers[0].close()
            assert cancelled.is_set() and workers[0].recent == []
        finally:
            for worker in workers:
                await worker.close()
            await session.close()

    asyncio.run(run())


def test_failed_framework_run_is_rebuilt_without_losing_viewer_history():
    async def run():
        class Selector(Select):
            count = 0

            async def select(self, *args):
                self.count += 1
                decision = await super().select(*args)
                if self.count == 1:
                    decision.selected_candidate_id = "not-in-pool"
                return decision

        worker, batch, _, messages = setup(Selector())
        worker.offer(batch)
        await worker.task
        assert len(messages) == 1 and messages[0]["result"]["producer"]["action"] == "SUPPRESS"
        assert not worker.publication.last_air
        worker.offer(
            batch.model_copy(
                update={
                    "state": batch.state.model_copy(update={"timestamp_ms": batch.state.timestamp_ms + 1})
                }
            )
        )
        await worker.task
        assert len(messages) == 2 and messages[-1]["result"]["producer"]["action"] == "AIR"
        assert len(worker.recent) == 1
        await worker.close()

    asyncio.run(run())


def test_publication_rechecks_on_air_state_and_major_events_can_interrupt():
    _, candidates = setup_candidates()
    moment = candidates[0]
    policy = PublicationPolicy(clock=lambda: 100)
    viewer = ViewerProfile(persona="analyst")
    state = {"timestamp_ms": moment.valid_from_ms, "wall_now": 100}
    assert policy.check(moment, viewer, **state).action == "AIR"
    assert policy.last_air == {}
    # The display can change while language is being prepared.
    held = policy.commit(moment, viewer, active=moment, on_air_at=99, **state)
    assert held.action == "SUPPRESS" and policy.last_air == {}
    goal = moment.model_copy(update={"frame": "GOAL", "evidence": ["goal-source"]})
    assert policy.commit(goal, viewer, active=moment, on_air_at=99, **state).action == "AIR"
    assert policy.commit(goal.model_copy(update={"id": "new-id"}), viewer, **state).action == "SUPPRESS"
    assert policy.last_air == {"analyst": 100}
