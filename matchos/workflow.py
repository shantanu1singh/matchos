"""Microsoft Agent Framework workflow for one live match tick.

Code executors own state, triggering, candidate verification and production.
A typed editorial decision selects from verified candidates. The language agent
prepares guarded persona copy before one complete overlay is yielded.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field

from typing_extensions import Never

from agent_framework import (
    Case,
    Default,
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from matchos.telemetry import record, quiet
from matchos.observability import trace_carrier
from matchos.decision_client import DecisionClient, selection_hold_reason
from matchos.language_agent import Agent, build_language_executor, final_overlay
from matchos.llm_config import LLMConfig
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
from matchos.verifier import Verifier, MAJOR_EVENT_WINDOW_MS


@dataclass
class LiveTick:
    tick: Tick
    viewer: ViewerProfile
    review_requested: bool = False


@dataclass
class PipelinePacket:
    tick: Tick
    viewer: ViewerProfile
    review_requested: bool = False
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
    reasoning_timeout_s: float = 15
    recent_updates: list[dict] = field(default_factory=list)
    additional_focus_ids: list[str] = field(default_factory=list)
    shared_candidates_only: bool = False
    observed_major_event_ids: set[str] = field(default_factory=set)


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
        packet = PipelinePacket(tick=incoming.tick, viewer=incoming.viewer, state=state,
                                review_requested=incoming.review_requested)
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
        # Seeking restores rolling evidence without replaying historical model calls.
        # Recover a still-recent goal/dismissal even if it preceded this live tick.
        recent_major = [event for event in self.services.state_engine.events
                        if 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS
                        and ((event.type == "SHOT" and event.outcome == "GOAL")
                             or (event.type == "CARD" and event.qualifiers.get("card") == "red"))]
        unseen = [event for event in recent_major if event.id not in self.services.observed_major_event_ids]
        if unseen:
            reasons = ["goal" if event.type == "SHOT" else "red_card" for event in unseen]
            packet.trigger = TriggerResult(fired=True, reasons=list(dict.fromkeys([*packet.trigger.reasons, *reasons])))
            self.services.trigger_engine.last_fire_ms = state.timestamp_ms
        self.services.observed_major_event_ids.update(event.id for event in recent_major)
        if packet.review_requested:
            # A guided checkpoint requests evaluation; evidence and editorial selection stay unchanged.
            packet.trigger = TriggerResult(fired=True, reasons=list(dict.fromkeys(
                [*packet.trigger.reasons, "guided_demo_review"])))
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
        await ctx.yield_output(PipelineResult(kind="candidates", trace_context=trace_carrier(), candidates=packet.candidates.model_copy(deep=True),
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
        if decision is not None and (held := selection_hold_reason(decision)):
            packet.producer = ProducerDecision(action="SUPPRESS", reason=held)
        else:
            packet.producer = deepcopy(self.services.producer).decide(
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
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="publish")
        self.services = services

    @handler
    async def publish(self, packet: PipelinePacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        started = time.perf_counter()
        assert packet.moment is not None
        packet.overlay = await final_overlay(self.services.agent, packet.moment, packet.viewer,
                                             self.services.language_provider, self.services.reasoning_timeout_s)
        packet.timings_ms["language"] = (time.perf_counter() - started) * 1000
        record("final overlay", packet.moment, packet.overlay, packet.timings_ms["language"])
        if packet.moment.expires_at_ms <= self.services.state_engine.state.timestamp_ms:
            packet.producer = ProducerDecision(action="SUPPRESS", reason="This story expired while preparing its final text.")
        else:
            packet.producer = self.services.producer.decide(packet.moment, True, packet.decision.should_surface, packet.viewer)
        if packet.producer.action != "AIR":
            packet.overlay = None
        await ctx.yield_output(self._result(packet, "moment" if packet.overlay else "trace"))

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


def build_workflow(services: MatchServices | None = None, *, shared_candidates_only: bool = False) -> tuple[Workflow, MatchServices]:
    if services is None:
        services = new_services()
    services.shared_candidates_only = shared_candidates_only or services.shared_candidates_only
    _, agent, provider = build_language_executor()
    services.language_provider = provider
    services.agent = agent
    state = StateUpdateExecutor(services)
    trigger = TriggerExecutor(services)
    quiet = QuietExecutor()
    decision = DecisionExecutor(services)
    verifier = VerificationExecutor(services)
    producer = ProducerExecutor(services)
    publish = PublishExecutor(services)
    trace = TraceExecutor()
    if services.shared_candidates_only:
        workflow = (WorkflowBuilder(start_executor=state, name="matchos-shared-evidence")
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
        .build()
    )
    return workflow, services


def new_services() -> MatchServices:
    state_engine = StateEngine()
    from matchos.decision_client import build_decision_client

    config = LLMConfig.from_env()
    return MatchServices(
        state_engine=state_engine,
        trigger_engine=TriggerEngine(),
        decisions=build_decision_client(config),
        reasoning_timeout_s=config.reasoning_timeout_s,
        verifier=Verifier(state_engine),
        producer=Producer(),
        language_provider="local-renderer",
    )


def build_shared_workflow(services: MatchServices | None = None) -> tuple[Workflow, MatchServices]:
    """Named entry point for the shared, deterministic evidence graph."""
    return build_workflow(services, shared_candidates_only=True)
