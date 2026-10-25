"""Seeded match simulation: a world advanced by movement, events and restart systems.

The event policy is authoritative; movement animates those named events. Keeping
one shared world and RNG preserves repeatable warmup, seeking and live playback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from matchos.schemas import LineSample, MatchEvent, Tick
from matchos.simulation.events import EventGenerator
from matchos.simulation.movement import MovementSystem
from matchos.simulation.restarts import RestartSystem
from matchos.simulation.world import SUBSTEP_MS, MatchWorld


@dataclass
class MatchSimulator(MatchWorld):
    movement: MovementSystem = field(init=False)
    restarts: RestartSystem = field(init=False)
    events: EventGenerator = field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.restarts = RestartSystem(self)
        self.movement = MovementSystem(self, self.restarts)
        self.events = EventGenerator(self, self.movement, self.restarts)

    def collect_warmup(self) -> list[Tick]:
        """Play the hidden minutes and return them so rolling state can warm up."""
        target = int(self.scenario.start_minute * 60_000)
        ticks: list[Tick] = []
        while self.clock_ms < target:
            ticks.append(self._emit_tick(min(1000, target - self.clock_ms)))
        self.live = True
        return ticks

    def step(self, dt_ms: int) -> Tick:
        self.live = True
        if self.clock_ms >= 95 * 60_000:
            return Tick(
                match_id=self.match_id,
                events=[],
                frame=self.frame(),
                samples=[self.line_sample()],
            )
        return self._emit_tick(max(1, dt_ms))

    def _emit_tick(self, dt_ms: int) -> Tick:
        events: list[MatchEvent] = []
        samples: list[LineSample] = []
        remaining = max(1, dt_ms)
        while remaining > 0:
            chunk = min(SUBSTEP_MS, remaining)
            events.extend(self._advance(chunk))
            if self.clock_ms % 500 < chunk:
                samples.append(self.line_sample())
            remaining -= chunk
        if not samples:
            samples.append(self.line_sample())
        return Tick(
            match_id=self.match_id,
            events=events,
            frame=self.frame(),
            samples=samples,
        )

    def _advance(self, dt_ms: int) -> list[MatchEvent]:
        self.clock_ms += dt_ms
        events: list[MatchEvent] = []
        events.extend(self.events.scripted())
        dt_s = dt_ms / 1000.0
        self.movement.move_players(dt_s)
        self.movement.move_ball(dt_s)
        if self.carry_target is not None and self.clock_ms >= self.carry_until_ms:
            self.carry_target = None
        if self.flight is None and (
            self.carrier_id is None
            or self.carrier_id not in self.players
            or not self.players[self.carrier_id].on_pitch
        ):
            self.carrier_id = self.nearest_actual(self.possession, self.ball)
        # The next event waits until the previous one has been shown. The dots
        # do not choose it; they only have to finish arriving.
        if (
            self.flight is None
            and self.clock_ms >= self.carry_until_ms
            and self.clock_ms >= self.next_action_ms
            and self.movement.arrival_done()
        ):
            if self.pending_penalty_taker is not None:
                events.extend(self.events.take_penalty())
            else:
                events.extend(self.events.generate())
        return events
