"""Vercel AI Gateway native typed decisions (not chat completions)."""

from __future__ import annotations

import json

import httpx

from matchos.decisions.remote import RemoteDecisionClient
from matchos.decisions.schema import _decision_state, _parse_gateway, _questions
from matchos.llm_config import LLMConfig
from matchos.schemas import MatchState, MomentDecision


class GatewayDecisionClient(RemoteDecisionClient):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(
            config,
            model=config.decision_model,
            provider=f"vercel:{config.decision_model}",
            endpoint=f"{config.decision_base_url}/decisions",
            api_key=config.gateway_api_key,
        )

    async def _request(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.config.decision_timeout_s) as client:
            response = await client.post(
                self.endpoint, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}
            )
            response.raise_for_status()
            return response.json()

    def _payload(self, state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
        return {
            "model": self.model,
            "input": json.dumps(_decision_state(state, reasons, focus_player_id)),
            "questions": _questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        return _parse_gateway(body, self.provider, latency_ms)
