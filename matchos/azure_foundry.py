"""Microsoft SDK adapters for Foundry's decision API and Agent Framework Responses client.

Decision-1 is a typed scoring model, not a chat model. Azure Core supplies its
async authenticated REST transport; the generative deployment uses Microsoft's
OpenAIChatClient (Responses API). Both remain inside the existing workflow.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)
from agent_framework.openai import OpenAIChatClient
from azure.core import AsyncPipelineClient
from azure.core.credentials import AzureKeyCredential
from azure.core.pipeline.policies import (
    AsyncBearerTokenCredentialPolicy,
    AsyncRetryPolicy,
    AzureKeyCredentialPolicy,
)
from azure.core.rest import HttpRequest
from azure.identity.aio import DefaultAzureCredential

from matchos.decision_client import (
    GatewayDecisionClient,
    _decision_state,
    _parse_systemone,
    _systemone_questions,
)
from matchos.llm_config import LLMConfig
from matchos.model_logging import ModelCall
from matchos.schemas import MatchState, MomentDecision

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
logger = logging.getLogger(__name__)


class FoundryDecisionClient(GatewayDecisionClient):
    native_systemone = True

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self.model = config.azure_decision_deployment
        self.provider = f"azure-foundry:{self.model}"
        self.api_key = config.azure_key("decision")
        self.endpoint = f"{config.azure_resource_endpoint('decision')}/providers/microsoft/v1/systemone"

    async def _request(self, payload: dict) -> dict:
        async def send():
            async with AsyncExitStack() as stack:
                if self.config.azure_auth_mode == "entra":
                    credential = await stack.enter_async_context(DefaultAzureCredential())
                    auth = AsyncBearerTokenCredentialPolicy(credential, TOKEN_SCOPE)
                else:
                    auth = AzureKeyCredentialPolicy(AzureKeyCredential(self.api_key), "api-key")
                client = await stack.enter_async_context(
                    AsyncPipelineClient(
                        self.config.azure_resource_endpoint("decision"),
                        policies=[AsyncRetryPolicy(retry_total=0), auth],
                        connection_timeout=self.config.decision_timeout_s,
                        read_timeout=self.config.decision_timeout_s,
                    )
                )
                async with client.send_request(HttpRequest("POST", self.endpoint, json=payload)) as response:
                    response.raise_for_status()
                    return response.json()

        # Includes credential acquisition, connect and response; never stalls match frames.
        return await asyncio.wait_for(send(), timeout=self.config.decision_timeout_s)

    def _payload(self, state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
        return {
            "model": self.model,
            "state": _decision_state(state, reasons, focus_player_id),
            "questions": _systemone_questions(),
        }

    def _parse_response(self, body: dict, latency_ms: float) -> MomentDecision:
        return _parse_systemone(body, self.provider, latency_ms)


class FoundryLanguageClient(FunctionInvocationLayer, BaseChatClient):
    """Delegate protocol conversion and inference to Microsoft's framework client.

    A bounded call owns and closes its HTTP and credential clients. Returning a
    single completed text update also avoids publishing provisional persona copy.
    """

    def __init__(self, config: LLMConfig) -> None:
        super().__init__()
        self.config = config

    async def _generate(self, messages, options):
        from matchos.language_agent import render_from_messages

        config = self.config
        model = f"azure-foundry:{config.azure_reasoning_deployment}"
        call = ModelCall(
            "reasoning",
            model,
            {
                "instructions": options.get("instructions"),
                "messages": [{"role": str(message.role), "content": message.text} for message in messages],
            },
            secrets=(config.azure_key("reasoning"),),
        )

        async def generate():
            async with AsyncExitStack() as stack:
                auth = {"api_key": config.azure_key("reasoning")}
                if config.azure_auth_mode == "entra":
                    credential = await stack.enter_async_context(DefaultAzureCredential())
                    auth = {"credential": credential}
                client = OpenAIChatClient(
                    model=config.azure_reasoning_deployment,
                    azure_endpoint=config.azure_resource_endpoint("reasoning"),
                    api_version=config.azure_openai_api_version,
                    **auth,
                )
                # Framework exposes the underlying SDK client for transport settings.
                client.client.max_retries = 0
                await stack.enter_async_context(client.client)
                response = await client.get_response(messages=messages, options={**options, "store": False})
                call.result(response.to_dict())
                if not response.text or not response.text.strip():
                    raise ValueError("Empty Foundry reasoning response")
                return response.text

        try:
            return await asyncio.wait_for(generate(), timeout=config.reasoning_timeout_s)
        except asyncio.CancelledError:
            call.result({"reason": "session or request cancelled"}, phase="CANCELLED")
            raise
        except Exception as exc:
            # SDK exceptions can contain credentials or provider-returned arbitrary text.
            error = _error_metadata(exc)
            text = render_from_messages(messages)
            logger.warning("Reasoning model %s failed (%s); using the fact renderer", model, error)
            call.result({**error, "text": text}, phase="FALLBACK")
            return text

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        if stream:

            async def chunks():
                yield ChatResponseUpdate(
                    contents=[Content.from_text(await self._generate(messages, options))],
                    role="assistant",
                    author_name="matchos-language",
                    finish_reason="stop",
                )

            return ResponseStream(chunks(), finalizer=ChatResponse.from_updates)

        async def once():
            return ChatResponse(
                messages=[Message("assistant", [await self._generate(messages, options)])],
                model=self.config.azure_reasoning_deployment,
            )

        return once()


def _error_metadata(exc: Exception) -> dict:
    """Keep HTTP status even when the framework wraps an SDK exception."""
    error = {"error": type(exc).__name__}
    cause = exc
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        status = getattr(cause, "status_code", None)
        if isinstance(status, int):
            error["status"] = status
            break
        cause = cause.__cause__
    return error
