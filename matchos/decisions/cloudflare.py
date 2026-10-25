"""Cloudflare decision adapter."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from matchos.decisions.remote import RemoteDecisionClient
from matchos.decisions.schema import _decision_state, _parse_systemone, _systemone_questions
from matchos.llm_config import LLMConfig
from matchos.schemas import MatchState, MomentDecision


class CloudflareDecisionClient(RemoteDecisionClient):
    """Cloudflare Workers AI authentication and native SystemOne format."""

    native_systemone = True

    @property
    def request_model(self) -> str:
        return self.model.rsplit("/", 1)[-1]

    def __init__(self, config: LLMConfig) -> None:
        if config.cloudflare_model not in {"@cf/cloudflare/clef", "@cf/cloudflare/clef-flash"}:
            raise ValueError("Unsupported Cloudflare decision model")
        model = config.cloudflare_model
        account = quote(config.cloudflare_account_id, safe="")
        super().__init__(
            config,
            model=model,
            provider=f"cloudflare:{model}",
            api_key=config.cloudflare_api_token,
            endpoint=f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
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
            "model": self.model.rsplit("/", 1)[-1],
            "state": _decision_state(state, reasons, focus_player_id),
            "questions": _systemone_questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        return _parse_systemone(body, self.provider, latency_ms)
