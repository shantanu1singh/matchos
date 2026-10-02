"""Decide whether a verified moment should interrupt the viewer right now."""

from __future__ import annotations

import time

from matchos.schemas import MomentFrame, Persona, ProducerDecision, VerifiedMoment, ViewerProfile

GAPS: dict[Persona, float] = {
    "casual": 45.0,
    "fan": 30.0,
    "analyst": 15.0,
    "player": 20.0,
    "broadcaster": 18.0,
}

PERSONAS: tuple[Persona, ...] = ("casual", "analyst", "fan", "player", "broadcaster")


class Producer:
    def __init__(self, clock=None) -> None:
        self.clock = clock or time.monotonic
        self.last_air: dict[Persona, float] = {}
        self.last_frame: dict[str, float] = {}
        self.last_goal_at = -1_000.0

    def decide(
        self,
        moment: VerifiedMoment | None,
        verified: bool,
        should_surface: float,
        viewer: ViewerProfile,
        reason: str | None = None,
    ) -> ProducerDecision:
        if not verified or moment is None:
            return ProducerDecision(action="REJECT", reason=reason or "Verifier rejected the claim.")
        if should_surface < 0.5 and moment.frame not in {"GOAL", "RED_CARD", "COMEBACK"}:
            return ProducerDecision(action="SUPPRESS", reason="Decision call kept this below the surface line.")
        now = self.clock()
        if moment.frame == "GOAL" or moment.frame == "COMEBACK":
            self.last_goal_at = now
        if moment.frame == "PRESS_BREAKDOWN" and now - self.last_goal_at < 15:
            return ProducerDecision(action="SUPPRESS", reason="Holding the tactical note for 15s after a goal.")
        if now - self.last_frame.get(moment.frame, -1_000.0) < 90 and moment.frame not in {"GOAL", "COMEBACK", "RED_CARD"}:
            return ProducerDecision(action="SUPPRESS", reason="Same story aired inside the last 90 seconds.")
        air_for = [persona for persona in PERSONAS if self._persona_allows(persona, moment, viewer, now)]
        if viewer.persona not in air_for:
            return ProducerDecision(
                action="SUPPRESS",
                reason=f"{viewer.persona} cadence or relevance held this back.",
                priority=round(moment.importance * moment.confidence, 3),
                air_for=air_for,
            )
        self.last_frame[moment.frame] = now
        for persona in air_for:
            self.last_air[persona] = now
        return ProducerDecision(
            action="AIR",
            reason="Priority, novelty and viewer cadence all clear.",
            priority=round(moment.importance * moment.confidence, 3),
            air_for=air_for,
        )

    def _persona_allows(self, persona: Persona, moment: VerifiedMoment, viewer: ViewerProfile, now: float) -> bool:
        if now - self.last_air.get(persona, -1_000.0) < GAPS[persona] and moment.frame not in {"GOAL", "COMEBACK", "RED_CARD"}:
            return False
        if persona == "casual":
            return moment.importance >= 0.62 or moment.frame in {"GOAL", "COMEBACK", "RED_CARD"}
        if persona == "analyst":
            return moment.frame in {"PRESS_BREAKDOWN", "MOMENTUM_SWING", "RED_CARD", "GOAL", "COMEBACK", "SUBSTITUTE_IMPACT"}
        if persona == "fan":
            return True
        if persona == "player":
            focus = viewer.player_id
            if moment.player_id and moment.player_id == focus:
                return True
            return moment.importance >= 0.75
        return moment.importance >= 0.55 or moment.frame in {"GOAL", "PRESS_BREAKDOWN", "RED_CARD", "COMEBACK"}
