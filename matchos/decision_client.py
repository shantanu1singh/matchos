"""Native typed decisions via Azure Foundry, Cloudflare, or Vercel."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Protocol, get_args
from urllib.parse import quote

import httpx

from matchos.teams import identity_context
from matchos.llm_config import LLMConfig
from matchos.model_logging import ModelCall
from matchos.schemas import MatchState, MomentDecision, MomentFrame, VerifiedMoment, ViewerProfile

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
    async def select(self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict) -> MomentDecision: ...

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

    native_systemone = False

    @property
    def request_model(self) -> str:
        return self.model

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.provider = f"vercel:{config.decision_model}"
        self.model = config.decision_model
        self.endpoint = f"{config.decision_base_url}/decisions"
        self.api_key = config.gateway_api_key
        self._fallback = LocalDecisionClient()

    async def select(self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict) -> MomentDecision:
        if not candidates:
            raise ValueError("Editorial selection requires verified candidates")
        started = time.perf_counter()
        questions = _selection_questions(candidates)
        editorial = _editorial_state(state, candidates, viewer, context)
        native = self.native_systemone
        payload = ({"model": self.request_model, "state": editorial,
                    "questions": _systemone_questions(questions)} if native else
                   {"model": self.model, "input": json.dumps(editorial), "questions": questions})
        call = ModelCall("editorial selection", self.provider, payload,
                         secrets=(self.api_key, self.config.gateway_api_key, self.config.cloudflare_api_token))
        try:
            body = await self._request(payload)
            call.result(body)
            answers = _selection_answers(body, native)
            return _parse_selection(answers, candidates, self.provider, (time.perf_counter() - started) * 1000)
        except asyncio.CancelledError:
            call.result({"reason": "session or request cancelled"}, phase="CANCELLED")
            raise
        except Exception as exc:
            call.result({"error": _failure_detail(exc, self.config)}, phase="ERROR")
            logger.warning("Editorial model %s failed (%s); using the existing local fallback", self.model, _failure_detail(exc, self.config))
            decision = await self._fallback.select(state, candidates, viewer, context)
            decision.provider = f"local-decision-fallback:{self.provider}"
            decision.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            call.result(decision.model_dump(mode="json"), phase="FALLBACK")
            return decision

    async def _request(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.config.decision_timeout_s) as client:
            response = await client.post(self.endpoint, json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            return response.json()

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
                         secrets=(self.api_key, self.config.gateway_api_key, self.config.cloudflare_api_token))
        try:
            body = await self._request(payload)
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
        except Exception as exc:
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
        "roster": identity_context(),
        "minute": state.minute,
        "score": {"RED": state.score_red, "BLUE": state.score_blue},
        "reasons": reasons,
        "focusPlayerId": focus_player_id,
        "deltas": state.deltas,
        "teams": {"RED": state.red.model_dump(mode="json"), "BLUE": state.blue.model_dump(mode="json")},
        "players": {key: player.model_dump(mode="json") for key, player in state.players.items()},
    }


def _systemone_questions(questions_list: list[dict] | None = None) -> dict:
    questions = {}
    for question in questions_list if questions_list is not None else _questions():
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

    native_systemone = True

    @property
    def request_model(self) -> str:
        return self.model.rsplit("/", 1)[-1]

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
            "questions": _systemone_questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        return _parse_systemone(body, self.provider, latency_ms)


def _parse_systemone(body: dict, provider: str, latency_ms: float) -> MomentDecision:
    if not isinstance(body, dict):
        raise ValueError("Invalid SystemOne response envelope")
    if body.get("success") is False:
        raise ValueError("SystemOne rejected the decision request")
    result = body.get("result", body)
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("Invalid SystemOne answers")
    answers = result["answers"]
    normalized = []
    for name, question in _systemone_questions().items():
        answer = answers[name]
        if not isinstance(answer, dict):
            raise ValueError("Invalid SystemOne answer")
        if answer["type"] != question["type"]:
            raise ValueError("Invalid SystemOne decision answer type")
        if question["type"] == "noul":
            normalized.append({"name": name, "type": "predicate", "probability": answer["noul"]})
        elif question["type"] == "choice":
            if not isinstance(answer["probabilities"], dict):
                raise ValueError("Invalid SystemOne choice probabilities")
            normalized.append({
                "name": name, "type": "choice", "choice": answer["choice"],
                "confidence": answer["confidence"],
                "probabilities": [{"value": key, "probability": probability} for key, probability in answer["probabilities"].items()],
            })
        else:
            normalized.append({"name": name, "type": "score", "score": answer["score"]})
    return _parse_gateway({"answers": normalized}, provider, latency_ms)


def _selection_questions(candidates: list[VerifiedMoment]) -> list[dict]:
    from matchos.editorial import REASONS
    return [
        {"name": "reason", "type": "choice", "instructions": "Choose the main editorial reason for your selection or abstention. Use only reasons supported by the supplied viewer, candidates and recent updates.",
         "choices": [{"value": code} for code in REASONS]},
        {"name": "candidate", "type": "choice",
         "instructions": "Choose the single verified candidate most worth showing NOW to this viewer, or NONE. Judge interest, novelty, team/player relevance, match phase and recent updates. All candidates have already passed evidence checks; do not re-detect events. Goals and red cards usually deserve immediate attention. Prefer NONE to repetitive or unhelpful interruptions.",
         "choices": [{"value": moment.id} for moment in candidates] + [{"value": "NONE"}]},
        {"name": "importance", "type": "score", "instructions": "Editorial importance of the chosen candidate for this viewer, from 0 to 5; 0 for NONE.",
         "levels": [{"label": str(level)} for level in range(6)]},
        {"name": "shouldSurface", "type": "predicate", "instructions": "Is the chosen verified candidate worth interrupting this viewer now, considering recent updates? Answer no for NONE."},
    ]


def _editorial_state(state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict) -> dict:
    from matchos.language_agent import PERCENT_FACTS
    from matchos.producer import GAPS
    policies = {
        "analyst": "Cover both teams neutrally, with no club allegiance or preferred player. Prioritize meaningful tactical or territorial changes with strong before-and-after evidence; cover major goals and dismissals using their verified score and player-count context.",
        "fan": "Prioritize the selected team's fortunes, including verified pressure from the opponent. A goal or dismissal for either side can change the supported team's score or player advantage; use that verified context. Avoid repetitive emotional commentary.",
        "player": "Prioritize direct evidence about the focused player. Prefer NONE for unrelated tactical stories unless there is a major match event.",
        "casual": "Interrupt for goals, red cards, comebacks or clearly significant chances. Prefer NONE for ordinary tactical fluctuations, gradual possession changes and repeated updates.",
        "broadcaster": "Cover the whole match neutrally, with no club allegiance or preferred player. Ask: what should the audience notice right now? Prioritize goals, dismissals, score changes and clear new turning points that make a useful live commentary cue. Tactical changes deserve airtime when they establish a clear match story, not merely because another metric changed. Prefer NONE over routine possession fluctuations or repeats. Use only the supplied evidence; do not invent drama, chances or causal links.",
    }
    return {"task": "Select from verified candidates; do not invent facts or choose unavailable candidates.",
            "roster": identity_context(),
            "minute": state.minute, "score": {"RED": state.score_red, "BLUE": state.score_blue},
            "playersOnPitch": {"RED": state.red.players_on_pitch, "BLUE": state.blue.players_on_pitch},
            "redCards": {"RED": state.red.red_cards, "BLUE": state.blue.red_cards},
            "viewer": viewer.model_profile(),
            "editorialPolicy": {"guidance": policies[viewer.persona], "minimumGapSeconds": GAPS[viewer.persona], "timeUnit": "wall-clock seconds"}, **context,
            "verifiedCandidates": [
                {"id": moment.id, "frame": moment.frame, "team": moment.team_id, "player": moment.player_id,
                 "facts": {key: f"{round(float(value) * 100)}%" if key in PERCENT_FACTS else value
                           for key, value in moment.facts.items()},
                 "checks": [check.model_dump(mode="json") for check in moment.checks],
                 "evidence": [item.model_dump(mode="json") for item in moment.evidence_detail]}
                for moment in candidates]}


def _selection_answers(body: dict, native_systemone: bool) -> dict:
    if not native_systemone:
        return {answer["name"]: answer for answer in body["answers"]}
    if body.get("success") is False:
        raise ValueError("SystemOne rejected selection")
    raw = body.get("result", body)["answers"]
    if raw["candidate"]["type"] != "choice" or raw["importance"]["type"] != "score" or raw["shouldSurface"]["type"] != "noul":
        raise ValueError("Invalid selection answer types")
    choice = raw["candidate"]
    return {**({"reason": raw["reason"]} if "reason" in raw else {}), "candidate": {**choice, "probabilities": [{"value": key, "probability": value} for key, value in choice["probabilities"].items()]},
            "importance": raw["importance"],
            "shouldSurface": {"type": "predicate", "probability": raw["shouldSurface"]["noul"]}}


def _parse_selection(answers: dict, candidates: list[VerifiedMoment], provider: str, latency_ms: float) -> MomentDecision:
    for name, kind in (("candidate", "choice"), ("importance", "score"), ("shouldSurface", "predicate")):
        if answers[name]["type"] != kind:
            raise ValueError("Invalid selection answer")
    choice = answers["candidate"]
    available = {moment.id: moment for moment in candidates}
    selected = choice["choice"]
    if selected not in {*available, "NONE"}:
        raise ValueError("Selection is not a verified candidate")
    scores = {item["value"]: _bounded(item["probability"]) for item in choice["probabilities"]}
    if set(scores) - {*available, "NONE"}:
        raise ValueError("Unverified candidate in selection probabilities")
    confidence = _bounded(choice.get("confidence", scores.get(selected, 0)))
    scores.setdefault(selected, confidence)
    importance = _bounded(answers["importance"]["score"], 5)
    surface = _bounded(answers["shouldSurface"]["probability"])
    reason = answers.get("reason")
    reason_code = None
    if reason is not None:
        from matchos.editorial import REASONS
        if reason.get("type") != "choice" or reason.get("choice") not in REASONS:
            raise ValueError("Invalid editorial reason")
        reason_code = reason["choice"]
    moment = available.get(selected)
    frame_scores = {}
    for candidate in candidates:
        frame_scores[candidate.frame] = max(frame_scores.get(candidate.frame, 0), scores.get(candidate.id, 0))
    return MomentDecision(editorial_reason=reason_code, selected_candidate_id=moment.id if moment else None, candidate_scores=scores,
                          tactical_shift=sum(score for frame, score in frame_scores.items() if frame == "PRESS_BREAKDOWN"),
                          momentum_shift=frame_scores.get("MOMENTUM_SWING", 0),
                          player_story=max(frame_scores.get("PLAYER_IMPACT", 0), frame_scores.get("SUBSTITUTE_IMPACT", 0)),
                          importance=importance if moment else 0, should_surface=surface if moment else 0,
                          primary_frame=moment.frame if moment else candidates[0].frame,
                          primary_frame_confidence=confidence, frame_scores=frame_scores,
                          team_id=moment.team_id if moment else None, player_id=moment.player_id if moment else None,
                          provider=provider, latency_ms=round(latency_ms, 3))


def _failure_detail(exc: Exception, config: LLMConfig) -> str:
    # Azure SDK exceptions may contain response bodies or credentials. Keep diagnostics structural.
    from azure.core.exceptions import AzureError
    if isinstance(exc, AzureError):
        status = getattr(exc, "status_code", None)
        return f"HTTP {status}" if status else f"Azure SDK failure: {type(exc).__name__}"
    if isinstance(exc, TimeoutError):
        return f"TimeoutError; DECISION_TIMEOUT_S={config.decision_timeout_s:g}"
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
    if config.decision_provider == "azure":
        if not config.azure_configured("decision"):
            logger.warning("Azure decision is not configured; set AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY (or AZURE_AUTH_MODE=entra). Using local decisions")
            return LocalDecisionClient()
        from matchos.azure_foundry import FoundryDecisionClient
        return FoundryDecisionClient(config)
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

    async def select(self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict) -> MomentDecision:
        """Keep offline/failure behavior, restricted to evidence-backed candidates."""
        if not candidates:
            raise ValueError("Editorial selection requires verified candidates")
        started = time.perf_counter()
        call = ModelCall("editorial selection", self.provider, _editorial_state(state, candidates, viewer, context))
        scores = _fallback_candidate_scores(state, candidates, viewer, context)
        chosen = max(candidates, key=lambda candidate: scores[candidate.id])
        confidence = scores[chosen.id]
        threshold = {"analyst": .5, "fan": .5, "broadcaster": .65, "casual": .65, "player": .55}[viewer.persona]
        selected = chosen.id if confidence >= threshold else "NONE"
        result = _parse_selection({"candidate": {"type": "choice", "choice": selected, "confidence": confidence if selected != "NONE" else 1 - confidence,
                                                "probabilities": [{"value": candidate.id, "probability": scores[candidate.id]} for candidate in candidates]
                                                                 + [{"value": "NONE", "probability": _clamp(1 - confidence)}]},
                                   "importance": {"type": "score", "score": round(confidence * 5, 2)},
                                   "shouldSurface": {"type": "predicate", "probability": max(confidence, .9) if chosen.frame in {"GOAL", "COMEBACK", "RED_CARD"} else confidence}},
                                  candidates, self.provider, (time.perf_counter() - started) * 1000)
        call.result(result.model_dump(mode="json"))
        return result

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


def _fallback_candidate_scores(state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict) -> dict[str, float]:
    """Offline editorial preferences; scores are heuristics, not calibrated probabilities."""
    from matchos.producer import GAPS
    recent = context.get("recentUpdates", [])
    scores = {}
    major = {"GOAL", "COMEBACK", "RED_CARD"}
    for candidate in candidates:
        frame = candidate.frame
        # Score each player's own evidence rather than an inactive viewer focus.
        base = _frame_scores(state, context.get("triggerReasons", []), candidate.player_id)
        score = base.get(frame, 0)
        if frame in major:
            score = .95 if frame == "RED_CARD" else .97
        else:
            if frame == "SHOT_CHANCE":
                score = .6  # The candidate already passed the shot evidence checks.
            if viewer.persona == "analyst":
                score = score + .08 if frame == "PRESS_BREAKDOWN" else score + .03 if frame == "MOMENTUM_SWING" else score * .85
            elif viewer.persona == "broadcaster":
                score = score + .1 if frame == "SHOT_CHANCE" else score * (.7 if frame == "PLAYER_IMPACT" else .85)
            elif viewer.persona == "fan":
                score = score + .08 if candidate.team_id == viewer.team_id else score * .92
            elif viewer.persona == "player":
                score = score + .1 if candidate.player_id == viewer.player_id else score * .5
            elif viewer.persona == "casual":
                score *= 1.1 if frame == "SHOT_CHANCE" else .6
            if any(item.get("frame") == frame and item.get("secondsAgo", 1000) < 90 for item in recent):
                score *= .35
            if any(item.get("secondsAgo", 1000) < GAPS[viewer.persona] for item in recent):
                score = 0
        # Major live events must outrank even the strongest routine story.
        scores[candidate.id] = round(_clamp(score, hi=1 if frame in major else .94), 3)
    return scores


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


