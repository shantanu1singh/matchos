"""Canonical contracts shared by the simulator, intelligence path, and UI.

Event shape follows a Kloppy-style ontology serialized like StatsBomb JSON.
Tracking uses a 105m × 68m pitch, the same idea as FIFA EPTS / Metrica.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TeamId = Literal["RED", "BLUE"]
Persona = Literal["casual", "analyst", "fan", "player", "broadcaster"]
Language = Literal["en", "hi"]

EventType = Literal[
    "PASS",
    "SHOT",
    "CARRY",
    "DUEL",
    "PRESSURE",
    "INTERCEPTION",
    "RECOVERY",
    "CLEARANCE",
    "FOUL",
    "CARD",
    "SUBSTITUTION",
    "FORMATION_CHANGE",
    "OFFSIDE",
]

MomentFrame = Literal[
    "PRESS_BREAKDOWN",
    "MOMENTUM_SWING",
    "PLAYER_IMPACT",
    "SUBSTITUTE_IMPACT",
    "GOAL",
    "SHOT_CHANCE",
    "RED_CARD",
    "COMEBACK",
]


class Point(BaseModel):
    x: float
    y: float


class MatchEvent(BaseModel):
    id: str
    match_id: str
    sequence: int
    period: Literal[1, 2]
    timestamp_ms: int
    type: EventType
    team_id: TeamId
    player_id: str | None = None
    target_player_id: str | None = None
    possession_id: int
    location: Point | None = None
    end_location: Point | None = None
    outcome: str | None = None
    qualifiers: dict[str, object] = Field(default_factory=dict)
    related_event_ids: list[str] = Field(default_factory=list)

    @property
    def minute(self) -> int:
        return self.timestamp_ms // 60_000

    @property
    def second(self) -> int:
        return (self.timestamp_ms // 1000) % 60


class TrackingPlayer(BaseModel):
    player_id: str
    team_id: TeamId
    number: int
    role: str
    x: float
    y: float
    speed_mps: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    on_pitch: bool = True


class BallAction(BaseModel):
    """The pass, shot, or carry the ball is travelling on."""

    kind: Literal["PASS", "SHOT", "CARRY"]
    team_id: TeamId
    start: Point
    end: Point
    started_ms: int
    duration_ms: int
    receiver_id: str | None = None


class TrackingFrame(BaseModel):
    match_id: str
    timestamp_ms: int
    period: Literal[1, 2]
    ball: Point
    ball_z: float = 0.0
    ball_vx: float = 0.0
    ball_vy: float = 0.0
    players: list[TrackingPlayer]
    possession_team: TeamId
    score_red: int
    score_blue: int
    action: BallAction | None = None


class LineSample(BaseModel):
    timestamp_ms: int
    red_line_m: float
    blue_line_m: float
    ball_x: float
    possession_team: TeamId


class Tick(BaseModel):
    """One live slice of the match. The UI uses `frame`; intelligence uses both."""

    match_id: str
    events: list[MatchEvent]
    frame: TrackingFrame
    samples: list[LineSample]


class TeamRollup(BaseModel):
    possession_last5: float = 0.5
    final_third_entries_last5: int = 0
    final_third_entries_prev5: int = 0
    press_bypass_rate: float | None = None
    press_bypass_prev: float | None = None
    press_bypass_sample: int = 0
    defensive_line_m: float | None = None
    defensive_line_prev_m: float | None = None
    shots_last5: int = 0
    turnovers_last5: int = 0


class PlayerRollup(BaseModel):
    team_id: TeamId
    number: int
    on_pitch: bool = True
    final_third_receptions_last8: int = 0
    final_third_receptions_prev30: int = 0
    between_lines_last8: int = 0
    between_lines_prev30: int = 0
    progressive_actions: int = 0
    box_entries: int = 0
    shots: int = 0
    subbed_on_ms: int | None = None


class MatchState(BaseModel):
    match_id: str
    timestamp_ms: int
    period: Literal[1, 2]
    minute: int
    second: int
    score_red: int
    score_blue: int
    possession_team: TeamId
    red: TeamRollup = Field(default_factory=TeamRollup)
    blue: TeamRollup = Field(default_factory=TeamRollup)
    players: dict[str, PlayerRollup] = Field(default_factory=dict)
    deltas: dict[str, float] = Field(default_factory=dict)


class QuestionAnswer(BaseModel):
    probability: float
    value: str | bool | float


class MomentDecision(BaseModel):
    """Typed probabilities from the single decision call. No prose."""

    tactical_shift: float
    momentum_shift: float
    player_story: float
    importance: float
    primary_frame: MomentFrame
    primary_frame_confidence: float
    should_surface: float
    frame_scores: dict[str, float]
    player_id: str | None = None
    team_id: TeamId | None = None
    provider: str
    latency_ms: float


class Check(BaseModel):
    name: str
    passed: bool
    detail: str


class EvidenceItem(BaseModel):
    id: str
    timestamp_ms: int
    minute: int
    second: int
    type: str
    team_id: str
    player_id: str | None = None
    target_player_id: str | None = None
    outcome: str | None = None
    location: Point | None = None
    end_location: Point | None = None
    label: str


class VerifiedMoment(BaseModel):
    id: str
    type: str
    team_id: TeamId | None = None
    player_id: str | None = None
    frame: MomentFrame
    importance: float
    confidence: float
    facts: dict[str, float | int | str]
    evidence: list[str]
    evidence_detail: list[EvidenceItem]
    valid_from_ms: int
    expires_at_ms: int
    highlight_player_ids: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)


class VerificationResult(BaseModel):
    verified: bool
    moment: VerifiedMoment | None = None
    checks: list[Check] = Field(default_factory=list)
    reason: str | None = None


class TriggerResult(BaseModel):
    fired: bool
    reasons: list[str] = Field(default_factory=list)
    cooled_down: bool = False


class ProducerDecision(BaseModel):
    action: Literal["AIR", "SUPPRESS", "REJECT"]
    reason: str
    priority: float = 0.0
    air_for: list[Persona] = Field(default_factory=list)


class Overlay(BaseModel):
    moment_id: str
    persona: Persona
    language: Language
    team_id: TeamId
    player_id: str | None = None
    kicker: str
    headline: str
    body: str
    stats: list[dict[str, str]] = Field(default_factory=list)
    graphic: dict[str, object] | None = None
    source: Literal["template", "agent"]
    provider: str


class ViewerProfile(BaseModel):
    persona: Persona = "casual"
    team_id: TeamId = "BLUE"
    language: Language = "en"
    player_id: str = "red_7"


class PipelineResult(BaseModel):
    kind: Literal["moment", "trace", "enrichment"]
    trigger: TriggerResult | None = None
    decision: MomentDecision | None = None
    verification: VerificationResult | None = None
    producer: ProducerDecision | None = None
    moment: VerifiedMoment | None = None
    overlay: Overlay | None = None
    state: MatchState | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
    enrichment_text: str | None = None
    moment_id: str | None = None
