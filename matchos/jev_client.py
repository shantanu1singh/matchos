"""One decision call per trigger.

Jev is an external typed-probability service. When `JEV_BASE_URL` is set the
executor posts state and questions there. Otherwise a local model returns the
same kind of object so the demo does not invent prose in the decision slot.
"""

from __future__ import annotations

import math
import os
import time
from typing import Protocol

import httpx

from matchos.schemas import MatchState, MomentDecision, MomentFrame

QUESTIONS = {
    "tacticalShift": "Has a team's pressing or territorial structure materially changed?",
    "momentumShift": "Has control of the match swung enough to matter?",
    "playerStory": "Is one player's recent involvement a story of its own?",
    "importance": "How important is the strongest change, from 0 to 5?",
    "primaryFrame": "Which single frame best names the change?",
    "shouldSurface": "Is this worth interrupting a viewer?",
}


class DecisionClient(Protocol):
    async def decide(self, state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> MomentDecision: ...


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class LocalJevClient:
    """Deterministic stand-in with the same request and response shape as Jev."""

    provider = "local-jev"

    async def decide(self, state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> MomentDecision:
        started = time.perf_counter()
        deltas = state.deltas
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
        return MomentDecision(
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


class HttpJevClient:
    provider = "jev"

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float = 0.8) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._fallback = LocalJevClient()

    async def decide(self, state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> MomentDecision:
        payload = {
            "state": state.deltas,
            "context": {
                "minute": state.minute,
                "score": [state.score_red, state.score_blue],
                "reasons": reasons,
                "focusPlayerId": focus_player_id,
            },
            "questions": QUESTIONS,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(f"{self.base_url}/decide", json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
            decision = _parse_remote(body, (time.perf_counter() - started) * 1000)
            if decision.primary_frame == "PLAYER_IMPACT" and not decision.player_id:
                decision.player_id = focus_player_id
                focus = state.players.get(focus_player_id or "")
                decision.team_id = focus.team_id if focus is not None else decision.team_id
            return decision
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            decision = await self._fallback.decide(state, reasons, focus_player_id)
            decision.provider = "local-jev-fallback"
            return decision


def build_decision_client() -> DecisionClient:
    base = os.getenv("JEV_BASE_URL", "").strip()
    if not base:
        return LocalJevClient()
    return HttpJevClient(base, os.getenv("JEV_API_KEY"))


def _frame_scores(state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> dict[str, float]:
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
        + _sigmoid((max(deltas.get("blue_entries_delta", 0), deltas.get("red_entries_delta", 0)) - 3) / 1.4) * 0.55
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
    if "goal" in reasons and abs(state.score_red - state.score_blue) <= 1 and (state.score_red + state.score_blue) >= 2:
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


def _parse_remote(body: dict, latency_ms: float) -> MomentDecision:
    answers = body.get("answers") or body
    frames = answers.get("frame_scores") or answers.get("frameScores") or {}
    primary = answers.get("primaryFrame") or answers.get("primary_frame")
    if isinstance(primary, dict):
        frame = str(primary.get("value") or "PRESS_BREAKDOWN")
        confidence = float(primary.get("probability") or 0.5)
    else:
        frame = str(primary or "PRESS_BREAKDOWN")
        confidence = float(answers.get("primary_frame_confidence") or 0.5)
    if frame not in {
        "PRESS_BREAKDOWN",
        "MOMENTUM_SWING",
        "PLAYER_IMPACT",
        "SUBSTITUTE_IMPACT",
        "GOAL",
        "SHOT_CHANCE",
        "RED_CARD",
        "COMEBACK",
    }:
        frame = "MOMENTUM_SWING"
    surface = answers.get("shouldSurface", answers.get("should_surface", 0.5))
    if isinstance(surface, dict):
        surface_p = float(surface.get("probability") or 0.5)
    else:
        surface_p = float(surface)
    importance = answers.get("importance", 3)
    if isinstance(importance, dict):
        importance_v = float(importance.get("value") or importance.get("probability") or 3)
    else:
        importance_v = float(importance)
    def prob(name: str) -> float:
        raw = answers.get(name, 0.0)
        if isinstance(raw, dict):
            return float(raw.get("probability") or 0.0)
        return float(raw)

    return MomentDecision(
        tactical_shift=prob("tacticalShift") or prob("tactical_shift"),
        momentum_shift=prob("momentumShift") or prob("momentum_shift"),
        player_story=prob("playerStory") or prob("player_story"),
        importance=importance_v,
        primary_frame=frame,  # type: ignore[arg-type]
        primary_frame_confidence=confidence,
        should_surface=surface_p,
        frame_scores={str(k): float(v) for k, v in frames.items()} or {frame: confidence},
        player_id=answers.get("player_id"),
        team_id=answers.get("team_id"),
        provider="jev",
        latency_ms=round(latency_ms, 3),
    )
