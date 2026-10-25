"""Offline decision fallback, restricted to evidence-backed candidate IDs."""

from __future__ import annotations

import math
import time

from matchos.decisions.schema import _decision_state, _editorial_state, _parse_selection
from matchos.model_logging import ModelCall
from matchos.schemas import MatchState, MomentDecision, VerifiedMoment, ViewerProfile


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class LocalDecisionClient:
    """Deterministic typed-probability model for offline use."""

    provider = "local-decision"

    async def select(
        self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict
    ) -> MomentDecision:
        """Keep offline/failure behavior, restricted to evidence-backed candidates."""
        if not candidates:
            raise ValueError("Editorial selection requires verified candidates")
        started = time.perf_counter()
        call = ModelCall(
            "editorial selection", self.provider, _editorial_state(state, candidates, viewer, context)
        )
        scores = _fallback_candidate_scores(state, candidates, viewer, context)
        chosen = max(candidates, key=lambda candidate: scores[candidate.id])
        confidence = scores[chosen.id]
        threshold = {"analyst": 0.5, "fan": 0.5, "broadcaster": 0.65, "casual": 0.65, "player": 0.55}[
            viewer.persona
        ]
        selected = chosen.id if confidence >= threshold else "NONE"
        result = _parse_selection(
            {
                "candidate": {
                    "type": "choice",
                    "choice": selected,
                    "confidence": confidence if selected != "NONE" else 1 - confidence,
                    "probabilities": [
                        {"value": candidate.id, "probability": scores[candidate.id]}
                        for candidate in candidates
                    ]
                    + [{"value": "NONE", "probability": _clamp(1 - confidence)}],
                },
                "importance": {"type": "score", "score": round(confidence * 5, 2)},
                "shouldSurface": {
                    "type": "predicate",
                    "probability": max(confidence, 0.9)
                    if chosen.frame in {"GOAL", "COMEBACK", "RED_CARD"}
                    else confidence,
                },
            },
            candidates,
            self.provider,
            (time.perf_counter() - started) * 1000,
        )
        call.result(result.model_dump(mode="json"))
        return result

    async def decide(
        self, state: MatchState, reasons: list[str], focus_player_id: str | None = None
    ) -> MomentDecision:
        started = time.perf_counter()
        call = ModelCall("decision", self.provider, _decision_state(state, reasons, focus_player_id))
        scores = _frame_scores(state, reasons, focus_player_id)
        public = {key: value for key, value in scores.items() if not key.startswith("_")}
        primary = max(public, key=public.get)
        confidence = scores[primary]
        tactical = max(scores["PRESS_BREAKDOWN"], scores["RED_CARD"] * 0.8)
        momentum = scores["MOMENTUM_SWING"]
        player = max(scores["PLAYER_IMPACT"], scores["SUBSTITUTE_IMPACT"])
        importance = round(5.0 * max(confidence, tactical, momentum, player), 2)
        should = _clamp(confidence * 0.72 + min(1.0, importance / 5.0) * 0.28)
        if primary in {"GOAL", "RED_CARD", "COMEBACK"}:
            should = max(should, 0.9)
        player_id = None
        team_id = None
        if primary == "PLAYER_IMPACT":
            player_id = focus_player_id
            focus = state.players.get(focus_player_id or "")
            team_id = focus.team_id if focus is not None else None
        elif primary == "SUBSTITUTE_IMPACT":
            player_id = "red_17"
            team_id = "RED"
        elif primary == "PRESS_BREAKDOWN":
            team_id = "BLUE" if scores.get("_blue_press", 0) >= scores.get("_red_press", 0) else "RED"
        elif primary in {"GOAL", "COMEBACK", "SHOT_CHANCE"}:
            team_id = "RED" if "goal" in reasons or state.score_red >= state.score_blue else "BLUE"
        elif primary == "RED_CARD":
            team_id = "BLUE"
        latency = (time.perf_counter() - started) * 1000
        public_scores = {key: round(value, 3) for key, value in scores.items() if not key.startswith("_")}
        decision = MomentDecision(
            tactical_shift=round(tactical, 3),
            momentum_shift=round(momentum, 3),
            player_story=round(player, 3),
            importance=importance,
            primary_frame=primary,  # type: ignore[arg-type]
            primary_frame_confidence=round(confidence, 3),
            should_surface=round(should, 3),
            frame_scores=public_scores,
            player_id=player_id,
            team_id=team_id,  # type: ignore[arg-type]
            provider=self.provider,
            latency_ms=round(latency, 3),
        )
        call.result(decision.model_dump(mode="json"))
        return decision


def _fallback_candidate_scores(
    state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict
) -> dict[str, float]:
    """Offline editorial preferences; scores are heuristics, not calibrated probabilities."""
    from matchos.publication import GAPS

    recent = context.get("recentUpdates", [])
    scores = {}
    major = {"GOAL", "COMEBACK", "RED_CARD"}
    for candidate in candidates:
        frame = candidate.frame
        # Score each player's own evidence rather than an inactive viewer focus.
        base = _frame_scores(state, context.get("triggerReasons", []), candidate.player_id)
        score = base.get(frame, 0)
        if frame in major:
            score = 0.95 if frame == "RED_CARD" else 0.97
        else:
            if frame == "SHOT_CHANCE":
                score = 0.6  # The candidate already passed the shot evidence checks.
            if viewer.persona == "analyst":
                score = (
                    score + 0.08
                    if frame == "PRESS_BREAKDOWN"
                    else score + 0.03
                    if frame == "MOMENTUM_SWING"
                    else score * 0.85
                )
            elif viewer.persona == "broadcaster":
                score = (
                    score + 0.1
                    if frame == "SHOT_CHANCE"
                    else score * (0.7 if frame == "PLAYER_IMPACT" else 0.85)
                )
            elif viewer.persona == "fan":
                score = score + 0.08 if candidate.team_id == viewer.team_id else score * 0.92
            elif viewer.persona == "player":
                score = score + 0.1 if candidate.player_id == viewer.player_id else score * 0.5
            elif viewer.persona == "casual":
                score *= 1.1 if frame == "SHOT_CHANCE" else 0.6
            if any(item.get("frame") == frame and item.get("secondsAgo", 1000) < 90 for item in recent):
                score *= 0.35
            if any(item.get("secondsAgo", 1000) < GAPS[viewer.persona] for item in recent):
                score = 0
        # Major live events must outrank even the strongest routine story.
        scores[candidate.id] = round(_clamp(score, hi=1 if frame in major else 0.94), 3)
    return scores


def _frame_scores(
    state: MatchState, reasons: list[str], focus_player_id: str | None = None
) -> dict[str, float]:
    deltas = state.deltas
    blue_bypass = deltas.get("blue_press_bypass_delta", 0.0)
    red_bypass = deltas.get("red_press_bypass_delta", 0.0)
    blue_line = deltas.get("blue_line_delta_m", 0.0)
    red_line = deltas.get("red_line_delta_m", 0.0)
    blue_press = _sigmoid((blue_bypass - 0.18) / 0.07) * 0.65 + _sigmoid((-(blue_line) - 3.0) / 1.6) * 0.35
    red_press = _sigmoid((red_bypass - 0.18) / 0.07) * 0.65 + _sigmoid((-(red_line) - 3.0) / 1.6) * 0.35
    if state.blue.press_bypass_sample < 5:
        blue_press *= 0.35
    if state.red.press_bypass_sample < 5:
        red_press *= 0.35
    press = max(blue_press, red_press)

    territory = abs(state.red.possession_last5 - 0.5)
    momentum = _clamp(
        _sigmoid((territory - 0.12) / 0.05) * 0.45
        + _sigmoid((max(deltas.get("blue_entries_delta", 0), deltas.get("red_entries_delta", 0)) - 3) / 1.4)
        * 0.55
    )

    focus = state.players.get(focus_player_id or "")
    player = 0.0
    if focus is not None:
        player = _clamp((focus.between_lines_last8 - 2) / 5.0)
        if focus.between_lines_last8 >= 4 and focus.between_lines_last8 >= focus.between_lines_prev30:
            player = max(player, 0.72)

    sub = state.players.get("red_17")
    substitute = 0.0
    if sub is not None and sub.subbed_on_ms is not None:
        substitute = _clamp((sub.progressive_actions + sub.box_entries) / 6.0)

    goal = 0.97 if "goal" in reasons else 0.0
    card = 0.95 if "red_card" in reasons else 0.0
    shot = 0.08
    comeback = 0.0
    if (
        "goal" in reasons
        and abs(state.score_red - state.score_blue) <= 1
        and (state.score_red + state.score_blue) >= 2
    ):
        comeback = 0.9

    return {
        "PRESS_BREAKDOWN": round(press, 3),
        "MOMENTUM_SWING": round(momentum, 3),
        "PLAYER_IMPACT": round(player, 3),
        "SUBSTITUTE_IMPACT": round(substitute, 3),
        "GOAL": goal,
        "SHOT_CHANCE": shot,
        "RED_CARD": card,
        "COMEBACK": comeback,
        "_blue_press": blue_press,
        "_red_press": red_press,
    }
