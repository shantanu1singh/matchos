"""Live match session: simulator ticks in, Agent Framework workflow out."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from matchos.telemetry import Telemetry
from matchos.language_agent import personalize
from matchos.schemas import MatchEvent, Overlay, PipelineResult, Tick, TrackingFrame, ViewerProfile
from matchos.simulator import MatchSimulator
from matchos.state_engine import StateEngine, THIRTY
from matchos.workflow import LiveTick, MatchServices, Workflow, build_shared_workflow


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
    telemetry: Telemetry = field(default_factory=Telemetry)
    running: bool = True
    demo_step: int | None = None
    demo_request_id: int | None = None
    candidates: list[Candidate] = field(default_factory=list)
    active: PipelineResult | None = None
    latest_candidates: PipelineResult | None = None
    on_air_at: float = 0.0
    started_wall: float = field(default_factory=time.perf_counter)
    live_state_engine: StateEngine = field(default_factory=StateEngine)
    _model_task: asyncio.Task | None = field(default=None, init=False)
    _pending_tick: Tick | None = field(default=None, init=False)
    _pending_started: float = field(default=0.0, init=False)
    _outputs: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=32), init=False)

    def __post_init__(self) -> None:
        self.simulator = MatchSimulator(seed=self.seed, scenario_id=self.scenario_id)
        self.workflow, self.services = build_shared_workflow()

    async def warmup(self) -> None:
        ticks = await asyncio.to_thread(self.simulator.collect_warmup)
        for tick in ticks:
            self.services.state_engine.ingest(tick)
            self.live_state_engine.ingest(tick)

    async def seek_to(self, timestamp_ms: int) -> TrackingFrame:
        """Rebuild a fresh session's evidence without making historical model calls."""
        target = max(int(self.simulator.scenario.start_minute * 60_000),
                     min(95 * 60_000, int(timestamp_ms)))
        # Align to the simulator's fixed physics step for repeatable replay.
        target = target // 50 * 50

        def replay() -> TrackingFrame:
            events, samples = [], []
            for tick in self.simulator.collect_warmup():
                events.extend(tick.events)
                samples.extend(tick.samples)
            while self.simulator.clock_ms < target:
                tick = self.simulator.step(min(1000, target - self.simulator.clock_ms))
                events.extend(tick.events)
                samples.extend(tick.samples)
            frame = self.simulator._frame()
            # Replay physics once; compute rolling aggregates only at the destination.
            history = Tick(match_id=self.simulator.match_id, frame=frame,
                           events=events, samples=samples)
            self.services.state_engine.ingest(history)
            self.live_state_engine.ingest(history)
            return frame

        return await asyncio.to_thread(replay)

    async def tick(self, *, evaluate: bool = True) -> tuple[TrackingFrame, list[MatchEvent], list[PipelineResult]]:
        frame_started = time.perf_counter()
        dt = max(100, int(100 * self.speed))
        tick = await asyncio.to_thread(self.simulator.step, dt)
        self.live_state_engine.ingest(tick)
        if not evaluate:
            # Guided clips advance live frames while reviewing one checkpoint's evidence.
            return tick.frame, tick.events, []
        # Keep one pending batch, preserving evidence while replacing its frame.
        # The workflow owns a separate rolling engine, changed only by its worker.
        if self._pending_tick is None:
            self._pending_tick = tick
            self._pending_started = frame_started
        else:
            cutoff = tick.frame.timestamp_ms - THIRTY
            self._pending_tick = tick.model_copy(update={
                "events": [event for event in self._pending_tick.events + tick.events if event.timestamp_ms >= cutoff],
                "samples": [sample for sample in self._pending_tick.samples + tick.samples if sample.timestamp_ms >= cutoff],
            })
        if self._model_task is None or self._model_task.done():
            self._model_task = asyncio.create_task(self._process_models())
        outputs: list[PipelineResult] = []
        while not self._outputs.empty():
            data, viewer = self._outputs.get_nowait()
            if data.kind != "candidates" and viewer != self.viewer:
                continue
            if data.kind in {"moment", "enrichment"} and data.moment is not None:
                if data.moment.expires_at_ms <= tick.frame.timestamp_ms:
                    continue
            outputs.append(data)
            self._remember(data)
        return tick.frame, tick.events, outputs

    async def _process_models(self) -> None:
        try:
            while self._pending_tick is not None:
                tick, started = self._pending_tick, self._pending_started
                self._pending_tick = None
                viewer = self.viewer.model_copy(deep=True)
                with self.telemetry.capture("Match workflow", {"timestamp_ms": tick.frame.timestamp_ms, "viewer": viewer.model_dump()}):
                    async for event in self.workflow.run(LiveTick(tick=tick, viewer=viewer), stream=True):
                        data = event.data
                        if isinstance(data, PipelineResult):
                            if data.kind == "moment":
                                data.timings_ms["fast"] = (time.perf_counter() - started) * 1000
                            await self._outputs.put((data, viewer))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("Model workflow failed (%s); live frames continue", type(exc).__name__)
            # A failed framework run must not leave its executors mid-run.
            self.workflow, self.services = build_shared_workflow(self.services)

    async def close(self) -> None:
        self.running = False
        if self._model_task is not None:
            self._model_task.cancel()
            try:
                await self._model_task
            except asyncio.CancelledError:
                pass
            self._model_task = None
        self._pending_tick = None
        while not self._outputs.empty():
            self._outputs.get_nowait()

    def _remember(self, result: PipelineResult) -> None:
        if result.kind == "candidates":
            self.latest_candidates = result
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
        if (self.active is None or self.on_air_at == 0 or major or now - self.on_air_at >= 40
                or (self.active.moment and self.active.moment.expires_at_ms <= self.simulator.clock_ms)):
            self.active = result
            self.on_air_at = now
            self.services.recent_updates.append({"aired_at": now, "momentId": result.moment.id,
                                                "frame": result.moment.frame, "team": result.moment.team_id,
                                                "player": result.moment.player_id, "viewer": self.viewer.model_dump(),
                                                "headline": result.overlay.headline if result.overlay else ""})
            self.services.recent_updates = self.services.recent_updates[-10:]
            return True
        if result.producer is not None:
            result.producer.action = "SUPPRESS"
            result.producer.reason = "A graphic is already on air."
        return False

    async def reframe(self) -> Overlay | None:
        if self.active is None or self.active.moment is None or self.services.agent is None:
            return None
        from matchos.copy import render_overlay

        active, viewer = self.active, self.viewer.model_copy(deep=True)
        self.reframe_template()
        with self.telemetry.capture("Viewer reframe", {"moment": active.moment.id, "timestamp_ms": active.moment.valid_from_ms, "viewer": viewer.model_dump()}):
            text = await personalize(self.services.agent, active.moment, viewer)
        if self.active is not active or self.viewer != viewer:
            return None
        rich = render_overlay(active.moment, viewer, rich=True, provider=self.services.language_provider)
        rich.body = text
        self.active.overlay = rich
        return rich

    def reframe_template(self) -> Overlay | None:
        if self.active is None or self.active.moment is None:
            return None
        from matchos.copy import render_overlay

        fast = render_overlay(self.active.moment, self.viewer, rich=False, provider="template")
        self.active.overlay = fast
        return fast
