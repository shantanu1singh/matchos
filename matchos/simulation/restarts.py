"""Resolve arrivals, possession changes and dead-ball restarts."""

from __future__ import annotations

from matchos.pitch import (
    other,
)
from matchos.schemas import (
    Point,
    TeamId,
)
from matchos.simulation.world import Flight, MatchWorld


class RestartSystem:
    def __init__(self, world: MatchWorld) -> None:
        self.world = world

    def land(self, flight: Flight) -> None:
        self.world.flight = None
        self.world.focus.clear()
        self.world.ball = flight.end
        self.world.ball_vx *= 0.2
        self.world.ball_vy *= 0.2
        self.world.ball_z = 0.0
        if self.world.pending_restart is not None:
            self.apply()
            return
        if flight.kind == "SHOT":
            if self.world.carrier_id is None:
                self.kickoff()
            return
        if flight.kind == "RESTART" and flight.receiver_id in self.world.players:
            self.world.carrier_id = flight.receiver_id
            self.world.possession = flight.team_id
            self.world.next_action_ms = self.world.clock_ms + 500
            return
        if (
            flight.receiver_id
            and flight.receiver_id in self.world.players
            and self.world.players[flight.receiver_id].on_pitch
        ):
            winner = self.world.players[flight.receiver_id]
            if winner.team_id != flight.team_id or not flight.success:
                self.world.possession = winner.team_id
                self.world.possession_id += 1
                self.world.next_action_ms = self.world.clock_ms + int(self.world.rng.randint(400, 900))
            else:
                self.world.possession = flight.team_id
                self.world.next_action_ms = max(self.world.next_action_ms, self.world.clock_ms + 480)
            self.world.carrier_id = winner.player_id
            return
        self.turnover(flight.end, flight.team_id)

    def kickoff(self) -> None:
        """Reset a dead ball at centre; never animate it flying out of the net."""
        centre = Point(x=52.5, y=34.0)
        self.world.flight = None
        self.world.focus.clear()
        self.world.carry_target = None
        self.world.carry_until_ms = 0
        self.world.pending_penalty_taker = None
        self.world.pending_restart = None
        self.world.ball = centre
        self.world.ball_vx = 0.0
        self.world.ball_vy = 0.0
        self.world.ball_z = 0.0
        taker_id = self.world.nearest_actual(self.world.possession, centre)
        if taker_id is None:
            return
        self.world.carrier_id = taker_id
        self.world.focus[taker_id] = (centre.x, centre.y)
        # _arrival_done keeps open play paused until the taker has actually
        # reached the stationary ball.
        self.world.next_action_ms = self.world.clock_ms + 900

    def schedule(self, kind: str, team: TeamId, spot: Point, taker_id: str | None = None) -> str | None:
        """Queue a dead-ball restart to be applied when the current ball flight ends."""
        taker = taker_id or self.world.nearest_actual(team, spot)
        self.world.pending_restart = (kind, team, spot, taker)
        if taker:
            self.world.focus[taker] = (spot.x, spot.y)
        return taker

    def apply(self) -> None:
        pending = self.world.pending_restart
        self.world.pending_restart = None
        if pending is None:
            return
        _kind, team, spot, taker = pending
        self.world.possession = team
        self.world.possession_id += 1
        self.world.ball = Point(x=spot.x, y=spot.y)
        self.world.ball_vx = 0.0
        self.world.ball_vy = 0.0
        self.world.ball_z = 0.0
        self.world.carrier_id = taker or self.world.nearest_actual(team, spot)
        self.world.focus.clear()
        if self.world.carrier_id:
            self.world.focus[self.world.carrier_id] = (spot.x, spot.y)
        self.world.next_action_ms = self.world.clock_ms + 800

    def turnover(self, spot: Point, previous: TeamId) -> None:
        self.world.possession = other(previous)
        self.world.possession_id += 1
        self.world.carrier_id = self.world.nearest_actual(self.world.possession, spot)
        if self.world.carrier_id:
            self.world.focus[self.world.carrier_id] = (spot.x, spot.y)
        self.world.next_action_ms = self.world.clock_ms + int(self.world.rng.randint(400, 900))
