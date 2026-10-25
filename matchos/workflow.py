"""Shared Agent Framework evidence graph; viewer editorial graphs live in viewer_workflow."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Never

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from matchos.decision_client import DecisionClient
from matchos.language_agent import Agent, build_language_agent
from matchos.llm_config import LLMConfig
from matchos.observability import trace_carrier
from matchos.schemas import (
    MatchState,
    PipelineResult,
    Tick,
    TriggerResult,
    VerifiedCandidates,
    ViewerProfile,
)
from matchos.state_engine import StateEngine
from matchos.telemetry import quiet, record
from matchos.trigger_engine import TriggerEngine
from matchos.verifier import MAJOR_EVENT_WINDOW_MS, Verifier


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
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class MatchServices:
    state_engine: StateEngine
    trigger_engine: TriggerEngine
    decisions: DecisionClient
    verifier: Verifier
    language_provider: str
    agent: Agent | None = None
    reasoning_timeout_s: float = 15
    additional_focus_ids: list[str] = field(default_factory=list)
    observed_major_event_ids: set[str] = field(default_factory=set)


def _fired(packet: object) -> bool:
    return isinstance(packet, PipelinePacket) and packet.trigger is not None and packet.trigger.fired


def _quiet(packet: object) -> bool:
    return isinstance(packet, PipelinePacket) and not _fired(packet)


class StateUpdateExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="state_update")
        self.services = services

    @handler
    async def update(self, incoming: LiveTick, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = self.services.state_engine.ingest(incoming.tick)
        packet = PipelinePacket(
            tick=incoming.tick,
            viewer=incoming.viewer,
            state=state,
            review_requested=incoming.review_requested,
        )
        packet.timings_ms["state"] = (time.perf_counter() - started) * 1000
        record(
            "state",
            {
                "frame": incoming.tick.frame.model_dump(),
                "events": [e.model_dump() for e in incoming.tick.events],
                "sampleCount": len(incoming.tick.samples),
                "latestSamples": [sample.model_dump() for sample in incoming.tick.samples[-30:]],
            },
            state,
            packet.timings_ms["state"],
        )
        await ctx.send_message(packet)


class TriggerExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="trigger")
        self.services = services

    @handler
    async def evaluate(self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket]) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        focuses = [
            state.players.get(player_id)
            for player_id in dict.fromkeys([packet.viewer.player_id, *self.services.additional_focus_ids])
        ]
        involvement = []
        for player in focuses:
            if player is not None:
                previous_rate = player.between_lines_prev30 / 30.0 if player.between_lines_prev30 else 0.0
                recent_rate = player.between_lines_last8 / 8.0
                delta = (
                    (recent_rate - previous_rate) / previous_rate
                    if previous_rate > 0
                    else float(player.between_lines_last8)
                )
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
        recent_major = [
            event
            for event in self.services.state_engine.events
            if 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS
            and (
                (event.type == "SHOT" and event.outcome == "GOAL")
                or (event.type == "CARD" and event.qualifiers.get("card") == "red")
            )
        ]
        unseen = [event for event in recent_major if event.id not in self.services.observed_major_event_ids]
        if unseen:
            reasons = ["goal" if event.type == "SHOT" else "red_card" for event in unseen]
            packet.trigger = TriggerResult(
                fired=True, reasons=list(dict.fromkeys([*packet.trigger.reasons, *reasons]))
            )
            self.services.trigger_engine.last_fire_ms = state.timestamp_ms
        self.services.observed_major_event_ids.update(event.id for event in recent_major)
        if packet.review_requested:
            # A guided checkpoint requests evaluation; evidence and editorial selection stay unchanged.
            packet.trigger = TriggerResult(
                fired=True, reasons=list(dict.fromkeys([*packet.trigger.reasons, "guided_demo_review"]))
            )
        packet.timings_ms["trigger"] = (time.perf_counter() - started) * 1000
        record(
            "trigger",
            {"deltas": state.deltas, "samples": samples},
            packet.trigger,
            packet.timings_ms["trigger"],
        )
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


class VerificationExecutor(Executor):
    def __init__(self, services: MatchServices) -> None:
        super().__init__(id="verifier")
        self.services = services

    @handler
    async def verify(
        self, packet: PipelinePacket, ctx: WorkflowContext[PipelinePacket, PipelineResult]
    ) -> None:
        started = time.perf_counter()
        state = packet.state or self.services.state_engine.state
        packet.candidates = self.services.verifier.candidates(
            state, packet.tick.events, packet.viewer.player_id, self.services.additional_focus_ids
        )
        packet.timings_ms["verifier"] = (time.perf_counter() - started) * 1000
        record(
            "verified candidates",
            {
                "state": state.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in packet.tick.events],
                "focusPlayer": packet.viewer.player_id,
            },
            packet.candidates,
            packet.timings_ms["verifier"],
        )
        # Publish an immutable shared pool before any viewer's model call starts.
        await ctx.yield_output(
            PipelineResult(
                kind="candidates",
                trace_context=trace_carrier(),
                candidates=packet.candidates.model_copy(deep=True),
                state=state.model_copy(deep=True),
                trigger=packet.trigger,
            )
        )
        await ctx.send_message(packet)


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
        language_provider="local-renderer",
    )


def build_shared_workflow(services: MatchServices | None = None) -> tuple[Workflow, MatchServices]:
    """Named entry point for the shared, deterministic evidence graph."""
    services = services or new_services()
    services.agent, services.language_provider = build_language_agent()
    state = StateUpdateExecutor(services)
    trigger = TriggerExecutor(services)
    quiet_end = QuietExecutor()
    verifier = VerificationExecutor(services)
    workflow = (
        WorkflowBuilder(start_executor=state, name="matchos-shared-evidence")
        .add_edge(state, trigger)
        .add_edge(trigger, quiet_end, condition=_quiet)
        .add_edge(trigger, verifier, condition=_fired)
        .add_edge(verifier, CandidateEndExecutor())
        .build()
    )
    return workflow, services
