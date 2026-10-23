"""Microsoft Agent Framework workflow for one live match tick.

Code executors own state, triggering, candidate verification and production.
The only model-shaped call on the way to a fast overlay is the typed
editorial decision client, which selects from verified candidates. The language agent runs after that overlay is yielded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from typing_extensions import Never

from agent_framework import (
    AgentExecutorRequest,
    AgentExecutorResponse,
    Case,
    Default,
    Executor,
    Message,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from matchos.telemetry import record, quiet
from matchos.copy import render_overlay
from matchos.decision_client import DecisionClient
from matchos.language_agent import Agent, build_language_executor, faithful_text, prompt_payload
from matchos.producer import Producer
from matchos.schemas import (
    MatchState,
    MomentDecision,
    Overlay,
    PipelineResult,
    ProducerDecision,
    Tick,
    TriggerResult,
    VerificationResult,
    VerifiedMoment,
    VerifiedCandidates,
    ViewerProfile,
)
from matchos.state_engine import StateEngine
from matchos.trigger_engine import TriggerEngine
from matchos.verifier import Verifier


@dataclass
class LiveTick:
    tick: Tick
    viewer: ViewerProfile


@dataclass
class PipelinePacket:
    tick: Tick
    viewer: ViewerProfile
    state: MatchState | None = None
    trigger: TriggerResult | None = None
    candidates: VerifiedCandidates | None = None
    decision: MomentDecision | None = None
    verification: VerificationResult | None = None
    producer: ProducerDecision | None = None
    moment: VerifiedMoment | None = None
    overlay: Overlay | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class MatchServices:
    state_engine: StateEngine
    trigger_engine: TriggerEngine
    decisions: DecisionClient
    verifier: Verifier
    producer: Producer
    language_provider: str
    agent: Agent | None = None
    pending_moment: VerifiedMoment | None = None
    pending_viewer: ViewerProfile | None = None
    recent_updates: list[dict] = field(default_factory=list)
    additional_focus_ids: list[str] = field(default_factory=list)
    shared_candidates_only: bool = False


def _fired(packet: object) -> bool:
    return isinstance(packet, PipelinePacket) and packet.trigger is not None and packet.trigger.fired


def _quiet(packet: object) -> bool:
    return isinstance(packet, PipelinePacket) and not _fired(packet)


def _air(packet: object) -> bool:
    return isinstance(packet, PipelinePacket) and packet.producer is not None and packet.producer.action == "AIR"


class StateUpdateExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="state_update")
        self.services = services

    @handler
    async def update(self, incoming: LiveTick, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = self.services.state_engine.ingest(incoming.tick)
        packet = PipelinePacket(tick=incoming.tick, viewer=incoming.viewer, state=state)
        packet.timings_ms["state"] = (time.perf_counter() - started) * 1000
        record("state", {"frame": incoming.tick.frame.model_dump(), "events": [e.model_dump() for e in incoming.tick.events], "sampleCount": len(incoming.tick.samples), "latestSamples": [sample.model_dump() for sample in incoming.tick.samples[-30:]]}, state, packet.timings_ms["state"])
        await ctx.send_message(packet)


class TriggerExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="trigger")
        self.services = services

    @handler
    async def evaluate(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        focuses = [state.players.get(player_id) for player_id in dict.fromkeys(
            [packet.viewer.player_id, *self.services.additional_focus_ids])]
        involvement = []
        for player in focuses:
            if player is not None:
                previous_rate = player.between_lines_prev30 / 30.0 if player.between_lines_prev30 else 0.0
                recent_rate = player.between_lines_last8 / 8.0
                delta = (recent_rate - previous_rate) / previous_rate if previous_rate > 0 else float(player.between_lines_last8)
                involvement.append((delta, player.between_lines_last8, player))
        eligible = [item for item in involvement if item[1] >= 4]
        focus_delta, _, focus = max(eligible or involvement, key=lambda item: item[0], default=(0, 0, None))
        state.deltas["focus_player_involvement_delta"] = round(focus_delta, 3)
        samples = {
            "red": state.red.press_bypass_sample,
            "blue": state.blue.press_bypass_sample,
            "red_rate": state.red.press_bypass_rate or 0.0,
            "blue_rate": state.blue.press_bypass_rate or 0.0,
            "red_possession": state.red.possession_last5,
            "blue_possession": state.blue.possession_last5,
            "focus_between": focus.between_lines_last8 if focus else 0,
        }
        packet.trigger = self.services.trigger_engine.evaluate(packet.tick, state.deltas, samples)
        packet.timings_ms["trigger"] = (time.perf_counter() - started) * 1000
        record("trigger", {"deltas": state.deltas, "samples": samples}, packet.trigger, packet.timings_ms["trigger"])
        await ctx.send_message(packet)


class QuietExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="quiet")

    @handler
    async def stop(self, packet: PipelinePacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        quiet()
        return None


class CandidateEndExecutor(Executor):
    def __init__(self):
        super().__init__(id="candidate_broadcast")

    @handler
    async def stop(self, packet: PipelinePacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        # Candidate publication already happened in the verifier. Each viewer runs independently.
        return None


class DecisionExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="decision")
        self.services = services

    @handler
    async def decide(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        state = packet.state or self.services.state_engine.state
        reasons = packet.trigger.reasons if packet.trigger else []
        candidates = packet.candidates.candidates if packet.candidates else []
        if candidates:
            now = time.monotonic()
            history = [{**{key: value for key, value in update.items() if key != "aired_at"},
                        "secondsAgo": round(now - update["aired_at"], 1)} for update in self.services.recent_updates[-10:]]
            context = {"triggerReasons": reasons, "recentUpdates": history}
            packet.decision = await self.services.decisions.select(state, candidates, packet.viewer, context)
            packet.timings_ms["decision"] = packet.decision.latency_ms
            chosen = next((candidate for candidate in candidates if candidate.id == packet.decision.selected_candidate_id), None)
            if packet.decision.selected_candidate_id is not None and chosen is None:
                raise ValueError("Decision selected an unverified candidate")
            if chosen is not None:
                packet.moment = chosen.model_copy(update={"importance": packet.decision.importance / 5,
                                                        "confidence": packet.decision.primary_frame_confidence})
                packet.verification = VerificationResult(verified=True, moment=packet.moment, checks=chosen.checks)
            else:
                # Abstention is an editorial outcome, not failed factual verification.
                packet.verification = None
            record("editorial selection", {"candidateIds": [candidate.id for candidate in candidates], "viewer": packet.viewer.model_dump(), **context}, packet.decision, packet.decision.latency_ms)
        else:
            packet.verification = VerificationResult(verified=False, checks=packet.candidates.checks if packet.candidates else [],
                                                     reason="No candidate passed the evidence checks; model call skipped.")
            record("editorial selection", {"candidateIds": []}, {"skipped": True, "reason": packet.verification.reason})
        await ctx.send_message(packet)


class VerificationExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="verifier")
        self.services = services

    @handler
    async def verify(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket, PipelineResult]) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        packet.candidates = self.services.verifier.candidates(state, packet.tick.events, packet.viewer.player_id, self.services.additional_focus_ids)
        packet.timings_ms["verifier"] = (time.perf_counter() - started) * 1000
        record("verified candidates", {"state": state.model_dump(mode="json"), "events": [event.model_dump(mode="json") for event in packet.tick.events], "focusPlayer": packet.viewer.player_id}, packet.candidates, packet.timings_ms["verifier"])
        # Publish an immutable shared pool before any viewer's model call starts.
        await ctx.yield_output(PipelineResult(kind="candidates", candidates=packet.candidates.model_copy(deep=True),
                                             state=state.model_copy(deep=True), trigger=packet.trigger))
        await ctx.send_message(packet)


class ProducerExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="producer")
        self.services = services

    @handler
    async def produce(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        verification = packet.verification
        decision = packet.decision
        if decision is not None and decision.selected_candidate_id is None:
            packet.producer = ProducerDecision(action="SUPPRESS", reason="Decision model chose no update.")
        else:
            packet.producer = self.services.producer.decide(
                verification.moment if verification else None,
                bool(verification and verification.verified),
                decision.should_surface if decision else 0.0,
                packet.viewer,
                verification.reason if verification else None,
            )
        packet.timings_ms["producer"] = (time.perf_counter() - started) * 1000
        record("producer", packet.verification, packet.producer, packet.timings_ms["producer"])
        await ctx.send_message(packet)


class PublishExecutor(Executor):
    def __init__(self, services: MatchServices, language_executor) -> None:
        super().__init__(id="publish")
        self.services = services
        self.language_executor = language_executor

    @handler
    async def publish(self, packet: PipelinePacket, ctx: WorkflowContext[AgentExecutorRequest, PipelineResult]) -> None:
        started = time.perf_counter()
        assert packet.moment is not None
        overlay = render_overlay(packet.moment, packet.viewer, rich=False, provider="template")
        packet.overlay = overlay
        packet.timings_ms["template"] = (time.perf_counter() - started) * 1000
        record("template", packet.moment, overlay, packet.timings_ms["template"])
        self.services.pending_moment = packet.moment
        self.services.pending_viewer = packet.viewer
        await ctx.yield_output(self._result(packet, "moment"))
        # Each enrichment stands alone. Prior moments must not leak into the prompt.
        self.language_executor._cache.clear()
        await ctx.send_message(
            AgentExecutorRequest(
                messages=[Message("user", [prompt_payload(packet.moment, packet.viewer)])],
                should_respond=True,
            )
        )

    def _result(self, packet: PipelinePacket, kind: str) -> PipelineResult:
        return PipelineResult(
            kind=kind,  # type: ignore[arg-type]
            trigger=packet.trigger,
            candidates=packet.candidates,
            decision=packet.decision,
            verification=packet.verification,
            producer=packet.producer,
            moment=packet.moment,
            overlay=packet.overlay,
            state=packet.state,
            timings_ms=packet.timings_ms,
            moment_id=packet.moment.id if packet.moment else None,
        )


class TraceExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="trace")

    @handler
    async def trace(self, packet: PipelinePacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        if packet.trigger is None or not packet.trigger.fired:
            return
        await ctx.yield_output(
            PipelineResult(
                kind="trace",
                trigger=packet.trigger,
                candidates=packet.candidates,
                decision=packet.decision,
                verification=packet.verification,
                producer=packet.producer,
                moment=packet.moment,
                state=packet.state,
                timings_ms=packet.timings_ms,
                moment_id=packet.moment.id if packet.moment else None,
            )
        )


class EnrichmentExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="enrichment")
        self.services = services

    @handler
    async def enrich(self, response: AgentExecutorResponse, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        moment = self.services.pending_moment
        viewer = self.services.pending_viewer
        if moment is None or viewer is None:
            return
        text = faithful_text(response.agent_response.text or "", moment, viewer)
        overlay = render_overlay(moment, viewer, rich=True, provider=self.services.language_provider)
        overlay.body = text
        overlay.headline = text if viewer.persona == "broadcaster" else overlay.headline
        await ctx.yield_output(
            PipelineResult(
                kind="enrichment",
                moment=moment,
                overlay=overlay,
                moment_id=moment.id,
                enrichment_text=text,
            )
        )


def build_workflow(services: MatchServices | None = None, *, shared_candidates_only: bool = False) -> tuple[Workflow, MatchServices]:
    if services is None:
        services = new_services()
    services.shared_candidates_only = shared_candidates_only or services.shared_candidates_only
    language_executor, agent, provider = build_language_executor()
    services.language_provider = provider
    services.agent = agent
    state = StateUpdateExecutor(services)
    trigger = TriggerExecutor(services)
    quiet = QuietExecutor()
    decision = DecisionExecutor(services)
    verifier = VerificationExecutor(services)
    producer = ProducerExecutor(services)
    publish = PublishExecutor(services, language_executor)
    trace = TraceExecutor()
    enrich = EnrichmentExecutor(services)
    if services.shared_candidates_only:
        workflow = (WorkflowBuilder(start_executor=state)
                    .add_edge(state, trigger)
                    .add_edge(trigger, quiet, condition=_quiet)
                    .add_edge(trigger, verifier, condition=_fired)
                    .add_edge(verifier, CandidateEndExecutor()).build())
        return workflow, services
    workflow = (
        WorkflowBuilder(start_executor=state)
        .add_edge(state, trigger)
        .add_edge(trigger, quiet, condition=_quiet)
        .add_edge(trigger, verifier, condition=_fired)
        .add_edge(verifier, decision)
        .add_edge(decision, producer)
        .add_switch_case_edge_group(
            producer,
            [
                Case(condition=_air, target=publish),
                Default(target=trace),
            ],
        )
        .add_edge(publish, language_executor)
        .add_edge(language_executor, enrich)
        .build()
    )
    return workflow, services


def new_services() -> MatchServices:
    state_engine = StateEngine()
    from matchos.decision_client import build_decision_client

    return MatchServices(
        state_engine=state_engine,
        trigger_engine=TriggerEngine(),
        decisions=build_decision_client(),
        verifier=Verifier(state_engine),
        producer=Producer(),
        language_provider="local-renderer",
    )
