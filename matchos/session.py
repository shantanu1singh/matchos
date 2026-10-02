"""Live match session: simulator ticks in, Agent Framework workflow out."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from matchos.language_agent import personalize
from matchos.schemas import Overlay, PipelineResult, ViewerProfile
from matchos.simulator import MatchSimulator
from matchos.workflow import LiveTick, MatchServices, Workflow, build_workflow


@dataclass
class Candidate:
    at: float
    result: PipelineResult


@dataclass
class MatchSession:
    scenario_id: str
    seed: int
    speed: int
    viewer: ViewerProfile
    simulator: MatchSimulator = field(init=False)
    workflow: Workflow = field(init=False)
    services: MatchServices = field(init=False)
    running: bool = True
    candidates: list[Candidate] = field(default_factory=list)
    active: PipelineResult | None = None
    on_air_at: float = 0.0
    started_wall: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        self.simulator = MatchSimulator(seed=self.seed, scenario_id=self.scenario_id)
        self.workflow, self.services = build_workflow()

    async def warmup(self) -> None:
        ticks = await asyncio.to_thread(self.simulator.collect_warmup)
        for tick in ticks:
            self.services.state_engine.ingest(tick)

    async def tick(self) -> tuple[object, list[PipelineResult]]:
        frame_started = time.perf_counter()
        dt = max(100, int(100 * self.speed))
        tick = await asyncio.to_thread(self.simulator.step, dt)
        outputs: list[PipelineResult] = []
        async for event in self.workflow.run(LiveTick(tick=tick, viewer=self.viewer), stream=True):
            data = event.data
            if isinstance(data, PipelineResult):
                if data.kind == "moment":
                    data.timings_ms["fast"] = (time.perf_counter() - frame_started) * 1000
                outputs.append(data)
                self._remember(data)
        return tick.frame, tick.events, outputs

    def _remember(self, result: PipelineResult) -> None:
        if result.kind == "moment" and result.moment is not None:
            self.candidates.append(Candidate(at=time.time(), result=result))
            self.candidates = self.candidates[-40:]
        elif result.kind == "trace" and (result.decision is not None or result.producer is not None):
            self.candidates.append(Candidate(at=time.time(), result=result))
            self.candidates = self.candidates[-40:]
        elif result.kind == "enrichment" and self.active and self.active.moment and result.moment_id == self.active.moment.id:
            self.active.overlay = result.overlay
            self.active.enrichment_text = result.enrichment_text

    def take_air(self, result: PipelineResult) -> bool:
        """Keep one graphic up long enough to reframe it. Goals and red cards can break in."""
        if result.moment is None:
            return False
        now = time.monotonic()
        major = result.moment.frame in {"GOAL", "COMEBACK", "RED_CARD"}
        if self.active is None or self.on_air_at == 0 or major or now - self.on_air_at >= 40:
            self.active = result
            self.on_air_at = now
            return True
        if result.producer is not None:
            result.producer.action = "SUPPRESS"
            result.producer.reason = "A graphic is already on air."
        return False

    async def reframe(self) -> Overlay | None:
        if self.active is None or self.active.moment is None or self.services.agent is None:
            return None
        from matchos.copy import render_overlay

        fast = render_overlay(self.active.moment, self.viewer, rich=False, provider="template")
        self.active.overlay = fast
        text = await personalize(self.services.agent, self.active.moment, self.viewer)
        rich = render_overlay(self.active.moment, self.viewer, rich=True, provider=self.services.language_provider)
        rich.body = text
        if self.viewer.persona == "broadcaster":
            rich.headline = text
        self.active.overlay = rich
        return rich
