"""Microsoft Agent Framework workflow for one live match tick.

Code executors own state, triggering, verification and production.
The only model-shaped call on the way to a fast overlay is the typed
decision client. The language agent runs after that overlay is yielded.
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

from matchos.copy import render_overlay
from matchos.jev_client import DecisionClient
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
        await ctx.send_message(packet)


class TriggerExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="trigger")
        self.services = services

    @handler
    async def evaluate(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        focus_id = packet.viewer.player_id
        focus = state.players.get(focus_id)
        focus_delta = 0.0
        if focus is not None:
            previous_rate = focus.between_lines_prev30 / 30.0 if focus.between_lines_prev30 else 0.0
            recent_rate = focus.between_lines_last8 / 8.0
            focus_delta = ((recent_rate - previous_rate) / previous_rate) if previous_rate > 0 else float(focus.between_lines_last8)
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
        await ctx.send_message(packet)


class QuietExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="quiet")

    @handler
    async def stop(self, packet: PipelinePacket, ctx: WorkflowContext[Never, PipelineResult]) -> None:
        return None


class JevDecisionExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="jev_decision")
        self.services = services

    @handler
    async def decide(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        state = packet.state or self.services.state_engine.state
        reasons = packet.trigger.reasons if packet.trigger else []
        packet.decision = await self.services.decisions.decide(state, reasons, packet.viewer.player_id)
        packet.timings_ms["decision"] = packet.decision.latency_ms
        await ctx.send_message(packet)


class VerificationExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="verifier")
        self.services = services

    @handler
    async def verify(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        assert packet.decision is not None
        packet.verification = self.services.verifier.verify(state, packet.decision, packet.tick.events)
        if packet.verification.moment is not None:
            packet.verification.moment.checks = list(packet.verification.checks)
            packet.moment = packet.verification.moment
        packet.timings_ms["verifier"] = (time.perf_counter() - started) * 1000
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
        packet.producer = self.services.producer.decide(
            verification.moment if verification else None,
            bool(verification and verification.verified),
            decision.should_surface if decision else 0.0,
            packet.viewer,
            verification.reason if verification else None,
        )
        packet.timings_ms["producer"] = (time.perf_counter() - started) * 1000
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


def build_workflow(services: MatchServices | None = None) -> tuple[Workflow, MatchServices]:
    if services is None:
        services = new_services()
    language_executor, agent, provider = build_language_executor()
    services.language_provider = provider
    services.agent = agent
    state = StateUpdateExecutor(services)
    trigger = TriggerExecutor(services)
    quiet = QuietExecutor()
    jev = JevDecisionExecutor(services)
    verifier = VerificationExecutor(services)
    producer = ProducerExecutor(services)
    publish = PublishExecutor(services, language_executor)
    trace = TraceExecutor()
    enrich = EnrichmentExecutor(services)
    workflow = (
        WorkflowBuilder(start_executor=state)
        .add_edge(state, trigger)
        .add_edge(trigger, quiet, condition=_quiet)
        .add_edge(trigger, jev, condition=_fired)
        .add_edge(jev, verifier)
        .add_edge(verifier, producer)
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
    from matchos.jev_client import build_decision_client

    return MatchServices(
        state_engine=state_engine,
        trigger_engine=TriggerEngine(),
        decisions=build_decision_client(),
        verifier=Verifier(state_engine),
        producer=Producer(),
        language_provider="local-renderer",
    )
