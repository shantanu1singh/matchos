"""Decision client composition: choose a provider without coupling editorial code to it."""

from __future__ import annotations

import logging

from matchos.decisions.contracts import DecisionClient
from matchos.decisions.local import LocalDecisionClient
from matchos.llm_config import LLMConfig

logger = logging.getLogger(__name__)
__all__ = ["DecisionClient", "LocalDecisionClient", "build_decision_client"]


def build_decision_client(config: LLMConfig | None = None) -> DecisionClient:
    config = config or LLMConfig.from_env()
    if config.decision_provider == "local":
        return LocalDecisionClient()
    if config.decision_provider == "azure":
        if not config.azure_configured("decision"):
            logger.warning(
                "Azure decision is not configured; set AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY (or AZURE_AUTH_MODE=entra). Using local decisions"
            )
            return LocalDecisionClient()
        from matchos.azure_foundry import FoundryDecisionClient

        return FoundryDecisionClient(config)
    if config.decision_provider == "cloudflare":
        if not config.cloudflare_account_id or not config.cloudflare_api_token:
            return LocalDecisionClient()
        from matchos.decisions.cloudflare import CloudflareDecisionClient

        return CloudflareDecisionClient(config)
    if config.decision_provider == "vercel":
        if config.decision_model == "local" or not config.gateway_api_key:
            return LocalDecisionClient()
        from matchos.decisions.gateway import GatewayDecisionClient

        return GatewayDecisionClient(config)
    raise ValueError("Unknown decision provider")
