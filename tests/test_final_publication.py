"""Only finished, current persona text can become an on-air card."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from test_editorial_selection import answers, setup_candidates

import matchos.language_agent as language_module
from matchos.copy import render_overlay
from matchos.decision_client import _parse_selection
from matchos.perspective import Perspective
from matchos.schemas import PipelineResult, VerifiedCandidates, ViewerProfile
from matchos.telemetry import Telemetry


def worker_setup(monkeypatch, render, *, timeout=1, persona="analyst", agent=True):
    engine, candidates = setup_candidates()
    target = next(candidate for candidate in candidates if candidate.frame == "MOMENTUM_SWING")

    class Selector:
        async def select(self, state, offered, viewer, context):
            return _parse_selection(answers(target.id, offered), offered, "test", 1)

    monkeypatch.setattr(language_module, "generate_text", render)
    session = SimpleNamespace(
        services=SimpleNamespace(
            decisions=Selector(),
            agent=object() if agent else None,
            language_provider="test-language",
            reasoning_timeout_s=timeout,
        ),
        telemetry=Telemetry(),
        live_state_engine=engine,
    )
    messages = []

    async def send(message):
        messages.append(message)

    worker = Perspective(session, ViewerProfile(persona=persona), 0, send)
    batch = PipelineResult(
        kind="candidates",
        candidates=VerifiedCandidates(candidates=candidates),
        state=engine.state.model_copy(deep=True),
    )
    return worker, batch, engine, target, messages


def test_previous_story_stays_until_one_final_card_and_air_time_starts_on_publish(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def render(*args):
            started.set()
            await release.wait()
            return "Final persona-specific copy"

        worker, batch, _, target, messages = worker_setup(monkeypatch, render)
        previous = target.model_copy(update={"id": "previous", "expires_at_ms": target.expires_at_ms + 10000})
        worker.active, worker.on_air_at = previous, time.monotonic() - 41
        producer_clock = [100]
        worker.publication.clock = lambda: producer_clock[0]
        worker.offer(batch)
        await asyncio.wait_for(started.wait(), 1)
        assert messages == []
        assert worker.active is previous and worker.recent == []
        assert worker.publication.last_air == {} and worker.publication.last_frame == {}
        producer_clock[0] = 200
        ready = time.monotonic()
        release.set()
        await asyncio.wait_for(worker.task, 1)
        assert len(messages) == 1 and messages[0]["phase"] == "decision"
        assert messages[0]["result"]["overlay"]["body"] == "Final persona-specific copy"
        assert messages[0]["explanation"]["status"] == "update"
        assert worker.active.id == target.id
        assert worker.on_air_at >= ready and worker.recent[0]["aired_at"] >= ready
        assert worker.publication.last_air["analyst"] == 200
        await worker.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["expired", "closed"])
def test_late_results_never_replace_a_card_or_reserve_airtime(monkeypatch, outcome):
    async def run():
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def render(*args):
            started.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
            return "Late text"

        worker, batch, engine, target, messages = worker_setup(monkeypatch, render)
        worker.offer(batch)
        await asyncio.wait_for(started.wait(), 1)
        if outcome == "closed":
            await worker.close()  # Used by persona changes, seek, reset and disconnect.
            assert cancelled.is_set() and messages == []
        else:
            engine.state.timestamp_ms = target.expires_at_ms
            release.set()
            await asyncio.wait_for(worker.task, 1)
            assert len(messages) == 1
            assert messages[0]["explanation"]["status"] == "no_interruption"
            assert messages[0]["result"]["overlay"] is None
        assert worker.active is None and worker.recent == []
        assert worker.publication.last_air == {} and worker.publication.last_frame == {}
        await worker.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "exception", "no_agent"])
def test_render_failure_publishes_one_persona_fallback(monkeypatch, failure):
    async def run():
        async def render(*args):
            if failure == "exception":
                raise RuntimeError("model failed")
            if failure == "no_agent":
                raise AssertionError("must skip the model")
            await asyncio.Event().wait()

        worker, batch, _, target, messages = worker_setup(
            monkeypatch, render, timeout=0.01, persona="fan", agent=failure != "no_agent"
        )
        worker.offer(batch)
        await asyncio.wait_for(worker.task, 1)
        assert len(messages) == 1 and messages[0]["phase"] == "decision"
        overlay = messages[0]["result"]["overlay"]
        assert overlay["source"] == "template"
        assert overlay["body"] == render_overlay(target, worker.viewer, rich=True, provider="template").body
        assert worker.active is not None and len(worker.recent) == 1
        await worker.close()

    asyncio.run(run())


def test_language_validation_happens_before_publication(monkeypatch):
    async def run():
        original = language_module.generate_text
        worker, batch, _, target, messages = worker_setup(monkeypatch, original)

        class Agent:
            async def run(self, prompt):
                return SimpleNamespace(text="Liverpool made 999 shots.")

        worker.session.services.agent = Agent()
        worker.offer(batch)
        await asyncio.wait_for(worker.task, 1)
        assert len(messages) == 1
        assert (
            messages[0]["result"]["overlay"]["body"]
            == render_overlay(target, worker.viewer, rich=True, provider="template").body
        )
        assert "999" not in messages[0]["result"]["overlay"]["body"]
        await worker.close()

    asyncio.run(run())


def test_shared_evidence_workflow_never_invokes_editorial_models(monkeypatch):
    from matchos.simulator import MatchSimulator
    from matchos.workflow import LiveTick, build_shared_workflow, new_services

    async def run():
        engine, _ = setup_candidates()
        services = new_services()
        services.state_engine, services.verifier.state_engine = engine, engine
        engine.ingest = lambda tick: engine.state

        class ForbiddenSelector:
            async def select(self, *args):
                raise AssertionError("Shared evidence must not select a story")

        services.decisions = ForbiddenSelector()
        workflow, _ = build_shared_workflow(services)
        tick = MatchSimulator(seed=7, scenario_id="balanced").step(1000)
        outputs = await workflow.run(LiveTick(tick=tick, viewer=ViewerProfile(persona="analyst")))
        results = outputs.get_outputs()
        assert len(results) == 1 and results[0].kind == "candidates"
        assert results[0].candidates.candidates
        assert results[0].decision is None and results[0].overlay is None

    asyncio.run(run())


def test_already_expired_candidates_skip_both_model_calls(monkeypatch):
    async def run():
        async def forbidden(*args):
            raise AssertionError("stale stories must skip models")

        worker, batch, engine, target, messages = worker_setup(monkeypatch, forbidden)
        worker.session.services.decisions.select = forbidden
        engine.state.timestamp_ms = max(m.expires_at_ms for m in batch.candidates.candidates)
        worker.offer(batch)
        await asyncio.wait_for(worker.task, 1)
        assert len(messages) == 1 and messages[0]["result"]["decision"] is None
        assert messages[0]["result"]["overlay"] is None
        assert "live window" in messages[0]["explanation"]["reason"]
        assert worker.recent == []
        await worker.close()

    asyncio.run(run())
