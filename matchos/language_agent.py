"""Language agent: a renderer, not a source of football truth.

Microsoft Agent Framework wraps either a LiteLLM provider or a local
fact-locked client in the same Agent. The enrichment executor discards any
reply that introduces a number the verified moment does not contain.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence

from agent_framework import (
    Agent,
    AgentExecutor,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)

from matchos.copy import prose_is_faithful, render_overlay
from matchos.llm_config import LLMConfig
from matchos.schemas import VerifiedMoment, ViewerProfile

INSTRUCTIONS = """You are the MatchOS language renderer.
You receive one verified football moment as JSON plus a viewer profile.
Write at most two short sentences in the requested language and persona.
Use only numbers that already appear in the facts. Do not invent metrics, events, or causes.
Casual: plain speech, almost no numbers.
Analyst: the exact before-and-after metrics.
Fan: same facts, framed for the viewer's team, never abusive or invented.
Player: what the focused player can take from the facts.
Broadcaster: one headline, no adjectives you cannot prove.
If the language is hi, write in Hindi. Otherwise write in English.
Return only the sentence or headline, with no JSON and no preamble.
"""


class LocalFactsChatClient(FunctionInvocationLayer, BaseChatClient):
    """Offline renderer used when no reasoning provider is configured."""

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, object],
        **kwargs: object,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        text = render_from_messages(messages)

        if stream:

            async def chunks() -> AsyncIterable[ChatResponseUpdate]:
                yield ChatResponseUpdate(
                    contents=[Content.from_text(text)],
                    role="assistant",
                    author_name="matchos-language",
                    finish_reason="stop",
                )

            return ResponseStream(chunks(), finalizer=ChatResponse.from_updates)

        async def once() -> ChatResponse:
            return ChatResponse(messages=[Message("assistant", [text])], model="matchos-fact-renderer")

        return once()


class LiteLLMChatClient(FunctionInvocationLayer, BaseChatClient):
    """Adapt provider-neutral async completions to the existing AgentExecutor."""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__()
        self.config = config

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, object],
        **kwargs: object,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        async def generate() -> str:
            try:
                litellm = await asyncio.to_thread(importlib.import_module, "litellm")

                request_messages = []
                instructions = options.get("instructions")
                if instructions:
                    request_messages.append({"role": "system", "content": str(instructions)})
                request_messages.extend(
                    {"role": str(message.role), "content": message.text or ""} for message in messages
                )
                response = await asyncio.wait_for(
                    litellm.acompletion(
                        messages=request_messages,
                        **self.config.completion_options(),
                    ),
                    timeout=self.config.reasoning_timeout_s,
                )
                text = response.choices[0].message.content
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Empty reasoning response")
                return text
            except ModuleNotFoundError as exc:
                logging.getLogger(__name__).warning(
                    "Reasoning dependency %s is missing from %s; install the project requirements. "
                    "Using the fact renderer",
                    exc.name, sys.executable,
                )
                return render_from_messages(messages)
            except Exception:
                # Provider errors must not remove an already verified overlay.
                # Do not log exception text: SDK errors may include credentials.
                logging.getLogger(__name__).warning(
                    "Reasoning model %s failed; using the fact renderer", self.config.reasoning_model
                )
                return render_from_messages(messages)

        if stream:

            async def chunks() -> AsyncIterable[ChatResponseUpdate]:
                yield ChatResponseUpdate(
                    contents=[Content.from_text(await generate())],
                    role="assistant",
                    author_name="matchos-language",
                    finish_reason="stop",
                )

            return ResponseStream(chunks(), finalizer=ChatResponse.from_updates)

        async def once() -> ChatResponse:
            return ChatResponse(
                messages=[Message("assistant", [await generate()])], model=self.config.reasoning_model
            )

        return once()


def render_from_messages(messages: Sequence[Message]) -> str:
    payload = _latest_payload(messages)
    if payload is None:
        return ""
    moment = VerifiedMoment.model_validate(payload["moment"])
    viewer = ViewerProfile.model_validate(payload["viewer"])
    overlay = render_overlay(moment, viewer, rich=True, provider="local-renderer")
    return overlay.body


def prompt_payload(moment: VerifiedMoment, viewer: ViewerProfile) -> str:
    return json.dumps(
        {
            "persona": viewer.persona,
            "language": viewer.language,
            "team": viewer.team_id,
            "playerId": viewer.player_id,
            "viewer": viewer.model_dump(),
            "moment": moment.model_dump(),
        },
        default=str,
    )


def _latest_payload(messages: Sequence[Message]) -> dict | None:
    for message in reversed(messages):
        text = message.text or ""
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def faithful_text(text: str, moment: VerifiedMoment, viewer: ViewerProfile) -> str:
    cleaned = " ".join(text.strip().split())
    if cleaned and prose_is_faithful(cleaned, moment):
        return cleaned
    return render_overlay(moment, viewer, rich=True, provider="template").body


def build_language_executor(config: LLMConfig | None = None) -> tuple[AgentExecutor, Agent, str]:
    config = config or LLMConfig.from_env()
    provider = "local-renderer"
    if config.reasoning_enabled:
        client = LiteLLMChatClient(config)
        provider = f"litellm:{config.reasoning_model}"
    else:
        client = LocalFactsChatClient()
    agent = Agent(
        client=client,
        name="matchos-language",
        instructions=INSTRUCTIONS,
        description="Renders a verified moment for one viewer without adding facts.",
    )
    executor = AgentExecutor(agent, id="language_agent")
    return executor, agent, provider


async def personalize(agent: Agent, moment: VerifiedMoment, viewer: ViewerProfile) -> str:
    response = await agent.run(prompt_payload(moment, viewer))
    text = response.text or ""
    return faithful_text(text, moment, viewer)
