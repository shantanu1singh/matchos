"""Pitch geometry on a 105m × 68m surface."""

from __future__ import annotations

import math
from typing import Iterable

from matchos.schemas import MatchEvent, Point, TeamId, TrackingPlayer

PITCH_X = 105.0
PITCH_Y = 68.0
FINAL_THIRD_RED = 70.0
FINAL_THIRD_BLUE = 35.0
BOX_RED = 88.5
BOX_BLUE = 16.5

DEF_ROLES = {"RB", "RCB", "LCB", "LB"}
FRONT_ROLES = {"RW", "ST", "LW"}

ANCHORS: dict[str, tuple[float, float]] = {
    "GK": (0.045, 0.50),
    "RB": (0.22, 0.14),
    "RCB": (0.20, 0.38),
    "LCB": (0.20, 0.62),
    "LB": (0.22, 0.86),
    "CDM": (0.40, 0.50),
    "RCM": (0.52, 0.34),
    "LCM": (0.52, 0.66),
    "RW": (0.72, 0.16),
    "ST": (0.76, 0.50),
    "LW": (0.72, 0.84),
}

ROSTER: list[tuple[str, int]] = [
    ("GK", 1),
    ("RB", 2),
    ("LB", 3),
    ("RCB", 4),
    ("LCB", 5),
    ("CDM", 6),
    ("RW", 7),
    ("RCM", 8),
    ("ST", 9),
    ("LCM", 10),
    ("LW", 11),
]


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def period_of(timestamp_ms: int) -> int:
    return 1 if timestamp_ms < 45 * 60_000 else 2


def other(team: TeamId) -> TeamId:
    return "BLUE" if team == "RED" else "RED"


def to_absolute(team: TeamId, ax: float, ay: float) -> tuple[float, float]:
    ax = clamp(ax, 0.0, 1.0)
    ay = clamp(ay, 0.0, 1.0)
    if team == "RED":
        return ax * PITCH_X, ay * PITCH_Y
    return (1.0 - ax) * PITCH_X, ay * PITCH_Y


def attack_x(team: TeamId, x: float) -> float:
    """0 at the team's own goal, 1 at the opponent's goal."""
    return x / PITCH_X if team == "RED" else (PITCH_X - x) / PITCH_X


def distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def defensive_line_height(players: Iterable[TrackingPlayer], team: TeamId) -> float:
    defs = [p for p in players if p.team_id == team and p.role in DEF_ROLES and p.on_pitch]
    if not defs:
        return 40.0
    if team == "RED":
        return sum(p.x for p in defs) / len(defs)
    return sum(PITCH_X - p.x for p in defs) / len(defs)


def defensive_line_x(players: Iterable[TrackingPlayer], team: TeamId) -> float:
    defs = [p for p in players if p.team_id == team and p.role in DEF_ROLES and p.on_pitch]
    if not defs:
        return 30.0 if team == "RED" else 75.0
    return sum(p.x for p in defs) / len(defs)


def first_line_x(players: Iterable[TrackingPlayer], team: TeamId) -> float:
    outfield = [p for p in players if p.team_id == team and p.role != "GK" and p.on_pitch]
    if not outfield:
        return 52.5
    if team == "BLUE":
        front = sorted(outfield, key=lambda p: p.x)[:3]
    else:
        front = sorted(outfield, key=lambda p: -p.x)[:3]
    return sum(p.x for p in front) / len(front)


def in_final_third(team: TeamId, x: float) -> bool:
    return x >= FINAL_THIRD_RED if team == "RED" else x <= FINAL_THIRD_BLUE


def in_box(team: TeamId, x: float) -> bool:
    return x >= BOX_RED if team == "RED" else x <= BOX_BLUE


def pass_against_press(event: MatchEvent) -> bool:
    """Recompute whether a pass started in the defending team's press zone."""
    if event.type != "PASS" or event.location is None:
        return False
    line = event.qualifiers.get("defending_first_line_x")
    defending = event.qualifiers.get("defending_team")
    if not isinstance(line, (int, float)) or defending not in ("RED", "BLUE"):
        return False
    start = event.location.x
    if defending == "BLUE":
        return line - 18.0 <= start <= line + 8.0
    return line - 8.0 <= start <= line + 18.0


def pass_bypassed(event: MatchEvent) -> bool:
    if event.type != "PASS" or event.outcome != "COMPLETE" or event.end_location is None:
        return False
    if not pass_against_press(event):
        return False
    line = float(event.qualifiers["defending_first_line_x"])  # type: ignore[arg-type]
    defending = event.qualifiers["defending_team"]
    end = event.end_location.x
    if defending == "BLUE":
        return end > line + 4.0
    return end < line - 4.0


def between_the_lines(event: MatchEvent) -> bool:
    if event.end_location is None:
        return False
    first = event.qualifiers.get("defending_first_line_x")
    block = event.qualifiers.get("defending_line_x")
    defending = event.qualifiers.get("defending_team")
    if not isinstance(first, (int, float)) or not isinstance(block, (int, float)):
        return False
    end = event.end_location.x
    if defending == "BLUE":
        low, high = float(first), float(block)
        return low + 2.0 < end < high - 1.0
    low, high = float(block), float(first)
    return low + 1.0 < end < high - 2.0


def entered_final_third(event: MatchEvent) -> bool:
    if event.location is None or event.end_location is None:
        return False
    team = event.team_id
    return (not in_final_third(team, event.location.x)) and in_final_third(team, event.end_location.x)


def shirt(player_id: str | None) -> str:
    if not player_id or "_" not in player_id:
        return ""
    team, number = player_id.split("_", 1)
    return f"{team.upper()} {number}"


def event_label(event: MatchEvent) -> str:
    who = shirt(event.player_id) or event.team_id
    if event.type == "PASS" and event.target_player_id:
        return f"{who} → {shirt(event.target_player_id)}"
    if event.type == "SUBSTITUTION":
        off = event.qualifiers.get("player_off")
        return f"{shirt(str(off)) if off else who} off, {who} on"
    if event.type == "CARD":
        return f"{who} {event.qualifiers.get('card', 'card')}"
    if event.type == "OFFSIDE":
        return f"{who} offside"
    if event.type == "FREE_KICK":
        return f"{event.team_id} free kick"
    if event.type == "PENALTY":
        return f"{event.team_id} penalty"
    if event.type == "FOUL":
        return f"{who} foul"
    if event.type == "SHOT":
        return f"{who} shot"
    return f"{who} {event.type.lower()}"
