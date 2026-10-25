"""Viewer-owned publication state. Editorial relevance belongs to the selector."""
from __future__ import annotations

import time

from matchos.schemas import Persona, ProducerDecision, VerifiedMoment, ViewerProfile

GAPS: dict[Persona, float] = {"casual": 45.0, "fan": 30.0, "analyst": 15.0, "player": 20.0, "broadcaster": 18.0}
MAJOR_FRAMES = frozenset({"GOAL", "COMEBACK", "RED_CARD"})
REPEAT_WINDOW_S = 90.0
ON_AIR_WINDOW_S = 40.0
POST_GOAL_WINDOW_S = 15.0


class PublicationPolicy:
    """Preflight never mutates state; commit rechecks and records one actual air."""

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.last_air: dict[Persona, float] = {}
        self.last_frame: dict[str, float] = {}
        self.last_goal_at = -1_000.0
        self.aired_major_events: set[str] = set()

    def has_aired(self, moment: VerifiedMoment) -> bool:
        return moment.frame in MAJOR_FRAMES and bool(self.aired_major_events.intersection(moment.evidence))

    def check(self, moment: VerifiedMoment, viewer: ViewerProfile, *, timestamp_ms: int,
              active: VerifiedMoment | None = None, on_air_at: float = 0,
              wall_now: float | None = None) -> ProducerDecision:
        now = self.clock()
        if moment.expires_at_ms <= timestamp_ms:
            reason = "The match moved past this story's live window before publication."
        elif self.has_aired(moment):
            reason = "This goal or dismissal has already aired for this viewer."
        elif moment.frame == "PRESS_BREAKDOWN" and now - self.last_goal_at < POST_GOAL_WINDOW_S:
            reason = "Holding the tactical note for 15s after a goal."
        elif moment.frame not in MAJOR_FRAMES and now - self.last_frame.get(moment.frame, -1_000.0) < REPEAT_WINDOW_S:
            reason = "Same story aired inside the last 90 seconds."
        elif moment.frame not in MAJOR_FRAMES and now - self.last_air.get(viewer.persona, -1_000.0) < GAPS[viewer.persona]:
            reason = "A recent update is still fresh for this viewer."
        elif (moment.frame not in MAJOR_FRAMES and active and active.expires_at_ms > timestamp_ms
              and (time.monotonic() if wall_now is None else wall_now) - on_air_at < ON_AIR_WINDOW_S):
            reason = "A graphic is already on air for this viewer."
        else:
            return ProducerDecision(action="AIR", reason="Freshness, duplication and viewer cadence all clear.",
                                    priority=round(moment.importance * moment.confidence, 3), air_for=[viewer.persona])
        return ProducerDecision(action="SUPPRESS", reason=reason)

    def commit(self, moment: VerifiedMoment, viewer: ViewerProfile, **state) -> ProducerDecision:
        # No await between rechecking publication state and committing it.
        decision = self.check(moment, viewer, **state)
        if decision.action == "AIR":
            now = self.clock()
            self.last_air[viewer.persona] = self.last_frame[moment.frame] = now
            if moment.frame in {"GOAL", "COMEBACK"}:
                self.last_goal_at = now
            if moment.frame in MAJOR_FRAMES:
                self.aired_major_events.update(moment.evidence)
        return decision

    def decide(self, moment, verified, should_surface, viewer, reason=None):
        """Compatibility for direct workflow integrations; selection is handled upstream.

        Keep the legacy argument shape and wire result without retaining its
        importance thresholds or persona/frame whitelist. New code uses check/commit.
        """
        if not verified or moment is None:
            return ProducerDecision(action="REJECT", reason=reason or "Verifier rejected the claim.")
        return self.commit(moment, viewer, timestamp_ms=moment.valid_from_ms)
