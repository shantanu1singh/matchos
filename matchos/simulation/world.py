"""Mutable match-world data and its read/query helpers."""

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
    first_line_x,
    other,
    period_of,
    to_absolute,
)
from matchos.scenarios import Scenario, get_scenario, style_for
from matchos.schemas import (
    BallAction,
    LineSample,
    MatchEvent,
    Point,
    TeamId,
    TrackingFrame,
    TrackingPlayer,
)
from matchos.teams import PLAYERS, shirt_number

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
class MatchWorld:
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
    pending_penalty_taker: str | None = None
    pending_restart: tuple[str, TeamId, Point, str | None] | None = None
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
                    number=shirt_number(f"{team.lower()}_{number}", number),
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
        self.carrier_id = self.nearest_actual(self.possession, Point(x=52.5, y=34.0))
        if self.carrier_id:
            carrier = self.players[self.carrier_id]
            self.ball = self.ball_at_feet(carrier)
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

    def minute(self) -> float:
        return self.clock_ms / 60_000.0

    def on_pitch(self, team: TeamId | None = None) -> list[SimPlayer]:
        players = [p for p in self.players.values() if p.on_pitch]
        if team is not None:
            players = [p for p in players if p.team_id == team]
        return players

    def tracking(self) -> list[TrackingPlayer]:
        rows = []
        for player in self.on_pitch():
            rows.append(
                TrackingPlayer(
                    player_id=player.player_id,
                    team_id=player.team_id,
                    number=player.number,
                    name=PLAYERS.get(player.player_id, {}).get("name", ""),
                    role=player.role,
                    x=round(player.x, 2),
                    y=round(player.y, 2),
                    speed_mps=round(math.hypot(player.vx, player.vy), 2),
                    vx=round(player.vx, 2),
                    vy=round(player.vy, 2),
                )
            )
        return rows

    def frame(self) -> TrackingFrame:
        period = 1 if period_of(self.clock_ms) == 1 else 2
        return TrackingFrame(
            match_id=self.match_id,
            timestamp_ms=self.clock_ms,
            period=period,  # type: ignore[arg-type]
            ball=Point(x=round(self.ball.x, 2), y=round(self.ball.y, 2)),
            ball_z=round(self.ball_z, 2),
            ball_vx=round(self.ball_vx, 2),
            ball_vy=round(self.ball_vy, 2),
            players=self.tracking(),
            possession_team=self.possession,
            score_red=self.score_red,
            score_blue=self.score_blue,
            action=self.action(),
        )

    def action(self) -> BallAction | None:
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

    def line_sample(self) -> LineSample:
        players = self.tracking()
        return LineSample(
            timestamp_ms=self.clock_ms,
            red_line_m=round(defensive_line_height(players, "RED"), 2),
            blue_line_m=round(defensive_line_height(players, "BLUE"), 2),
            ball_x=round(self.ball.x, 2),
            possession_team=self.possession,
        )

    def style(self, team: TeamId) -> dict[str, float]:
        return style_for(self.scenario_id, self.minute(), team)

    def formation_target(self, player: SimPlayer) -> tuple[float, float]:
        style = self.style(player.team_id)
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
        if player.team_id == "BLUE" and self.blue_down_a_player() and player.role != "GK":
            ax -= 0.04
            ay = 0.5 + (ay - 0.5) * 0.86
        x, y = to_absolute(player.team_id, ax, ay)
        return clamp(x, 1.0, PITCH_X - 1.0), clamp(y, 1.5, PITCH_Y - 1.5)

    def blue_down_a_player(self) -> bool:
        return len(self.on_pitch("BLUE")) < 11

    def slot(self, player: SimPlayer) -> Point:
        x, y = self.formation_target(player)
        return Point(x=x, y=y)

    def slot_nearest(self, team: TeamId, spot: Point) -> str | None:
        """Nearest tactical role slot. Use only when resetting/initializing shape."""
        choices = self.on_pitch(team)
        if not choices:
            return None
        return min(
            choices, key=lambda p: (self.slot(p).x - spot.x) ** 2 + (self.slot(p).y - spot.y) ** 2
        ).player_id

    def nearest_actual(self, team: TeamId, spot: Point, *, exclude: str | None = None) -> str | None:
        """Nearest player in the current physical world."""
        choices = [p for p in self.on_pitch(team) if p.player_id != exclude]
        if not choices:
            return None
        return min(choices, key=lambda p: (p.x - spot.x) ** 2 + (p.y - spot.y) ** 2).player_id

    def ball_at_feet(self, carrier: SimPlayer) -> Point:
        return Point(
            x=clamp(carrier.x + carrier.hx * 1.55, 0.4, 104.6),
            y=clamp(carrier.y + carrier.hy * 1.55, 0.4, 67.6),
        )

    def shape_tracking(self) -> list[TrackingPlayer]:
        rows = []
        for player in self.on_pitch():
            slot = self.slot(player)
            rows.append(
                TrackingPlayer(
                    player_id=player.player_id,
                    team_id=player.team_id,
                    number=player.number,
                    name=PLAYERS.get(player.player_id, {}).get("name", ""),
                    role=player.role,
                    x=round(slot.x, 2),
                    y=round(slot.y, 2),
                    on_pitch=True,
                )
            )
        return rows

    def geometry(self, attacking: TeamId) -> dict[str, object]:
        # Tactical metrics describe the world the viewer can actually see.
        players = self.tracking()
        defending = other(attacking)
        return {
            "defending_team": defending,
            "defending_first_line_x": round(first_line_x(players, defending), 2),
            "defending_line_x": round(defensive_line_x(players, defending), 2),
            "defending_line_m": round(defensive_line_height(players, defending), 2),
        }

    def pressure_modifier(self, carrier: SimPlayer) -> float:
        near = self.nearby_opponents(Point(x=carrier.x, y=carrier.y), carrier.team_id, 7.0)
        base = min(1.0, near / 3.0)
        press = self.style(other(carrier.team_id))["press"]
        return clamp(base * (0.55 + press * 0.6), 0.0, 1.0)

    def nearby_opponents(self, spot: Point, attacking_team: TeamId, radius: float) -> int:
        return sum(
            1
            for player in self.on_pitch(other(attacking_team))
            if math.hypot(player.x - spot.x, player.y - spot.y) <= radius
        )

    def pressers(self, carrier: SimPlayer) -> int:
        return self.nearby_opponents(Point(x=carrier.x, y=carrier.y), carrier.team_id, 8.0)

    def emit(
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
