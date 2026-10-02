"""Named scenario injections. They change the world, never the intelligence labels."""

from __future__ import annotations

from dataclasses import dataclass

from matchos.schemas import TeamId


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    start_minute: float
    warmup_from_minute: float
    default_speed: int
    score_red: int
    score_blue: int


SCENARIOS: dict[str, Scenario] = {
    "balanced": Scenario(
        id="balanced",
        label="Balanced match",
        description="Two even sides. Moments appear only when the run of play earns them.",
        start_minute=0,
        warmup_from_minute=0,
        default_speed=1,
        score_red=0,
        score_blue=0,
    ),
    "press_collapse": Scenario(
        id="press_collapse",
        label="Press collapse",
        description="Blue start with a high press. From 55' their line drops and Red begin to play through it.",
        start_minute=50,
        warmup_from_minute=42,
        default_speed=1,
        score_red=1,
        score_blue=1,
    ),
    "substitute_impact": Scenario(
        id="substitute_impact",
        label="Substitute impact",
        description="Red send on a quicker wide player at 65'. The feed is not told that this matters.",
        start_minute=62,
        warmup_from_minute=55,
        default_speed=1,
        score_red=1,
        score_blue=1,
    ),
    "late_comeback": Scenario(
        id="late_comeback",
        label="Late comeback",
        description="Red trail 0–2 at 75', change shape, and are given two real chances late.",
        start_minute=74,
        warmup_from_minute=70,
        default_speed=1,
        score_red=0,
        score_blue=2,
    ),
    "red_card": Scenario(
        id="red_card",
        label="Red card chaos",
        description="Blue go down to ten. The remaining block has to cover more grass.",
        start_minute=32,
        warmup_from_minute=25,
        default_speed=1,
        score_red=0,
        score_blue=0,
    ),
    "random": Scenario(
        id="random",
        label="Random",
        description="No scripted story. The seed alone decides the match.",
        start_minute=0,
        warmup_from_minute=0,
        default_speed=1,
        score_red=0,
        score_blue=0,
    ),
}


def get_scenario(scenario_id: str) -> Scenario:
    key = scenario_id if scenario_id in SCENARIOS else "balanced"
    return SCENARIOS[key]


def style_for(scenario_id: str, minute: float, team: TeamId) -> dict[str, float]:
    """Team style in [0, 1]. Higher press and defensive_line means a higher block."""
    if team == "RED":
        style = {"press": 0.58, "directness": 0.42, "width": 0.64, "tempo": 0.70, "defensive_line": 0.62}
    else:
        style = {"press": 0.70, "directness": 0.36, "width": 0.60, "tempo": 0.66, "defensive_line": 0.68}

    if scenario_id == "press_collapse" and team == "BLUE":
        # Ramp from a working high press into a dropped block between 55' and 58'.
        if minute < 55:
            style["press"] = 0.84
            style["defensive_line"] = 0.78
        else:
            t = min(1.0, (minute - 55.0) / 3.0)
            style["press"] = 0.84 - 0.58 * t
            style["defensive_line"] = 0.78 - 0.40 * t
    elif scenario_id == "press_collapse" and team == "RED" and minute >= 55:
        t = min(1.0, (minute - 55.0) / 3.0)
        style["directness"] = 0.42 + 0.28 * t
        style["tempo"] = 0.74 + 0.12 * t

    if scenario_id == "late_comeback" and team == "RED" and minute >= 77:
        style["directness"] = 0.72
        style["tempo"] = 0.88
        style["press"] = 0.80
    if scenario_id == "late_comeback" and team == "BLUE" and minute >= 82:
        style["defensive_line"] = 0.40
        style["press"] = 0.30

    if scenario_id == "red_card" and team == "BLUE" and minute >= 36:
        style["press"] = 0.28
        style["defensive_line"] = 0.36
        style["width"] = 0.42
    if scenario_id == "red_card" and team == "RED" and minute >= 36:
        style["directness"] = 0.55
        style["tempo"] = 0.80

    if scenario_id == "substitute_impact" and team == "RED" and minute >= 65:
        style["width"] = 0.78
        style["tempo"] = 0.82
    return style


def progression_bias(scenario_id: str, minute: float, attacking: TeamId) -> float:
    """Extra weight on passes that break the first line. 0 is neutral."""
    if scenario_id == "press_collapse" and attacking == "RED":
        if minute < 57:
            return -0.35
        t = min(1.0, (minute - 57.0) / 1.5)
        return -0.35 + 1.25 * t
    if scenario_id == "red_card" and attacking == "RED" and minute >= 36:
        return 0.45
    if scenario_id == "substitute_impact" and attacking == "RED" and minute >= 66:
        return 0.25
    if scenario_id == "late_comeback" and attacking == "RED" and minute >= 77:
        return 0.4
    return 0.0
