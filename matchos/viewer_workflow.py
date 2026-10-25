"""Agent Framework orchestration for one viewer's complete editorial run.

The perspective owns scheduling and lifecycle. This graph owns every editorial
stage, including all hold branches and the final publication-state commit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_framework import Case, Default, Executor, Workflow, WorkflowBuilder, WorkflowContext, handler
from typing_extensions import Never

from matchos.decision_client import selection_hold_reason
from matchos.editorial import explain
from matchos.language_agent import draft_overlay, validate_overlay
from matchos.publication import PublicationPolicy
from matchos.schemas import PipelineResult, ProducerDecision, VerifiedMoment, VerificationResult, ViewerProfile
from matchos.telemetry import record


class ViewerRuntime(Protocol):
    session: Any
    viewer: ViewerProfile
    publication: PublicationPolicy
    recent: list[dict]
    active: VerifiedMoment | None
    on_air_at: float
    started_at: float
    closed: bool


@dataclass
class ViewerPacket:
    result: PipelineResult
    candidates: list[VerifiedMoment] = field(default_factory=list)


def _continue(packet: object) -> bool:
    return isinstance(packet, ViewerPacket) and (packet.result.producer is None or packet.result.producer.action == "AIR")


def _policy_state(runtime: ViewerRuntime) -> dict:
    return {"timestamp_ms": runtime.session.live_state_engine.state.timestamp_ms,
            "active": runtime.active, "on_air_at": runtime.on_air_at, "wall_now": time.monotonic()}


class ViewerContextExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="viewer_context")
        self.runtime = runtime

    @handler
    async def prepare(self, batch: PipelineResult, ctx: WorkflowContext[ViewerPacket]) -> None:
        runtime = self.runtime
        result = PipelineResult(kind="trace", candidates=batch.candidates, state=batch.state, trigger=batch.trigger)
        candidates = [candidate for candidate in batch.candidates.candidates
                      if candidate.expires_at_ms > runtime.session.live_state_engine.state.timestamp_ms
                      and not runtime.publication.has_aired(candidate)]
        record("viewer context", {"viewer": runtime.viewer.model_profile(), "state": batch.state,
                                  "candidates": batch.candidates}, None)
        if runtime.closed:
            return
        if not candidates:
            result.producer = (ProducerDecision(action="SUPPRESS", reason="Available stories have already aired or their live window has passed.")
                               if batch.candidates.candidates else
                               ProducerDecision(action="REJECT", reason="No story passed the evidence checks."))
        await ctx.send_message(ViewerPacket(result=result, candidates=candidates))


class EditorialSelectionExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="editorial_selection")
        self.runtime = runtime

    @handler
    async def select(self, packet: ViewerPacket, ctx: WorkflowContext[ViewerPacket]) -> None:
        runtime, result = self.runtime, packet.result
        now = time.monotonic()
        watching = round(max(0.0, now - runtime.started_at), 1)
        context = {"triggerReasons": result.trigger.reasons if result.trigger else [],
                   "viewingContext": {"hasAiredStory": bool(runtime.recent), "secondsWatching": watching,
                       "secondsWaitingForFirstStory": watching if not runtime.recent else None,
                       "secondsSinceLastAir": round(max(0.0, now - runtime.recent[-1]["aired_at"]), 1) if runtime.recent else None},
                   "recentUpdates": [{**{key: value for key, value in item.items() if key != "aired_at"},
                                      "secondsAgo": round(now - item["aired_at"], 1)} for item in runtime.recent]}
        result.decision = await runtime.session.services.decisions.select(result.state, packet.candidates, runtime.viewer, context)
        result.timings_ms["decision"] = result.decision.latency_ms
        record("editorial selection", {"viewer": runtime.viewer.model_profile(),
               "candidateIds": [candidate.id for candidate in packet.candidates], **context},
               result.decision, result.decision.latency_ms)
        chosen = next((candidate for candidate in packet.candidates if candidate.id == result.decision.selected_candidate_id), None)
        if result.decision.selected_candidate_id is not None and chosen is None:
            raise ValueError("Unverified candidate selected")
        reason = selection_hold_reason(result.decision)
        if reason:
            result.producer = ProducerDecision(action="SUPPRESS", reason=reason)
        if chosen is not None:
            result.moment = chosen.model_copy(update={"importance": result.decision.importance / 5,
                                                       "confidence": result.decision.primary_frame_confidence})
            result.moment_id = result.moment.id
            result.verification = VerificationResult(verified=True, moment=result.moment, checks=chosen.checks)
        await ctx.send_message(packet)


class PublicationPreflightExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="publication_preflight")
        self.runtime = runtime

    @handler
    async def check(self, packet: ViewerPacket, ctx: WorkflowContext[ViewerPacket]) -> None:
        if self.runtime.closed:
            return
        started = time.perf_counter()
        result = packet.result
        result.producer = self.runtime.publication.check(result.moment, self.runtime.viewer, **_policy_state(self.runtime))
        result.timings_ms["publication_preflight"] = (time.perf_counter() - started) * 1000
        record("publication preflight", {"moment": result.moment, "viewer": self.runtime.viewer.model_profile()},
               result.producer, result.timings_ms["publication_preflight"])
        await ctx.send_message(packet)


class PersonaLanguageExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="persona_language")
        self.runtime = runtime

    @handler
    async def write(self, packet: ViewerPacket, ctx: WorkflowContext[ViewerPacket]) -> None:
        if self.runtime.closed:
            return
        started = time.perf_counter()
        services = self.runtime.session.services
        packet.result.overlay = await draft_overlay(services.agent, packet.result.moment, self.runtime.viewer,
                                                     services.language_provider, getattr(services, "reasoning_timeout_s", 15))
        packet.result.timings_ms["language"] = (time.perf_counter() - started) * 1000
        record("persona language", packet.result.moment, packet.result.overlay, packet.result.timings_ms["language"])
        await ctx.send_message(packet)


class CopyValidationExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="copy_validation")
        self.runtime = runtime

    @handler
    async def validate(self, packet: ViewerPacket, ctx: WorkflowContext[ViewerPacket]) -> None:
        started = time.perf_counter()
        result = packet.result
        result.overlay = validate_overlay(result.overlay, result.moment, self.runtime.viewer)
        result.timings_ms["copy_validation"] = (time.perf_counter() - started) * 1000
        record("final overlay", result.moment, result.overlay, result.timings_ms["copy_validation"])
        await ctx.send_message(packet)


class PublicationCommitExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="publication_commit")
        self.runtime = runtime

    @handler
    async def commit(self, packet: ViewerPacket, ctx: WorkflowContext[ViewerPacket]) -> None:
        runtime, result = self.runtime, packet.result
        if runtime.closed:
            return
        started = time.perf_counter()
        result.producer = runtime.publication.commit(result.moment, runtime.viewer, **_policy_state(runtime))
        if result.producer.action == "AIR":
            result.kind = "moment"
            now = time.monotonic()
            runtime.active, runtime.on_air_at = result.moment, now
            runtime.recent = (runtime.recent + [{"aired_at": now, "frame": result.moment.frame,
                              "momentId": result.moment.id, "headline": result.overlay.headline}])[-10:]
        else:
            result.overlay = None
        result.timings_ms["publication_commit"] = (time.perf_counter() - started) * 1000
        record("publication commit", {"moment": result.moment}, result.producer, result.timings_ms["publication_commit"])
        await ctx.send_message(packet)


class ViewerOutcomeExecutor(Executor):
    def __init__(self, runtime: ViewerRuntime):
        super().__init__(id="viewer_outcome")
        self.runtime = runtime

    @handler
    async def finish(self, packet: ViewerPacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        if self.runtime.closed:
            return
        result = packet.result
        record("viewer production", {"viewer": self.runtime.viewer.model_profile(),
               "candidateIds": [candidate.id for candidate in packet.candidates]},
               {"selection": result.decision, "producer": result.producer, "moment": result.moment,
                "overlay": result.overlay, "explanation": explain(result, self.runtime.viewer)})
        await ctx.yield_output(result)


def build_viewer_workflow(runtime: ViewerRuntime) -> Workflow:
    context = ViewerContextExecutor(runtime)
    selection = EditorialSelectionExecutor(runtime)
    preflight = PublicationPreflightExecutor(runtime)
    language = PersonaLanguageExecutor(runtime)
    validation = CopyValidationExecutor(runtime)
    commit = PublicationCommitExecutor(runtime)
    outcome = ViewerOutcomeExecutor(runtime)
    return (WorkflowBuilder(start_executor=context, name=f"matchos-{runtime.viewer.persona}-editorial")
            .add_switch_case_edge_group(context, [Case(condition=_continue, target=selection), Default(target=outcome)])
            .add_switch_case_edge_group(selection, [Case(condition=_continue, target=preflight), Default(target=outcome)])
            .add_switch_case_edge_group(preflight, [Case(condition=_continue, target=language), Default(target=outcome)])
            .add_edge(language, validation)
            .add_edge(validation, commit)
            .add_edge(commit, outcome)
            .build())
