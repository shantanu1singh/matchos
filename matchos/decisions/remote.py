"""Provider-neutral remote decision lifecycle; adapters own transport and wire format."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod

import httpx

from matchos.decisions.local import LocalDecisionClient
from matchos.decisions.schema import (
    _editorial_state,
    _parse_selection,
    _selection_answers,
    _selection_questions,
    _systemone_questions,
)
from matchos.llm_config import LLMConfig
from matchos.model_logging import ModelCall
from matchos.schemas import MatchState, MomentDecision, VerifiedMoment, ViewerProfile

logger = logging.getLogger(__name__)


class RemoteDecisionClient(ABC):
    """Shared validation, logging, deadlines and fallback for remote decision providers."""

    native_systemone = False

    @property
    def request_model(self) -> str:
        return self.model

    def __init__(self, config: LLMConfig, *, model: str, provider: str, endpoint: str, api_key: str) -> None:
        self.config = config
        self.model = model
        self.provider = provider
        self.endpoint = endpoint
        self.api_key = api_key
        self._fallback = LocalDecisionClient()

    async def select(
        self, state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict
    ) -> MomentDecision:
        if not candidates:
            raise ValueError("Editorial selection requires verified candidates")
        started = time.perf_counter()
        questions = _selection_questions(candidates)
        editorial = _editorial_state(state, candidates, viewer, context)
        native = self.native_systemone
        payload = (
            {"model": self.request_model, "state": editorial, "questions": _systemone_questions(questions)}
            if native
            else {"model": self.model, "input": json.dumps(editorial), "questions": questions}
        )
        call = ModelCall(
            "editorial selection",
            self.provider,
            payload,
            secrets=(self.api_key, self.config.gateway_api_key, self.config.cloudflare_api_token),
        )
        try:
            body = await self._request(payload)
            call.result(body)
            answers = _selection_answers(body, native)
            return _parse_selection(
                answers, candidates, self.provider, (time.perf_counter() - started) * 1000
            )
        except asyncio.CancelledError:
            call.result({"reason": "session or request cancelled"}, phase="CANCELLED")
            raise
        except Exception as exc:
            call.result({"error": _failure_detail(exc, self.config)}, phase="ERROR")
            logger.warning(
                "Editorial model %s failed (%s); using the existing local fallback",
                self.model,
                _failure_detail(exc, self.config),
            )
            decision = await self._fallback.select(state, candidates, viewer, context)
            decision.provider = f"local-decision-fallback:{self.provider}"
            decision.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            call.result(decision.model_dump(mode="json"), phase="FALLBACK")
            return decision

    @abstractmethod
    async def _request(self, payload: dict) -> dict:
        """Send one request using the provider's authenticated transport."""
        raise NotImplementedError

    @abstractmethod
    def _payload(self, state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
        raise NotImplementedError

    @abstractmethod
    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        raise NotImplementedError

    async def decide(
        self, state: MatchState, reasons: list[str], focus_player_id: str | None = None
    ) -> MomentDecision:
        started = time.perf_counter()
        payload = self._payload(state, reasons, focus_player_id)
        call = ModelCall(
            "decision",
            self.provider,
            payload,
            secrets=(self.api_key, self.config.gateway_api_key, self.config.cloudflare_api_token),
        )
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
                self.model,
                _failure_detail(exc, self.config),
            )
            decision = await self._fallback.decide(state, reasons, focus_player_id)
            decision.provider = f"local-decision-fallback:{self.provider}"
            decision.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            call.result(decision.model_dump(mode="json"), phase="FALLBACK")
            return decision


def _failure_detail(exc: Exception, config: LLMConfig) -> str:
    # Keep SDK diagnostics structural: their messages may contain response bodies or credentials.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}"
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
