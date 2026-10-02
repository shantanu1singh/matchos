"""Believable event-and-tracking simulator. Not a physics engine.

A seeded possession policy emits MatchEvents. A tracking layer moves 22 dots
toward formation anchors so the same feed can drive a tactical board.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

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

SUBSTEP_MS = 100


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
    flight: Flight | None = None
    next_action_ms: int = 400
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
                self.players[player.player_id] = player
        self.score_red = self.scenario.score_red
        self.score_blue = self.scenario.score_blue
        self.clock_ms = int(self.scenario.warmup_from_minute * 60_000)
        self.possession = "RED" if self.rng.random() < 0.5 else "BLUE"
        self.carrier_id = self._nearest(self.possession, Point(x=52.5, y=34.0))
        if self.carrier_id:
            carrier = self.players[self.carrier_id]
            self.ball = Point(x=carrier.x, y=carrier.y)
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
        self._move_players(dt_ms / 1000.0)
        self._move_ball()
        if self.flight is None and self.clock_ms >= self.next_action_ms:
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
        for player in self._on_pitch():
            if flight is not None and flight.kind == "CARRY" and player.player_id == flight.receiver_id:
                continue
            tx, ty = self._target(player)
            max_step = (4.2 + player.speed * 4.4) * dt_s
            if flight is not None and flight.kind == "PASS" and player.player_id == flight.receiver_id:
                tx, ty = flight.end.x, flight.end.y
                max_step = (7.2 + player.speed * 3.2) * dt_s
            elif player.player_id == self.carrier_id and flight is None:
                # Carrier keeps a touch of the ball rather than sprinting to a shape point.
                tx = clamp(player.x * 0.65 + tx * 0.35, 1.0, 104.0)
                ty = clamp(player.y * 0.65 + ty * 0.35, 1.5, 66.5)
            dx, dy = tx - player.x, ty - player.y
            dist = math.hypot(dx, dy)
            if dist <= max_step or dist < 0.05:
                nx, ny = tx, ty
            else:
                scale = max_step / dist
                nx, ny = player.x + dx * scale, player.y + dy * scale
            player.vx = (nx - player.x) / dt_s if dt_s else 0.0
            player.vy = (ny - player.y) / dt_s if dt_s else 0.0
            player.x, player.y = nx, ny
        self._separate()

    def _separate(self) -> None:
        """Push overlapping players apart. Never pull them together."""
        min_sep = 2.6
        players = self._on_pitch()
        for _ in range(3):
            for i, a in enumerate(players):
                for b in players[i + 1 :]:
                    dx, dy = b.x - a.x, b.y - a.y
                    dist = math.hypot(dx, dy)
                    if dist >= min_sep or dist <= 0.001:
                        continue
                    push = (min_sep - dist) * 0.5
                    ux, uy = dx / dist, dy / dist
                    if a.role != "GK":
                        a.x = clamp(a.x - ux * push, 1.0, PITCH_X - 1.0)
                        a.y = clamp(a.y - uy * push, 1.5, PITCH_Y - 1.5)
                    if b.role != "GK":
                        b.x = clamp(b.x + ux * push, 1.0, PITCH_X - 1.0)
                        b.y = clamp(b.y + uy * push, 1.5, PITCH_Y - 1.5)

    def _move_ball(self) -> None:
        if self.flight is not None:
            flight = self.flight
            elapsed = self.clock_ms - flight.started_ms
            t = clamp(elapsed / max(1, flight.duration_ms), 0.0, 1.0)
            self.ball = Point(
                x=flight.start.x + (flight.end.x - flight.start.x) * t,
                y=flight.start.y + (flight.end.y - flight.start.y) * t,
            )
            lift = 2.2 if flight.kind == "SHOT" else (0.15 if flight.kind == "CARRY" else 0.55)
            self.ball_z = math.sin(t * math.pi) * lift
            if flight.kind == "CARRY" and flight.receiver_id:
                carrier = self.players.get(flight.receiver_id)
                if carrier is not None and carrier.on_pitch:
                    self._place_behind(carrier, self.ball, flight)
            if t >= 1.0:
                self._land(flight)
            return
        if self.carrier_id and self.carrier_id in self.players and self.players[self.carrier_id].on_pitch:
            carrier = self.players[self.carrier_id]
            self.ball = self._feet_ball(carrier)
            self.ball_z = 0.0
        self.ball = Point(x=clamp(self.ball.x, 0.4, 104.6), y=clamp(self.ball.y, 0.4, 67.6))

    def _feet_ball(self, carrier: SimPlayer) -> Point:
        speed = math.hypot(carrier.vx, carrier.vy)
        if speed > 0.35:
            ox = carrier.vx / speed * 1.45
            oy = carrier.vy / speed * 1.45
        else:
            direction = 1.0 if carrier.team_id == "RED" else -1.0
            ox, oy = direction * 1.45, 0.0
        return Point(x=clamp(carrier.x + ox, 0.4, 104.6), y=clamp(carrier.y + oy, 0.4, 67.6))

    def _place_behind(self, player: SimPlayer, spot: Point, flight: Flight) -> None:
        dx = flight.end.x - flight.start.x
        dy = flight.end.y - flight.start.y
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        player.x = clamp(spot.x - ux * 1.45, 1.0, PITCH_X - 1.0)
        player.y = clamp(spot.y - uy * 1.45, 1.5, PITCH_Y - 1.5)
        player.vx = ux * 4.2
        player.vy = uy * 4.2

    def _land(self, flight: Flight) -> None:
        self.flight = None
        self.ball = flight.end
        self.ball_z = 0.0
        if flight.kind == "SHOT":
            return
        if flight.success and flight.receiver_id and flight.receiver_id in self.players and self.players[flight.receiver_id].on_pitch:
            receiver = self.players[flight.receiver_id]
            self._place_behind(receiver, flight.end, flight)
            self.carrier_id = flight.receiver_id
            self.possession = flight.team_id
            self.ball = self._feet_ball(receiver)
            self.next_action_ms = max(self.next_action_ms, self.clock_ms + 420)
            return
        self._turnover(flight.end, flight.team_id)
        if self.carrier_id and self.carrier_id in self.players:
            self._place_behind(self.players[self.carrier_id], flight.end, flight)
            self.ball = self._feet_ball(self.players[self.carrier_id])

    def _turnover(self, spot: Point, previous: TeamId) -> None:
        self.possession = other(previous)
        self.possession_id += 1
        self.carrier_id = self._nearest(self.possession, spot)
        self.next_action_ms = self.clock_ms + int(self.rng.randint(400, 900))

    def _nearest(self, team: TeamId, spot: Point) -> str | None:
        choices = self._on_pitch(team)
        if not choices:
            return None
        return min(choices, key=lambda p: (p.x - spot.x) ** 2 + (p.y - spot.y) ** 2).player_id

    def _act(self) -> list[MatchEvent]:
        if self.carrier_id is None or not self.players[self.carrier_id].on_pitch:
            self.carrier_id = self._nearest(self.possession, self.ball)
        if self.carrier_id is None:
            return []
        carrier = self.players[self.carrier_id]
        events: list[MatchEvent] = []
        if self._pressers(carrier) >= 2 and self.clock_ms - self.last_pressure_ms > 2500:
            events.append(self._emit("PRESSURE", other(carrier.team_id), self._nearest(other(carrier.team_id), self.ball), None, "APPLIED"))
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
        start = Point(x=carrier.x, y=carrier.y)
        end = self._receive_point(target, carrier.team_id)
        length = distance(start, end)
        pressure = self._pressure_modifier(carrier)
        qual = self._geometry(carrier.team_id)
        success_p = self._pass_success(carrier, target, length, pressure, qual, long_ball, end)
        success = self.rng.random() < success_p
        outcome = "COMPLETE" if success else ("OUT" if self.rng.random() < 0.25 else "INCOMPLETE")
        event = self._emit(
            "PASS",
            carrier.team_id,
            carrier.player_id,
            target.player_id if success or outcome == "INCOMPLETE" else None,
            outcome,
            start,
            end if outcome != "OUT" else Point(x=clamp(end.x, 0, 105), y=0.2 if end.y < 34 else 67.8),
            {
                **qual,
                "length_m": round(length, 1),
                "long_ball": long_ball,
                "pressure": round(pressure, 2),
                "entered_final_third": (not in_final_third(carrier.team_id, start.x))
                and in_final_third(carrier.team_id, end.x)
                and success,
            },
        )
        events = [event]
        pace = 13.0 if not long_ball else 17.0
        duration = int(max(560, min(2100, length / pace * 1000)))
        if outcome == "OUT":
            self.flight = Flight(start, event.end_location or end, self.clock_ms, duration, "PASS", carrier.team_id, None, False)
            self.carrier_id = None
            return events
        if success:
            self.flight = Flight(start, end, self.clock_ms, duration, "PASS", carrier.team_id, target.player_id, True)
            self.carrier_id = None
            return events
        # Incomplete: sometimes a recorded interception.
        if self.rng.random() < 0.7:
            interceptor = self._nearest(other(carrier.team_id), end)
            events.append(
                self._emit(
                    "INTERCEPTION",
                    other(carrier.team_id),
                    interceptor,
                    None,
                    "WON",
                    end,
                    end,
                    {"related": event.id},
                )
            )
        self.flight = Flight(start, end, self.clock_ms, duration, "PASS", carrier.team_id, None, False)
        self.carrier_id = None
        return events

    def _select_target(self, carrier: SimPlayer, long_ball: bool) -> SimPlayer | None:
        mates = [p for p in self._on_pitch(carrier.team_id) if p.player_id != carrier.player_id and p.role != "GK"]
        if long_ball:
            mates = [p for p in mates if p.role in {"ST", "RW", "LW", "LCM", "RCM"}] or mates
        if not mates:
            return None
        bias = progression_bias(self.scenario_id, self.minute(), carrier.team_id)
        defending = other(carrier.team_id)
        players = self._tracking()
        line = first_line_x(players, defending)
        scored: list[tuple[float, SimPlayer]] = []
        for mate in mates:
            forward = attack_x(carrier.team_id, mate.x) - attack_x(carrier.team_id, carrier.x)
            length = math.hypot(mate.x - carrier.x, mate.y - carrier.y)
            if length < 4 or length > (48 if long_ball else 36):
                continue
            score = 0.25 + max(0.0, forward) * 1.4
            score -= abs(length - (28 if long_ball else 16)) * 0.03
            score -= self._nearby_opponents(Point(x=mate.x, y=mate.y), 6.0) * 0.18
            breaks = (mate.x > line + 3 and carrier.team_id == "RED") or (mate.x < line - 3 and carrier.team_id == "BLUE")
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
        # A step toward goal from the receiver's current spot, so passes look progressive.
        nudge = 3.5 if team == "RED" else -3.5
        return Point(
            x=clamp(target.x + nudge + self.rng.uniform(-1.5, 1.5), 1.5, 103.5),
            y=clamp(target.y + self.rng.uniform(-2.0, 2.0), 2.0, 66.0),
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
        availability = 1.0 - min(0.45, self._nearby_opponents(Point(x=target.x, y=target.y), 5.5) * 0.15)
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
        length = distance(start, end_point)
        duration = int(max(700, min(2600, length / 5.0 * 1000)))
        self.flight = Flight(start, end_point, self.clock_ms, duration, "CARRY", carrier.team_id, carrier.player_id, success)
        self.carrier_id = carrier.player_id
        if not success:
            tackler = self._nearest(other(carrier.team_id), end_point)
            events.append(self._emit("DUEL", other(carrier.team_id), tackler, carrier.player_id, "WON", end_point, end_point))
        return events

    def _duel(self, carrier: SimPlayer) -> list[MatchEvent]:
        rival = self._nearest(other(carrier.team_id), Point(x=carrier.x, y=carrier.y))
        start = Point(x=carrier.x, y=carrier.y)
        won = self.rng.random() < 0.48 + carrier.dribbling * 0.2
        event = self._emit("DUEL", carrier.team_id if won else other(carrier.team_id), carrier.player_id if won else rival, rival, "WON" if won else "LOST", start, start)
        if not won:
            self._turnover(start, carrier.team_id)
        return [event]

    def _shot(self, carrier: SimPlayer) -> list[MatchEvent]:
        goal_x = 105.0 if carrier.team_id == "RED" else 0.0
        aim_y = clamp(34 + self.rng.uniform(-6, 6), 30.5, 37.5)
        start = Point(x=carrier.x, y=carrier.y)
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
        self.flight = Flight(start, end if on_target else Point(x=goal_x, y=clamp(aim_y + 8, 1, 67)), self.clock_ms, duration, "SHOT", carrier.team_id, None, False)
        self.carrier_id = None
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
            self.carrier_id = keeper if self.players[keeper].on_pitch else self._nearest(self.possession, end)
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
        start = Point(x=92.0 if scorer.team_id == "RED" else 13.0, y=34.0)
        end = Point(x=105.0 if scorer.team_id == "RED" else 0.0, y=34.0)
        scorer.x, scorer.y = start.x, start.y
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

    def _geometry(self, attacking: TeamId) -> dict[str, object]:
        players = self._tracking()
        defending = other(attacking)
        return {
            "defending_team": defending,
            "defending_first_line_x": round(first_line_x(players, defending), 2),
            "defending_line_x": round(defensive_line_x(players, defending), 2),
            "defending_line_m": round(defensive_line_height(players, defending), 2),
        }

    def _pressure_modifier(self, carrier: SimPlayer) -> float:
        near = self._nearby_opponents(Point(x=carrier.x, y=carrier.y), 7.0)
        base = min(1.0, near / 3.0)
        press = self._style(other(carrier.team_id))["press"]
        return clamp(base * (0.55 + press * 0.6), 0.0, 1.0)

    def _nearby_opponents(self, spot: Point, radius: float) -> int:
        team = self.possession
        return sum(
            1
            for player in self._on_pitch(other(team))
            if math.hypot(player.x - spot.x, player.y - spot.y) <= radius
        )

    def _pressers(self, carrier: SimPlayer) -> int:
        return self._nearby_opponents(Point(x=carrier.x, y=carrier.y), 8.0)

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
