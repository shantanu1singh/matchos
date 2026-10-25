"""Player steering, event-to-motion planning and ball physics."""

from __future__ import annotations

import math

from matchos.animation import plan_arrival
from matchos.pitch import (
    DEF_ROLES,
    PITCH_X,
    PITCH_Y,
    clamp,
)
from matchos.schemas import (
    Point,
    TeamId,
)
from matchos.simulation.restarts import RestartSystem
from matchos.simulation.world import Flight, MatchWorld, SimPlayer


class MovementSystem:
    def __init__(self, world: MatchWorld, restarts: RestartSystem) -> None:
        self.world = world
        self.restarts = restarts

    def move_players(self, dt_s: float) -> None:
        flight = self.world.flight
        pushes = self._separation_velocity()
        for player in self.world.on_pitch():
            focused = self.world.focus.get(player.player_id)
            collecting = (
                player.player_id == self.world.carrier_id and flight is None and not self.touching(player)
            )
            if flight is not None and player.player_id == flight.receiver_id:
                tx, ty = flight.end.x, flight.end.y
                remain = max(0.22, (flight.started_ms + flight.duration_ms - self.world.clock_ms) / 1000.0)
                top = min(7.2, max(4.2, math.hypot(tx - player.x, ty - player.y) / remain))
                self._steer(player, tx, ty, dt_s, top, 0.0, 0.0, arrive=True)
                continue
            elif collecting:
                tx, ty = self.world.ball.x, self.world.ball.y
                top = 6.2
                self.world.focus.pop(player.player_id, None)
            elif focused is not None:
                tx, ty = focused
                top = 6.4
            elif (
                player.player_id == self.world.carrier_id
                and self.world.carry_target is not None
                and self.world.clock_ms < self.world.carry_until_ms
            ):
                tx, ty = self.world.carry_target.x, self.world.carry_target.y
                top = 5.0
            else:
                # Formation is a soft zone, not a point every player is magnetized
                # toward. Players inside their tactical zone shed velocity and hold
                # shape; only players who drift outside the zone recover toward its
                # nearest edge. Event-specific movement above still takes priority.
                anchor_x, anchor_y = self.world.formation_target(player)
                dx, dy = anchor_x - player.x, anchor_y - player.y
                dist = math.hypot(dx, dy)
                zone = (
                    3.8
                    if player.role in DEF_ROLES
                    else (4.8 if player.role in {"CDM", "RCM", "LCM"} else 5.8)
                )
                if player.role == "GK":
                    zone = 2.2
                if dist <= zone:
                    tx, ty = player.x, player.y
                    top = 2.0
                else:
                    ux, uy = dx / dist, dy / dist
                    tx = anchor_x - ux * zone
                    ty = anchor_y - uy * zone
                    top = 3.2 + player.speed * 0.7
                if player.player_id == self.world.carrier_id and flight is None:
                    top = min(top, 4.2)
            if player.role == "GK":
                top = min(top, 4.0)
            if player.player_id in self.world.focus or (
                flight is not None and player.player_id == flight.receiver_id
            ):
                push_x, push_y = 0.0, 0.0
            else:
                push_x, push_y = pushes.get(player.player_id, (0.0, 0.0))
            self._steer(player, tx, ty, dt_s, top, push_x, push_y)

    def _steer(
        self,
        player: SimPlayer,
        tx: float,
        ty: float,
        dt_s: float,
        top: float,
        push_x: float,
        push_y: float,
        arrive: bool = False,
    ) -> None:
        """Accelerate toward a point. Direction changes take time, the way a player actually turns."""
        dx, dy = tx - player.x, ty - player.y
        dist = math.hypot(dx, dy)
        if dist < 0.25:
            desired_vx, desired_vy = push_x, push_y
            face_x, face_y = player.hx, player.hy
        else:
            # A player sent to meet the ball keeps their pace until they are on it.
            cruise = top if arrive else min(top, max(1.15, dist * 1.7))
            desired_vx = dx / dist * cruise + push_x
            desired_vy = dy / dist * cruise + push_y
            face_x, face_y = dx, dy
        mag = math.hypot(desired_vx, desired_vy)
        cap = top + 1.4
        if mag > cap:
            desired_vx *= cap / mag
            desired_vy *= cap / mag
        accel = 6.5 if player.role == "GK" else 9.5
        dvx, dvy = desired_vx - player.vx, desired_vy - player.vy
        dv = math.hypot(dvx, dvy)
        max_dv = accel * dt_s
        if dv > max_dv > 0:
            player.vx += dvx / dv * max_dv
            player.vy += dvy / dv * max_dv
        else:
            player.vx, player.vy = desired_vx, desired_vy
        player.x = clamp(player.x + player.vx * dt_s, 1.0, PITCH_X - 1.0)
        player.y = clamp(player.y + player.vy * dt_s, 1.5, PITCH_Y - 1.5)
        if player.x <= 1.05 and player.vx < 0:
            player.vx = 0.0
        if player.x >= PITCH_X - 1.05 and player.vx > 0:
            player.vx = 0.0
        if player.y <= 1.55 and player.vy < 0:
            player.vy = 0.0
        if player.y >= PITCH_Y - 1.55 and player.vy > 0:
            player.vy = 0.0
        speed = math.hypot(player.vx, player.vy)
        if speed > 0.9:
            face_x, face_y = player.vx, player.vy
        self._turn_facing(player, face_x, face_y, dt_s)

    def _turn_facing(self, player: SimPlayer, face_x: float, face_y: float, dt_s: float) -> None:
        mag = math.hypot(face_x, face_y)
        if mag < 0.05:
            return
        tx, ty = face_x / mag, face_y / mag
        dot = clamp(player.hx * tx + player.hy * ty, -1.0, 1.0)
        cross = player.hx * ty - player.hy * tx
        angle = math.atan2(cross, dot)
        max_turn = 5.0 * dt_s
        angle = clamp(angle, -max_turn, max_turn)
        cosine, sine = math.cos(angle), math.sin(angle)
        hx, hy = player.hx, player.hy
        player.hx = hx * cosine - hy * sine
        player.hy = hx * sine + hy * cosine
        norm = math.hypot(player.hx, player.hy) or 1.0
        player.hx /= norm
        player.hy /= norm

    def _separation_velocity(self) -> dict[str, tuple[float, float]]:
        """A soft push so players slide apart instead of popping to a new spot."""
        players = self.world.on_pitch()
        out = {player.player_id: [0.0, 0.0] for player in players}
        min_sep = 2.7
        for i, a in enumerate(players):
            for b in players[i + 1 :]:
                dx, dy = b.x - a.x, b.y - a.y
                dist = math.hypot(dx, dy)
                if dist >= min_sep or dist <= 0.001:
                    continue
                push = min(2.0, (min_sep - dist) * 1.35)
                ux, uy = dx / dist, dy / dist
                if a.role != "GK":
                    out[a.player_id][0] -= ux * push
                    out[a.player_id][1] -= uy * push
                if b.role != "GK":
                    out[b.player_id][0] += ux * push
                    out[b.player_id][1] += uy * push
        return {key: (value[0], value[1]) for key, value in out.items()}

    def touching(self, player: SimPlayer) -> bool:
        return math.hypot(player.x - self.world.ball.x, player.y - self.world.ball.y) < 2.6

    def arrival_done(self) -> bool:
        """The last event has been shown, so another one may be generated."""
        if self.world.carrier_id is None or self.world.carrier_id not in self.world.players:
            return True
        return self.touching(self.world.players[self.world.carrier_id])

    def realize(
        self,
        runner_id: str | None,
        start: Point,
        desired: Point,
        kind: str,
        team: TeamId,
        success: bool,
        pace: float,
        *,
        pull: bool = True,
        max_s: float = 2.7,
    ) -> Flight:
        """Send the ball to a point, and the named player to that same point."""
        if runner_id and runner_id in self.world.players:
            runner = self.world.players[runner_id]
            end_x, end_y, duration = plan_arrival(
                runner.x, runner.y, desired.x, desired.y, start.x, start.y, pace, pull=pull, max_s=max_s
            )
            self.world.focus[runner_id] = (end_x, end_y)
        else:
            runner_id = None
            end_x, end_y, duration = plan_arrival(
                start.x, start.y, desired.x, desired.y, start.x, start.y, pace, pull=False, max_s=max_s
            )
        end = Point(x=clamp(end_x, 0.4, 104.6), y=clamp(end_y, 0.4, 67.6))
        if runner_id:
            self.world.focus[runner_id] = (end.x, end.y)
        self.world.flight = Flight(start, end, self.world.clock_ms, duration, kind, team, runner_id, success)
        self.world.carrier_id = None
        return self.world.flight

    def move_ball(self, dt_s: float) -> None:
        if self.world.flight is not None:
            flight = self.world.flight
            elapsed = self.world.clock_ms - flight.started_ms
            linear = clamp(elapsed / max(1, flight.duration_ms), 0.0, 1.0)
            # Smoothstep: the kick eases away and the ball settles into the receiver.
            t = linear * linear * (3.0 - 2.0 * linear)
            nx = flight.start.x + (flight.end.x - flight.start.x) * t
            ny = flight.start.y + (flight.end.y - flight.start.y) * t
            if dt_s > 0:
                self.world.ball_vx = (nx - self.world.ball.x) / dt_s
                self.world.ball_vy = (ny - self.world.ball.y) / dt_s
            self.world.ball = Point(x=nx, y=ny)
            lift = 2.4 if flight.kind == "SHOT" else 0.5
            self.world.ball_z = math.sin(linear * math.pi) * lift
            if linear >= 1.0:
                self.restarts.land(flight)
            return
        carrier = self.world.players.get(self.world.carrier_id) if self.world.carrier_id else None
        if carrier is not None and carrier.on_pitch and self.touching(carrier):
            ideal = self.world.ball_at_feet(carrier)
            self._spring_ball(ideal.x, ideal.y, dt_s)
            self.world.ball_z += (0.0 - self.world.ball_z) * min(1.0, dt_s * 8.0)
            return
        self.world.ball_vx *= math.exp(-1.8 * dt_s)
        self.world.ball_vy *= math.exp(-1.8 * dt_s)
        self.world.ball = Point(
            x=clamp(self.world.ball.x + self.world.ball_vx * dt_s, 0.4, 104.6),
            y=clamp(self.world.ball.y + self.world.ball_vy * dt_s, 0.4, 67.6),
        )
        self.world.ball_z += (0.0 - self.world.ball_z) * min(1.0, dt_s * 6.0)

    def _spring_ball(self, ix: float, iy: float, dt_s: float) -> None:
        stiffness, damping = 42.0, 13.0
        ax = (ix - self.world.ball.x) * stiffness - self.world.ball_vx * damping
        ay = (iy - self.world.ball.y) * stiffness - self.world.ball_vy * damping
        self.world.ball_vx += ax * dt_s
        self.world.ball_vy += ay * dt_s
        speed = math.hypot(self.world.ball_vx, self.world.ball_vy)
        if speed > 12.0:
            self.world.ball_vx *= 12.0 / speed
            self.world.ball_vy *= 12.0 / speed
        self.world.ball = Point(
            x=clamp(self.world.ball.x + self.world.ball_vx * dt_s, 0.4, 104.6),
            y=clamp(self.world.ball.y + self.world.ball_vy * dt_s, 0.4, 67.6),
        )
