"""One typed decision request per trigger via Cloudflare or Vercel."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Protocol, get_args
from urllib.parse import quote

import httpx

from matchos.llm_config import LLMConfig
from matchos.model_logging import ModelCall
from matchos.schemas import MatchState, MomentDecision, MomentFrame

logger = logging.getLogger(__name__)
FRAMES = get_args(MomentFrame)

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


def _questions() -> list[dict]:
    questions = [
        {"type": "predicate", "name": name, "instructions": QUESTIONS[name]}
        for name in ("tacticalShift", "momentumShift", "playerStory", "shouldSurface")
    ]
    questions.extend([
        {"type": "score", "name": "importance", "instructions": QUESTIONS["importance"],
         "levels": [{"label": str(level)} for level in range(6)]},
        {"type": "choice", "name": "primaryFrame", "instructions": QUESTIONS["primaryFrame"],
         "choices": [{"value": frame} for frame in FRAMES]},
        {"type": "choice", "name": "team", "instructions": "Which team is the subject of the primary frame? Choose NONE if neither.",
         "choices": [{"value": team} for team in ("RED", "BLUE", "NONE")]},
    ])
    return questions


class GatewayDecisionClient:
    """Native typed decisions; Laya must not be sent to chat/completions."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.provider = f"vercel:{config.decision_model}"
        self.model = config.decision_model
        self.endpoint = f"{config.decision_base_url}/decisions"
        self.api_key = config.gateway_api_key
        self._fallback = LocalDecisionClient()

    def _payload(self, state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
        return {
            "model": self.config.decision_model,
            "input": json.dumps(_decision_state(state, reasons, focus_player_id)),
            "questions": _questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        return _parse_gateway(body, self.provider, latency_ms)

    async def decide(self, state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> MomentDecision:
        started = time.perf_counter()
        payload = self._payload(state, reasons, focus_player_id)
        call = ModelCall("decision", self.provider, payload,
                         secrets=(self.config.gateway_api_key, self.config.cloudflare_api_token))
        try:
            async with httpx.AsyncClient(timeout=self.config.decision_timeout_s) as client:
                response = await client.post(
                    self.endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                body = response.json()
            call.result(body)
            decision = self._parse_response(body, (time.perf_counter() - started) * 1000)
            if decision.primary_frame == "PLAYER_IMPACT":
                decision.player_id = focus_player_id
                focus = state.players.get(focus_player_id or "")
                if focus is not None:
                    decision.team_id = focus.team_id
            elif decision.primary_frame == "SUBSTITUTE_IMPACT":
                decision.player_id = "red_17"
                decision.team_id = "RED"
            return decision
        except asyncio.CancelledError:
            call.result({"reason": "session or request cancelled"}, phase="CANCELLED")
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            call.result({"error": _failure_detail(exc, self.config)}, phase="ERROR")
            logger.warning(
                "Decision model %s failed (%s); using the local model",
                self.model, _failure_detail(exc, self.config),
            )
            decision = await self._fallback.decide(state, reasons, focus_player_id)
            decision.provider = f"local-decision-fallback:{self.provider}"
            decision.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            call.result(decision.model_dump(mode="json"), phase="FALLBACK")
            return decision


def _decision_state(state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
    return {
        "minute": state.minute,
        "score": {"RED": state.score_red, "BLUE": state.score_blue},
        "reasons": reasons,
        "focusPlayerId": focus_player_id,
        "deltas": state.deltas,
        "teams": {"RED": state.red.model_dump(mode="json"), "BLUE": state.blue.model_dump(mode="json")},
        "players": {key: player.model_dump(mode="json") for key, player in state.players.items()},
    }


def _cloudflare_questions() -> dict:
    questions = {}
    for question in _questions():
        converted = {"type": question["type"], "instructions": question["instructions"]}
        if question["type"] == "predicate":
            converted["type"] = "noul"
        elif question["type"] == "choice":
            converted["criteria"] = {item["value"]: item["value"].replace("_", " ") for item in question["choices"]}
        elif question["type"] == "score":
            converted["criteria"] = [item["label"] for item in question["levels"]]
        questions[question["name"]] = converted
    return questions


class CloudflareDecisionClient(GatewayDecisionClient):
    """Share transport and fallback logic, adapting the Workers AI schema."""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        if config.cloudflare_model not in {"@cf/cloudflare/clef", "@cf/cloudflare/clef-flash"}:
            raise ValueError("Unsupported Cloudflare decision model")
        self.model = config.cloudflare_model
        self.provider = f"cloudflare:{self.model}"
        self.api_key = config.cloudflare_api_token
        account = quote(config.cloudflare_account_id, safe="")
        self.endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{self.model}"

    def _payload(self, state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
        return {
            "model": self.model.rsplit("/", 1)[-1],
            "state": _decision_state(state, reasons, focus_player_id),
            "questions": _cloudflare_questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        if not isinstance(body, dict):
            raise ValueError("Invalid Cloudflare response envelope")
        if body.get("success") is False:
            raise ValueError("Cloudflare rejected the decision request")
        result = body.get("result", body)
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise ValueError("Invalid Cloudflare answers")
        answers = result["answers"]
        normalized = []
        for name, question in _cloudflare_questions().items():
            answer = answers[name]
            if not isinstance(answer, dict):
                raise ValueError("Invalid Cloudflare answer")
            if answer["type"] != question["type"]:
                raise ValueError("Invalid Cloudflare decision answer type")
            if question["type"] == "noul":
                normalized.append({"name": name, "type": "predicate", "probability": answer["noul"]})
            elif question["type"] == "choice":
                if not isinstance(answer["probabilities"], dict):
                    raise ValueError("Invalid Cloudflare choice probabilities")
                normalized.append({
                    "name": name, "type": "choice", "choice": answer["choice"],
                    "confidence": answer["confidence"],
                    "probabilities": [{"value": key, "probability": probability} for key, probability in answer["probabilities"].items()],
                })
            else:
                normalized.append({"name": name, "type": "score", "score": answer["score"]})
        return _parse_gateway({"answers": normalized}, self.provider, latency_ms)


def _failure_detail(exc: Exception, config: LLMConfig) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        detail = f"HTTP {response.status_code}"
        try:
            body = response.json()
            error = body.get("error") or next(iter(body.get("errors", [])), {})
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    for secret in (config.gateway_api_key, config.cloudflare_api_token):
                        if secret:
                            message = message.replace(secret, "[REDACTED]")
                    detail += f": {' '.join(message.split())[:400]}"
        except (ValueError, AttributeError, TypeError):
            pass
        return detail
    if isinstance(exc, httpx.TimeoutException):
        return f"{type(exc).__name__}; DECISION_TIMEOUT_S={config.decision_timeout_s:g}"
    if isinstance(exc, httpx.HTTPError):
        return f"network failure: {type(exc).__name__}"
    # Response validation errors can contain arbitrary provider-returned values.
    return f"invalid typed response: {type(exc).__name__}"


def build_decision_client(config: LLMConfig | None = None) -> DecisionClient:
    config = config or LLMConfig.from_env()
    if config.decision_provider == "local":
        return LocalDecisionClient()
    if config.decision_provider == "cloudflare":
        if not config.cloudflare_account_id or not config.cloudflare_api_token:
            return LocalDecisionClient()
        return CloudflareDecisionClient(config)
    if config.decision_provider == "vercel":
        if config.decision_model == "local" or not config.gateway_api_key:
            return LocalDecisionClient()
        return GatewayDecisionClient(config)
    raise ValueError("Unknown decision provider")


def _bounded(value: object, upper: float = 1.0) -> float:
    if isinstance(value, bool):
        raise ValueError("Expected a numeric probability or score")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= upper:
        raise ValueError("Probability or score out of range")
    return number


def _parse_gateway(body: dict, provider: str, latency_ms: float) -> MomentDecision:
    answers = {answer["name"]: answer for answer in body["answers"]}
    for question in _questions():
        if answers[question["name"]]["type"] != question["type"]:
            raise ValueError("Missing, refused, or invalid decision answer")
    primary = answers["primaryFrame"]
    frame = primary["choice"]
    if frame not in FRAMES:
        raise ValueError("Unknown moment frame")
    scores = {item["value"]: _bounded(item["probability"]) for item in primary.get("probabilities", [])}
    if any(key not in FRAMES for key in scores):
        raise ValueError("Unknown moment frame in probabilities")
    confidence = _bounded(primary.get("confidence", scores.get(frame, 0.5)))
    scores.setdefault(frame, confidence)
    team = answers["team"]["choice"]
    if team not in ("RED", "BLUE", "NONE"):
        raise ValueError("Unknown team")
    return MomentDecision(
        tactical_shift=_bounded(answers["tacticalShift"]["probability"]),
        momentum_shift=_bounded(answers["momentumShift"]["probability"]),
        player_story=_bounded(answers["playerStory"]["probability"]),
        should_surface=_bounded(answers["shouldSurface"]["probability"]),
        importance=_bounded(answers["importance"]["score"], 5),
        primary_frame=frame,
        primary_frame_confidence=confidence,
        frame_scores=scores,
        team_id=None if team == "NONE" else team,
        provider=provider,
        latency_ms=round(latency_ms, 3),
    )


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class LocalDecisionClient:
    """Deterministic typed-probability model for offline use."""

    provider = "local-decision"

    async def decide(self, state: MatchState, reasons: list[str], focus_player_id: str | None = None) -> MomentDecision:
        started = time.perf_counter()
        call = ModelCall("decision", self.provider, _decision_state(state, reasons, focus_player_id))
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


