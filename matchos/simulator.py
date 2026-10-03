"""Event generator plus an animation planner.

The seeded possession policy is the source of truth: it decides who passes to
whom, and whether that pass is complete. It reads the formation, not the
drift of the dots. The animation planner then moves the named players and the
ball so the pitch shows that event. Everyone else eases toward a
formation-relative spot.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from matchos.animation import plan_arrival
from matchos.pitch import (
    ANCHORS,
    DEF_ROLES,
    PITCH_X,
    PITCH_Y,
    ROSTER,
    attack_x,
    clamp,
    defensive_line_height,
    defensive_line_x,
    distance,
    first_line_x,
    in_box,
    in_final_third,
    other,
    period_of,
    to_absolute,
)
from matchos.scenarios import Scenario, get_scenario, progression_bias, style_for
from matchos.schemas import (
    BallAction,
    LineSample,
    MatchEvent,
    Point,
    TeamId,
    Tick,
    TrackingFrame,
    TrackingPlayer,
)

SUBSTEP_MS = 50


@dataclass
class SimPlayer:
    player_id: str
    team_id: TeamId
    number: int
    role: str
    x: float
    y: float
    passing: float
    shooting: float
    speed: float
    dribbling: float
    pressing: float
    on_pitch: bool = True
    vx: float = 0.0
    vy: float = 0.0
    hx: float = 1.0
    hy: float = 0.0


@dataclass
class Flight:
    start: Point
    end: Point
    started_ms: int
    duration_ms: int
    kind: str
    team_id: TeamId
    receiver_id: str | None
    success: bool


@dataclass
class MatchSimulator:
    seed: int
    scenario_id: str
    scenario: Scenario = field(init=False)
    rng: random.Random = field(init=False)
    match_id: str = field(init=False)
    players: dict[str, SimPlayer] = field(init=False)
    clock_ms: int = 0
    score_red: int = 0
    score_blue: int = 0
    possession: TeamId = "RED"
    possession_id: int = 1
    carrier_id: str | None = None
    ball: Point = field(default_factory=lambda: Point(x=52.5, y=34.0))
    ball_z: float = 0.0
    ball_vx: float = 0.0
    ball_vy: float = 0.0
    flight: Flight | None = None
    carry_target: Point | None = None
    carry_until_ms: int = 0
    next_action_ms: int = 400
    focus: dict[str, tuple[float, float]] = field(default_factory=dict)
    sequence: int = 0
    live: bool = False
    done_scripts: set[str] = field(default_factory=set)
    last_pressure_ms: int = -10_000

    def __post_init__(self) -> None:
        self.scenario = get_scenario(self.scenario_id)
        self.scenario_id = self.scenario.id
        self.rng = random.Random(self.seed)
        self.match_id = f"m-{self.scenario.id}-{self.seed}"
        self.players = {}
        for team in ("RED", "BLUE"):
            for role, number in ROSTER:
                skills = self._skills(team, role)
                player = SimPlayer(
                    player_id=f"{team.lower()}_{number}",
                    team_id=team,  # type: ignore[arg-type]
                    number=number,
                    role=role,
                    x=0.0,
                    y=0.0,
                    **skills,
                )
                ax, ay = ANCHORS[role]
                player.x, player.y = to_absolute(team, ax, ay)  # type: ignore[arg-type]
                player.hx = 1.0 if team == "RED" else -1.0
                self.players[player.player_id] = player
        self.score_red = self.scenario.score_red
        self.score_blue = self.scenario.score_blue
        self.clock_ms = int(self.scenario.warmup_from_minute * 60_000)
        self.possession = "RED" if self.rng.random() < 0.5 else "BLUE"
        self.carrier_id = self._nearest_actual(self.possession, Point(x=52.5, y=34.0))
        if self.carrier_id:
            carrier = self.players[self.carrier_id]
            self.ball = self._ideal_ball(carrier)
        self.next_action_ms = self.clock_ms + 600

    def _skills(self, team: str, role: str) -> dict[str, float]:
        def j(base: float) -> float:
            return clamp(base + self.rng.uniform(-0.05, 0.05), 0.35, 0.95)

        shooting = 0.78 if role in {"ST", "RW", "LW"} else 0.42
        passing = 0.8 if role in {"CDM", "RCM", "LCM", "LCB", "RCB"} else 0.7
        speed = 0.86 if role in {"RW", "LW", "LB", "RB", "ST"} else 0.62
        dribbling = 0.8 if role in {"RW", "LW", "ST", "LCM"} else 0.48
        pressing = 0.75 if role in {"ST", "RW", "LW", "LCM", "RCM"} else 0.55
        return {
            "passing": j(passing),
            "shooting": j(shooting),
            "speed": j(speed),
            "dribbling": j(dribbling),
            "pressing": j(pressing),
        }

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
                frame=self._frame(),
                samples=[self._line_sample()],
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
                samples.append(self._line_sample())
            remaining -= chunk
        if not samples:
            samples.append(self._line_sample())
        return Tick(
            match_id=self.match_id,
            events=events,
            frame=self._frame(),
            samples=samples,
        )

    def _advance(self, dt_ms: int) -> list[MatchEvent]:
        self.clock_ms += dt_ms
        events: list[MatchEvent] = []
        events.extend(self._scripts())
        dt_s = dt_ms / 1000.0
        self._move_players(dt_s)
        self._move_ball(dt_s)
        if self.carry_target is not None and self.clock_ms >= self.carry_until_ms:
            self.carry_target = None
        if self.flight is None and (self.carrier_id is None or self.carrier_id not in self.players or not self.players[self.carrier_id].on_pitch):
            self.carrier_id = self._nearest_actual(self.possession, self.ball)
        # The next event waits until the previous one has been shown. The dots
        # do not choose it; they only have to finish arriving.
        if self.flight is None and self.clock_ms >= self.carry_until_ms and self.clock_ms >= self.next_action_ms and self._arrival_done():
            events.extend(self._act())
        return events

    def minute(self) -> float:
        return self.clock_ms / 60_000.0

    def _on_pitch(self, team: TeamId | None = None) -> list[SimPlayer]:
        players = [p for p in self.players.values() if p.on_pitch]
        if team is not None:
            players = [p for p in players if p.team_id == team]
        return players

    def _tracking(self) -> list[TrackingPlayer]:
        rows = []
        for player in self._on_pitch():
            rows.append(
                TrackingPlayer(
                    player_id=player.player_id,
                    team_id=player.team_id,
                    number=player.number,
                    role=player.role,
                    x=round(player.x, 2),
                    y=round(player.y, 2),
                    speed_mps=round(math.hypot(player.vx, player.vy), 2),
                    vx=round(player.vx, 2),
                    vy=round(player.vy, 2),
                )
            )
        return rows

    def _frame(self) -> TrackingFrame:
        period = 1 if period_of(self.clock_ms) == 1 else 2
        return TrackingFrame(
            match_id=self.match_id,
            timestamp_ms=self.clock_ms,
            period=period,  # type: ignore[arg-type]
            ball=Point(x=round(self.ball.x, 2), y=round(self.ball.y, 2)),
            ball_z=round(self.ball_z, 2),
            ball_vx=round(self.ball_vx, 2),
            ball_vy=round(self.ball_vy, 2),
            players=self._tracking(),
            possession_team=self.possession,
            score_red=self.score_red,
            score_blue=self.score_blue,
            action=self._action(),
        )

    def _action(self) -> BallAction | None:
        flight = self.flight
        if flight is None or flight.kind not in {"PASS", "SHOT", "CARRY"}:
            return None
        return BallAction(
            kind=flight.kind,  # type: ignore[arg-type]
            team_id=flight.team_id,
            start=flight.start,
            end=flight.end,
            started_ms=flight.started_ms,
            duration_ms=flight.duration_ms,
            receiver_id=flight.receiver_id,
        )

    def _line_sample(self) -> LineSample:
        players = self._tracking()
        return LineSample(
            timestamp_ms=self.clock_ms,
            red_line_m=round(defensive_line_height(players, "RED"), 2),
            blue_line_m=round(defensive_line_height(players, "BLUE"), 2),
            ball_x=round(self.ball.x, 2),
            possession_team=self.possession,
        )

    def _style(self, team: TeamId) -> dict[str, float]:
        return style_for(self.scenario_id, self.minute(), team)

    def _target(self, player: SimPlayer) -> tuple[float, float]:
        style = self._style(player.team_id)
        ax, ay = ANCHORS[player.role]
        ball_ax = attack_x(player.team_id, self.ball.x)
        possessing = self.possession == player.team_id
        if player.role == "GK":
            ax = clamp(0.035 + ball_ax * 0.05, 0.02, 0.12)
        else:
            if possessing:
                shift = (ball_ax - 0.52) * 0.22
            else:
                shift = (ball_ax - 0.72) * 0.18
            shift += (style["defensive_line"] - 0.62) * 0.10
            if player.role in DEF_ROLES:
                ax = clamp(0.20 + style["defensive_line"] * 0.18 + shift * 0.08, 0.18, 0.48)
            else:
                ax = clamp(ax + shift * 0.45, 0.16, 0.90)
            # Wide players hold the touchline a little more when the side wants width.
            if player.role in {"LW", "RW", "LB", "RB"}:
                ay = clamp(ay + (style["width"] - 0.6) * (0.08 if ay > 0.5 else -0.08), 0.06, 0.94)
            # Ball-side attraction, stronger for the carrier's teammates.
            ball_ay = self.ball.y / PITCH_Y
            pull = 0.18 if possessing else 0.10
            if player.role in {"CDM", "RCM", "LCM", "ST"}:
                ay = clamp(ay + (ball_ay - ay) * pull, 0.08, 0.92)
        # Ten men sit a little narrower and deeper.
        if player.team_id == "BLUE" and self._blue_down_a_player() and player.role != "GK":
            ax -= 0.04
            ay = 0.5 + (ay - 0.5) * 0.86
        x, y = to_absolute(player.team_id, ax, ay)
        return clamp(x, 1.0, PITCH_X - 1.0), clamp(y, 1.5, PITCH_Y - 1.5)

    def _blue_down_a_player(self) -> bool:
        return self.scenario_id == "red_card" and self.minute() >= 36

    def _move_players(self, dt_s: float) -> None:
        flight = self.flight
        pushes = self._separation_velocity()
        for player in self._on_pitch():
            focused = self.focus.get(player.player_id)
            collecting = player.player_id == self.carrier_id and flight is None and not self._touching(player)
            if flight is not None and player.player_id == flight.receiver_id:
                tx, ty = flight.end.x, flight.end.y
                remain = max(0.22, (flight.started_ms + flight.duration_ms - self.clock_ms) / 1000.0)
                top = min(7.2, max(4.2, math.hypot(tx - player.x, ty - player.y) / remain))
                self._steer(player, tx, ty, dt_s, top, 0.0, 0.0, arrive=True)
                continue
            elif collecting:
                tx, ty = self.ball.x, self.ball.y
                top = 6.2
                self.focus.pop(player.player_id, None)
            elif focused is not None:
                tx, ty = focused
                top = 6.4
            elif player.player_id == self.carrier_id and self.carry_target is not None and self.clock_ms < self.carry_until_ms:
                tx, ty = self.carry_target.x, self.carry_target.y
                top = 5.0
            else:
                # Formation is a soft zone, not a point every player is magnetized
                # toward. Players inside their tactical zone shed velocity and hold
                # shape; only players who drift outside the zone recover toward its
                # nearest edge. Event-specific movement above still takes priority.
                anchor_x, anchor_y = self._target(player)
                dx, dy = anchor_x - player.x, anchor_y - player.y
                dist = math.hypot(dx, dy)
                zone = 3.8 if player.role in DEF_ROLES else (4.8 if player.role in {"CDM", "RCM", "LCM"} else 5.8)
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
                if player.player_id == self.carrier_id and flight is None:
                    top = min(top, 4.2)
            if player.role == "GK":
                top = min(top, 4.0)
            if player.player_id in self.focus or (flight is not None and player.player_id == flight.receiver_id):
                push_x, push_y = 0.0, 0.0
            else:
                push_x, push_y = pushes.get(player.player_id, (0.0, 0.0))
            self._steer(player, tx, ty, dt_s, top, push_x, push_y)

    def _steer(self, player: SimPlayer, tx: float, ty: float, dt_s: float, top: float, push_x: float, push_y: float, arrive: bool = False) -> None:
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
        players = self._on_pitch()
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

    def _touching(self, player: SimPlayer) -> bool:
        return math.hypot(player.x - self.ball.x, player.y - self.ball.y) < 2.6

    def _arrival_done(self) -> bool:
        """The last event has been shown, so another one may be generated."""
        if self.carrier_id is None or self.carrier_id not in self.players:
            return True
        return self._touching(self.players[self.carrier_id])

    def _slot(self, player: SimPlayer) -> Point:
        x, y = self._target(player)
        return Point(x=x, y=y)

    def _slot_nearest(self, team: TeamId, spot: Point) -> str | None:
        """Nearest tactical role slot. Use only when resetting/initializing shape."""
        choices = self._on_pitch(team)
        if not choices:
            return None
        return min(choices, key=lambda p: (self._slot(p).x - spot.x) ** 2 + (self._slot(p).y - spot.y) ** 2).player_id

    def _nearest_actual(self, team: TeamId, spot: Point, *, exclude: str | None = None) -> str | None:
        """Nearest player in the current physical world."""
        choices = [p for p in self._on_pitch(team) if p.player_id != exclude]
        if not choices:
            return None
        return min(choices, key=lambda p: (p.x - spot.x) ** 2 + (p.y - spot.y) ** 2).player_id

    def _realize(
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
        if runner_id and runner_id in self.players:
            runner = self.players[runner_id]
            end_x, end_y, duration = plan_arrival(
                runner.x, runner.y, desired.x, desired.y, start.x, start.y, pace, pull=pull, max_s=max_s
            )
            self.focus[runner_id] = (end_x, end_y)
        else:
            runner_id = None
            end_x, end_y, duration = plan_arrival(
                start.x, start.y, desired.x, desired.y, start.x, start.y, pace, pull=False, max_s=max_s
            )
        end = Point(x=clamp(end_x, 0.4, 104.6), y=clamp(end_y, 0.4, 67.6))
        if runner_id:
            self.focus[runner_id] = (end.x, end.y)
        self.flight = Flight(start, end, self.clock_ms, duration, kind, team, runner_id, success)
        self.carrier_id = None
        return self.flight

    def _move_ball(self, dt_s: float) -> None:
        if self.flight is not None:
            flight = self.flight
            elapsed = self.clock_ms - flight.started_ms
            linear = clamp(elapsed / max(1, flight.duration_ms), 0.0, 1.0)
            # Smoothstep: the kick eases away and the ball settles into the receiver.
            t = linear * linear * (3.0 - 2.0 * linear)
            nx = flight.start.x + (flight.end.x - flight.start.x) * t
            ny = flight.start.y + (flight.end.y - flight.start.y) * t
            if dt_s > 0:
                self.ball_vx = (nx - self.ball.x) / dt_s
                self.ball_vy = (ny - self.ball.y) / dt_s
            self.ball = Point(x=nx, y=ny)
            lift = 2.4 if flight.kind == "SHOT" else 0.5
            self.ball_z = math.sin(linear * math.pi) * lift
            if linear >= 1.0:
                self._land(flight)
            return
        carrier = self.players.get(self.carrier_id) if self.carrier_id else None
        if carrier is not None and carrier.on_pitch and self._touching(carrier):
            ideal = self._ideal_ball(carrier)
            self._spring_ball(ideal.x, ideal.y, dt_s)
            self.ball_z += (0.0 - self.ball_z) * min(1.0, dt_s * 8.0)
            return
        self.ball_vx *= math.exp(-1.8 * dt_s)
        self.ball_vy *= math.exp(-1.8 * dt_s)
        self.ball = Point(
            x=clamp(self.ball.x + self.ball_vx * dt_s, 0.4, 104.6),
            y=clamp(self.ball.y + self.ball_vy * dt_s, 0.4, 67.6),
        )
        self.ball_z += (0.0 - self.ball_z) * min(1.0, dt_s * 6.0)

    def _ideal_ball(self, carrier: SimPlayer) -> Point:
        return Point(
            x=clamp(carrier.x + carrier.hx * 1.55, 0.4, 104.6),
            y=clamp(carrier.y + carrier.hy * 1.55, 0.4, 67.6),
        )

    def _spring_ball(self, ix: float, iy: float, dt_s: float) -> None:
        stiffness, damping = 42.0, 13.0
        ax = (ix - self.ball.x) * stiffness - self.ball_vx * damping
        ay = (iy - self.ball.y) * stiffness - self.ball_vy * damping
        self.ball_vx += ax * dt_s
        self.ball_vy += ay * dt_s
        speed = math.hypot(self.ball_vx, self.ball_vy)
        if speed > 12.0:
            self.ball_vx *= 12.0 / speed
            self.ball_vy *= 12.0 / speed
        self.ball = Point(
            x=clamp(self.ball.x + self.ball_vx * dt_s, 0.4, 104.6),
            y=clamp(self.ball.y + self.ball_vy * dt_s, 0.4, 67.6),
        )

    def _land(self, flight: Flight) -> None:
        self.flight = None
        self.focus.clear()
        self.ball = flight.end
        self.ball_vx *= 0.2
        self.ball_vy *= 0.2
        self.ball_z = 0.0
        if flight.kind == "SHOT":
            if self.carrier_id is None:
                self._kickoff()
            return
        if flight.kind == "RESTART" and flight.receiver_id in self.players:
            self.carrier_id = flight.receiver_id
            self.possession = flight.team_id
            self.next_action_ms = self.clock_ms + 500
            return
        if flight.receiver_id and flight.receiver_id in self.players and self.players[flight.receiver_id].on_pitch:
            winner = self.players[flight.receiver_id]
            if winner.team_id != flight.team_id or not flight.success:
                self.possession = winner.team_id
                self.possession_id += 1
                self.next_action_ms = self.clock_ms + int(self.rng.randint(400, 900))
            else:
                self.possession = flight.team_id
                self.next_action_ms = max(self.next_action_ms, self.clock_ms + 480)
            self.carrier_id = winner.player_id
            return
        self._turnover(flight.end, flight.team_id)

    def _kickoff(self) -> None:
        """A goal leaves the ball in the net. A named player restarts from the centre."""
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.ball_z = 0.0
        taker_id = f"{self.possession.lower()}_8"
        if taker_id not in self.players or not self.players[taker_id].on_pitch:
            taker_id = self._nearest_actual(self.possession, Point(x=52.5, y=34.0))
        if taker_id is None:
            return
        start = Point(x=self.ball.x, y=self.ball.y)
        flight = self._realize(taker_id, start, Point(x=52.5, y=34.0), "RESTART", self.possession, True, 12.0, pull=False, max_s=4.5)
        self.next_action_ms = self.clock_ms + flight.duration_ms + 500

    def _turnover(self, spot: Point, previous: TeamId) -> None:
        self.possession = other(previous)
        self.possession_id += 1
        self.carrier_id = self._nearest_actual(self.possession, spot)
        if self.carrier_id:
            self.focus[self.carrier_id] = (spot.x, spot.y)
        self.next_action_ms = self.clock_ms + int(self.rng.randint(400, 900))

    def _act(self) -> list[MatchEvent]:
        if self.carrier_id is None or not self.players[self.carrier_id].on_pitch:
            self.carrier_id = self._nearest_actual(self.possession, self.ball)
        if self.carrier_id is None:
            return []
        self.focus.clear()
        carrier = self.players[self.carrier_id]
        events: list[MatchEvent] = []
        if self._pressers(carrier) >= 2 and self.clock_ms - self.last_pressure_ms > 2500:
            events.append(self._emit("PRESSURE", other(carrier.team_id), self._slot_nearest(other(carrier.team_id), self._slot(carrier)), None, "APPLIED"))
            self.last_pressure_ms = self.clock_ms
        kind = self._choose_action(carrier)
        if kind == "SHOT":
            events.extend(self._shot(carrier))
        elif kind == "CARRY":
            events.extend(self._carry(carrier))
        elif kind == "DUEL":
            events.extend(self._duel(carrier))
        elif kind == "LONG_BALL":
            events.extend(self._pass(carrier, long_ball=True))
        else:
            events.extend(self._pass(carrier, long_ball=False))
        tempo = self._style(self.possession)["tempo"]
        gap = int(self.rng.randint(700, 1600) * (1.25 - tempo * 0.45))
        self.next_action_ms = self.clock_ms + gap
        return events

    def _choose_action(self, carrier: SimPlayer) -> str:
        ax = attack_x(carrier.team_id, carrier.x)
        style = self._style(carrier.team_id)
        weights = {
            "PASS": 0.60,
            "CARRY": 0.16 + carrier.dribbling * 0.08,
            "LONG_BALL": 0.06 + style["directness"] * 0.08,
            "DUEL": 0.05,
            "SHOT": 0.02,
        }
        if ax > 0.72:
            weights["SHOT"] = 0.16 + carrier.shooting * 0.12
            weights["PASS"] = 0.48
        elif ax < 0.30:
            weights["LONG_BALL"] += 0.1
            weights["SHOT"] = 0.0
        if self.scenario_id == "substitute_impact" and carrier.player_id == "red_17":
            weights["CARRY"] += 0.22
            weights["SHOT"] += 0.05
        return self._weighted(weights)

    def _weighted(self, weights: dict[str, float]) -> str:
        total = sum(max(0.0, v) for v in weights.values())
        roll = self.rng.random() * total
        upto = 0.0
        for key, value in weights.items():
            upto += max(0.0, value)
            if roll <= upto:
                return key
        return "PASS"

    def _pass(self, carrier: SimPlayer, long_ball: bool) -> list[MatchEvent]:
        target = self._select_target(carrier, long_ball)
        if target is None:
            return self._carry(carrier)
        start = Point(x=self.ball.x, y=self.ball.y)
        end = self._receive_point(target, carrier.team_id)
        length = distance(start, end)
        pressure = self._pressure_modifier(carrier)
        qual = self._geometry(carrier.team_id)
        success_p = self._pass_success(carrier, target, length, pressure, qual, long_ball, end)
        success = self.rng.random() < success_p
        outcome = "COMPLETE" if success else ("OUT" if self.rng.random() < 0.25 else "INCOMPLETE")
        runner_id = target.player_id
        desired = end
        challenge = self._named_interceptor(start, end, carrier.team_id) if outcome == "INCOMPLETE" else None
        if challenge is not None:
            desired, runner_id = challenge
        pace = 12.0 if not long_ball else 14.0
        if outcome == "OUT":
            desired = Point(x=clamp(end.x, 0.4, 104.6), y=0.4 if end.y < 34 else 67.6)
            runner_id = self._slot_nearest(other(carrier.team_id), desired)
            flight = self._realize(runner_id, start, desired, "PASS", carrier.team_id, False, pace, pull=False, max_s=3.2)
        else:
            flight = self._realize(runner_id, start, desired, "PASS", carrier.team_id, success, pace)
        shown_end = flight.end
        event = self._emit(
            "PASS",
            carrier.team_id,
            carrier.player_id,
            target.player_id if success or outcome == "INCOMPLETE" else None,
            outcome,
            start,
            shown_end,
            {
                **qual,
                "length_m": round(length, 1),
                "long_ball": long_ball,
                "pressure": round(pressure, 2),
                "entered_final_third": (not in_final_third(carrier.team_id, start.x))
                and in_final_third(carrier.team_id, shown_end.x)
                and success,
            },
        )
        events = [event]
        caught = outcome == "INCOMPLETE" and self.rng.random() < 0.7 and challenge is not None
        if caught:
            events.append(
                self._emit(
                    "INTERCEPTION",
                    other(carrier.team_id),
                    runner_id,
                    None,
                    "WON",
                    shown_end,
                    shown_end,
                    {"related": event.id},
                )
            )
        return events

    def _named_interceptor(self, start: Point, end: Point, attacking: TeamId) -> tuple[Point, str] | None:
        """The defender whose shape stands on this pass. None if the lane is clear."""
        dx, dy = end.x - start.x, end.y - start.y
        length2 = dx * dx + dy * dy
        if length2 < 36:
            return None
        best: tuple[float, str, Point] | None = None
        for player in self._on_pitch(other(attacking)):
            if player.role == "GK":
                continue
            # Interceptions belong to defenders who are actually near the lane.
            px, py = player.x, player.y
            t = ((px - start.x) * dx + (py - start.y) * dy) / length2
            t = clamp(t, 0.28, 0.82)
            point = Point(x=start.x + dx * t, y=start.y + dy * t)
            dist = math.hypot(px - point.x, py - point.y)
            if best is None or dist < best[0]:
                best = (dist, player.player_id, point)
        if best is None or best[0] > 8.0:
            return None
        return best[2], best[1]

    def _select_target(self, carrier: SimPlayer, long_ball: bool) -> SimPlayer | None:
        mates = [p for p in self._on_pitch(carrier.team_id) if p.player_id != carrier.player_id and p.role != "GK"]
        if long_ball:
            mates = [p for p in mates if p.role in {"ST", "RW", "LW", "LCM", "RCM"}] or mates
        if not mates:
            return None
        bias = progression_bias(self.scenario_id, self.minute(), carrier.team_id)
        defending = other(carrier.team_id)
        line = first_line_x(self._tracking(), defending)
        origin = Point(x=carrier.x, y=carrier.y)
        scored: list[tuple[float, SimPlayer]] = []
        for mate in mates:
            spot = Point(x=mate.x, y=mate.y)
            forward = attack_x(carrier.team_id, spot.x) - attack_x(carrier.team_id, origin.x)
            length = math.hypot(spot.x - origin.x, spot.y - origin.y)
            if length < 4 or length > (48 if long_ball else 36):
                continue
            score = 0.25 + max(0.0, forward) * 1.4
            score -= abs(length - (28 if long_ball else 16)) * 0.03
            score -= self._nearby_opponents(spot, carrier.team_id, 6.0) * 0.18
            # Keep tactical role as a weak prior, while current physical position
            # determines whether the pass is actually feasible.
            slot = self._slot(mate)
            score -= min(0.18, math.hypot(slot.x - spot.x, slot.y - spot.y) * 0.008)
            breaks = (spot.x > line + 3 and carrier.team_id == "RED") or (spot.x < line - 3 and carrier.team_id == "BLUE")
            if breaks:
                score += bias
            if self.scenario_id == "press_collapse" and mate.player_id == "red_7" and self.minute() >= 57:
                score += 0.45
            if self.scenario_id == "substitute_impact" and mate.player_id == "red_17" and self.minute() >= 65:
                score += 0.55
            scored.append((score, mate))
        if not scored:
            return self.rng.choice(mates)
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:3]
        weights = [max(0.05, item[0]) for item in top]
        return top[self._pick(weights)][1]

    def _pick(self, weights: list[float]) -> int:
        total = sum(weights)
        roll = self.rng.random() * total
        upto = 0.0
        for i, weight in enumerate(weights):
            upto += weight
            if roll <= upto:
                return i
        return len(weights) - 1

    def _receive_point(self, target: SimPlayer, team: TeamId) -> Point:
        """Lead the receiver from their current position, not an abstract formation slot."""
        lead_s = 0.55
        forward = 2.8 if team == "RED" else -2.8
        return Point(
            x=clamp(target.x + target.vx * lead_s + forward + self.rng.uniform(-0.8, 0.8), 2.0, 103.0),
            y=clamp(target.y + target.vy * lead_s + self.rng.uniform(-1.0, 1.0), 3.0, 65.0),
        )

    def _pass_success(
        self,
        carrier: SimPlayer,
        target: SimPlayer,
        length: float,
        pressure: float,
        qual: dict[str, object],
        long_ball: bool,
        end: Point,
    ) -> float:
        distance_mod = 1.05 - min(0.55, length / 70.0)
        angle_mod = 0.85 + 0.15 * (1.0 - min(1.0, abs(carrier.y - target.y) / 40.0))
        availability = 1.0 - min(0.45, self._nearby_opponents(Point(x=target.x, y=target.y), carrier.team_id, 5.5) * 0.15)
        pressure_mod = 1.0 - 0.45 * pressure
        p = carrier.passing * distance_mod * angle_mod * availability * pressure_mod
        if long_ball:
            p *= 0.82
        defending = other(carrier.team_id)
        line = float(qual["defending_first_line_x"])
        would_break = (carrier.team_id == "RED" and end.x > line + 4) or (carrier.team_id == "BLUE" and end.x < line - 4)
        minute = self.minute()
        if self.scenario_id == "press_collapse" and carrier.team_id == "RED" and would_break:
            # Through-balls stay difficult until Blue's block has had time to drop.
            if minute < 57:
                p = min(p, 0.32)
            else:
                t = min(1.0, (minute - 57.0) / 1.2)
                p = max(p, 0.74 + 0.16 * t)
        if self.scenario_id == "red_card" and carrier.team_id == "RED" and minute >= 36 and would_break:
            p = max(p, 0.72)
        return clamp(p, 0.08, 0.93)

    def _carry(self, carrier: SimPlayer) -> list[MatchEvent]:
        direction = 1.0 if carrier.team_id == "RED" else -1.0
        burst = 6.0 + carrier.dribbling * 10.0
        if carrier.player_id == "red_17":
            burst += 4.0
        end = Point(
            x=clamp(carrier.x + direction * burst, 1.5, 103.5),
            y=clamp(carrier.y + self.rng.uniform(-3.0, 3.0), 2.0, 66.0),
        )
        start = Point(x=carrier.x, y=carrier.y)
        pressure = self._pressure_modifier(carrier)
        success = self.rng.random() < clamp(0.45 + carrier.dribbling * 0.4 - pressure * 0.35, 0.15, 0.9)
        outcome = "COMPLETE" if success else "TACKLED"
        event = self._emit(
            "CARRY",
            carrier.team_id,
            carrier.player_id,
            None,
            outcome,
            start,
            end if success else Point(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2),
            {
                **self._geometry(carrier.team_id),
                "entered_final_third": success
                and (not in_final_third(carrier.team_id, start.x))
                and in_final_third(carrier.team_id, end.x),
                "box_entry": success and in_box(carrier.team_id, end.x) and not in_box(carrier.team_id, start.x),
            },
        )
        events = [event]
        end_point = end if success else Point(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2)
        if success:
            length = distance(Point(x=carrier.x, y=carrier.y), end_point)
            duration = int(max(800, min(2800, length / 4.4 * 1000)))
            self.carry_target = end_point
            self.carry_until_ms = self.clock_ms + duration
            self.carrier_id = carrier.player_id
        else:
            tackler = self._nearest_actual(other(carrier.team_id), end_point)
            events.append(self._emit("DUEL", other(carrier.team_id), tackler, carrier.player_id, "WON", end_point, end_point))
            self.carry_target = None
            self.carry_until_ms = 0
            self._realize(tackler, Point(x=self.ball.x, y=self.ball.y), end_point, "PASS", other(carrier.team_id), False, 9.0)
        return events

    def _duel(self, carrier: SimPlayer) -> list[MatchEvent]:
        spot = Point(x=carrier.x, y=carrier.y)
        rival = self._nearest_actual(other(carrier.team_id), spot)
        start = Point(x=self.ball.x, y=self.ball.y)
        won = self.rng.random() < 0.48 + carrier.dribbling * 0.2
        winner = carrier.player_id if won else rival
        loser = rival if won else carrier.player_id
        event = self._emit(
            "DUEL",
            carrier.team_id if won else other(carrier.team_id),
            winner,
            loser,
            "WON",
            start,
            start,
        )
        if not won and rival:
            self.possession = other(carrier.team_id)
            self.possession_id += 1
            self.carrier_id = rival
            self.focus[rival] = (start.x, start.y)
        return [event]

    def _shot(self, carrier: SimPlayer) -> list[MatchEvent]:
        goal_x = 105.0 if carrier.team_id == "RED" else 0.0
        aim_y = clamp(34 + self.rng.uniform(-6, 6), 30.5, 37.5)
        start = Point(x=self.ball.x, y=self.ball.y)
        end = Point(x=goal_x, y=aim_y)
        distance_m = math.hypot(goal_x - carrier.x, 34 - carrier.y)
        pressure = self._pressure_modifier(carrier)
        p_on_target = clamp(carrier.shooting * (1.15 - distance_m / 50.0) * (1 - 0.4 * pressure), 0.08, 0.8)
        on_target = self.rng.random() < p_on_target
        p_goal = p_on_target * (0.18 if distance_m < 14 else 0.06)
        is_goal = on_target and self.rng.random() < p_goal
        outcome = "GOAL" if is_goal else ("SAVED" if on_target else "OFF_TARGET")
        event = self._emit(
            "SHOT",
            carrier.team_id,
            carrier.player_id,
            None,
            outcome,
            start,
            end,
            {"distance_m": round(distance_m, 1), "xg": round(p_goal, 2)},
        )
        events = [event]
        duration = int(max(480, min(1600, distance_m / 16 * 1000)))
        shot_end = end if on_target else Point(x=goal_x, y=clamp(aim_y + 8, 1, 67))
        self.flight = Flight(start, shot_end, self.clock_ms, duration, "SHOT", carrier.team_id, None, False)
        self.carrier_id = None
        if not is_goal:
            keeper = "blue_1" if carrier.team_id == "RED" else "red_1"
            if keeper in self.players and self.players[keeper].on_pitch:
                # The keeper attacks an intercept point in front of the goal;
                # the shot itself can still travel to/through the goal line.
                keeper_x = 103.2 if carrier.team_id == "RED" else 1.8
                keeper_y = clamp(shot_end.y, 27.5, 40.5)
                self.focus[keeper] = (keeper_x, keeper_y)
        if is_goal:
            if carrier.team_id == "RED":
                self.score_red += 1
            else:
                self.score_blue += 1
            # Restart from the centre after a short beat. The flight visually ends in the goal.
            self.possession = other(carrier.team_id)
            self.possession_id += 1
            self.next_action_ms = self.clock_ms + duration + 700
        else:
            keeper = "blue_1" if carrier.team_id == "RED" else "red_1"
            self.possession = other(carrier.team_id)
            self.possession_id += 1
            self.carrier_id = keeper if keeper in self.players and self.players[keeper].on_pitch else self._nearest_actual(self.possession, end)
            self.next_action_ms = self.clock_ms + duration + 400
        return events

    def _scripts(self) -> list[MatchEvent]:
        events: list[MatchEvent] = []
        minute_ms = self.clock_ms
        if self.scenario_id == "substitute_impact" and "sub" not in self.done_scripts and minute_ms >= 65 * 60_000:
            events.extend(self._substitute("red_11", "red_17"))
            self.done_scripts.add("sub")
        if self.scenario_id == "red_card" and "card" not in self.done_scripts and minute_ms >= 36 * 60_000:
            events.extend(self._red_card("blue_8"))
            self.done_scripts.add("card")
        if self.scenario_id == "late_comeback" and "shape" not in self.done_scripts and minute_ms >= 77 * 60_000:
            events.append(
                self._emit(
                    "FORMATION_CHANGE",
                    "RED",
                    None,
                    None,
                    "4-2-4",
                    None,
                    None,
                    {"formation": "4-2-4"},
                )
            )
            self.done_scripts.add("shape")
        if self.scenario_id == "late_comeback":
            if "goal82" not in self.done_scripts and minute_ms >= 82 * 60_000:
                events.extend(self._script_goal("red_9"))
                self.done_scripts.add("goal82")
            if "goal88" not in self.done_scripts and minute_ms >= 88 * 60_000:
                events.extend(self._script_goal("red_7"))
                self.done_scripts.add("goal88")
        return events

    def _substitute(self, off_id: str, on_id: str) -> list[MatchEvent]:
        off = self.players[off_id]
        on = SimPlayer(
            player_id=on_id,
            team_id=off.team_id,
            number=17,
            role=off.role,
            x=off.x,
            y=off.y,
            passing=0.74,
            shooting=0.7,
            speed=0.93,
            dribbling=0.9,
            pressing=0.8,
        )
        off.on_pitch = False
        self.players[on_id] = on
        if self.carrier_id == off_id:
            self.carrier_id = on_id
        event = self._emit(
            "SUBSTITUTION",
            off.team_id,
            on_id,
            None,
            "COMPLETE",
            Point(x=off.x, y=off.y),
            Point(x=off.x, y=off.y),
            {"player_off": off_id, "player_on": on_id},
        )
        return [event]

    def _red_card(self, player_id: str) -> list[MatchEvent]:
        player = self.players[player_id]
        player.on_pitch = False
        if self.carrier_id == player_id:
            self._turnover(Point(x=player.x, y=player.y), player.team_id)
        spot = Point(x=player.x, y=player.y)
        foul = self._emit("FOUL", player.team_id, player_id, None, "FOUL", spot, spot, {"card": "red"})
        card = self._emit("CARD", player.team_id, player_id, None, "RED", spot, spot, {"card": "red"})
        return [foul, card]

    def _script_goal(self, scorer_id: str) -> list[MatchEvent]:
        scorer = self.players[scorer_id]
        scorer.on_pitch = True
        start = Point(x=self.ball.x, y=self.ball.y)
        end = Point(x=105.0 if scorer.team_id == "RED" else 0.0, y=34.0)
        self.possession = scorer.team_id
        self.carrier_id = None
        event = self._emit(
            "SHOT",
            scorer.team_id,
            scorer.player_id,
            None,
            "GOAL",
            start,
            end,
            {"distance_m": 13.0, "xg": 0.42, "scripted": True},
        )
        self.flight = Flight(start, end, self.clock_ms, 500, "SHOT", scorer.team_id, None, False)
        if scorer.team_id == "RED":
            self.score_red += 1
        else:
            self.score_blue += 1
        self.possession = other(scorer.team_id)
        self.possession_id += 1
        self.next_action_ms = self.clock_ms + 1200
        return [event]

    def _shape_tracking(self) -> list[TrackingPlayer]:
        rows = []
        for player in self._on_pitch():
            slot = self._slot(player)
            rows.append(
                TrackingPlayer(
                    player_id=player.player_id,
                    team_id=player.team_id,
                    number=player.number,
                    role=player.role,
                    x=round(slot.x, 2),
                    y=round(slot.y, 2),
                    on_pitch=True,
                )
            )
        return rows

    def _geometry(self, attacking: TeamId) -> dict[str, object]:
        players = self._shape_tracking()
        defending = other(attacking)
        return {
            "defending_team": defending,
            "defending_first_line_x": round(first_line_x(players, defending), 2),
            "defending_line_x": round(defensive_line_x(players, defending), 2),
            "defending_line_m": round(defensive_line_height(players, defending), 2),
        }

    def _pressure_modifier(self, carrier: SimPlayer) -> float:
        near = self._nearby_opponents(Point(x=carrier.x, y=carrier.y), carrier.team_id, 7.0)
        base = min(1.0, near / 3.0)
        press = self._style(other(carrier.team_id))["press"]
        return clamp(base * (0.55 + press * 0.6), 0.0, 1.0)

    def _nearby_opponents(self, spot: Point, attacking_team: TeamId, radius: float) -> int:
        return sum(
            1
            for player in self._on_pitch(other(attacking_team))
            if math.hypot(player.x - spot.x, player.y - spot.y) <= radius
        )

    def _pressers(self, carrier: SimPlayer) -> int:
        return self._nearby_opponents(Point(x=carrier.x, y=carrier.y), carrier.team_id, 8.0)

    def _emit(
        self,
        event_type: str,
        team_id: TeamId,
        player_id: str | None,
        target_id: str | None,
        outcome: str | None,
        location: Point | None = None,
        end_location: Point | None = None,
        qualifiers: dict | None = None,
    ) -> MatchEvent:
        self.sequence += 1
        period = 1 if period_of(self.clock_ms) == 1 else 2
        return MatchEvent(
            id=f"event-{self.sequence}",
            match_id=self.match_id,
            sequence=self.sequence,
            period=period,  # type: ignore[arg-type]
            timestamp_ms=self.clock_ms,
            type=event_type,  # type: ignore[arg-type]
            team_id=team_id,
            player_id=player_id,
            target_player_id=target_id,
            possession_id=self.possession_id,
            location=location,
            end_location=end_location,
            outcome=outcome,
            qualifiers=qualifiers or {},
        )
