"""Language agent: a renderer, not a source of football truth.

Microsoft Agent Framework wraps either a LiteLLM provider or a local
fact-locked client in the same Agent. The language guard discards any
reply that introduces a number the verified moment does not contain before publication.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
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

from matchos.teams import identity_context
from matchos.copy import prose_is_faithful, render_overlay
from matchos.llm_config import LLMConfig
from matchos.model_logging import ModelCall
from matchos.schemas import Overlay, VerifiedMoment, ViewerProfile

INSTRUCTIONS = """You are the MatchOS language renderer.
You receive one verified football moment as JSON plus a viewer profile.
Use the roster for club and player names. Internal IDs are opaque slots, never shirt numbers.
Write at most two short sentences in the requested language and persona.
Use only numbers that already appear in the facts. Do not invent metrics, events, or causes.
scoreRed/scoreBlue are the Liverpool/Manchester City scores; playersRed/playersBlue are the actual players still on the pitch. redCardsRed/redCardsBlue count dismissals. Use the current score and player advantage when relevant. A red card does not prove a later possession change was caused by it.
Possession and press bypass facts are percentages with a % unit. Always retain the % unit; never write a fractional ratio such as 0.639 possession. Distances are metres and counts are integers.
Casual: plain speech, almost no numbers.
Analyst: neutral coverage of both teams, explaining tactical changes using exact before-and-after metrics and the score/player-count context. No club allegiance; do not invent formations or tactical consequences.
Fan: same facts, framed for the viewer's team: celebrate their goals, explain setbacks and verified player advantages, never abusive or invented.
Player: what the focused player can take from the facts.
Broadcaster: one neutral headline for the whole audience, with no club allegiance and no adjectives you cannot prove.
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
        call = ModelCall("reasoning", "local-renderer", [{"role": str(message.role), "content": message.text} for message in messages])
        text = render_from_messages(messages)
        call.result({"text": text})

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
            call = ModelCall("reasoning", self.config.reasoning_model,
                             {"instructions": options.get("instructions"),
                              "messages": [{"role": str(message.role), "content": message.text} for message in messages]},
                             secrets=(self.config.reasoning_api_key, self.config.gateway_api_key, self.config.cloudflare_api_token))
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
                call.result(response.model_dump() if hasattr(response, "model_dump") else {"text": response.choices[0].message.content})
                text = response.choices[0].message.content
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Empty reasoning response")
                return text
            except asyncio.CancelledError:
                call.result({"reason": "session or request cancelled"}, phase="CANCELLED")
                raise
            except ModuleNotFoundError as exc:
                logging.getLogger(__name__).warning(
                    "Reasoning dependency %s is missing from %s; install the project requirements. "
                    "Using the fact renderer",
                    exc.name, sys.executable,
                )
                text = render_from_messages(messages)
                call.result({"error": "missing dependency", "dependency": exc.name, "text": text}, phase="FALLBACK")
                return text
            except Exception as exc:
                # Provider errors must not remove an already verified overlay.
                # Do not log exception text: SDK errors may include credentials.
                logging.getLogger(__name__).warning(
                    "Reasoning model %s failed; using the fact renderer", self.config.reasoning_model
                )
                text = render_from_messages(messages)
                call.result({"error": type(exc).__name__, "text": text}, phase="FALLBACK")
                return text

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
    if "fallbackText" in payload:
        return payload["fallbackText"]
    moment = VerifiedMoment.model_validate(payload["moment"])
    viewer = ViewerProfile.model_validate(payload["viewer"])
    overlay = render_overlay(moment, viewer, rich=True, provider="local-renderer")
    return overlay.body


PERCENT_FACTS = {"possessionLast5", "pressBypassBefore", "pressBypassNow"}


def prompt_payload(moment: VerifiedMoment, viewer: ViewerProfile) -> str:
    # Keep canonical facts untouched for arithmetic and evidence inspection.
    facts = {key: f"{round(float(value) * 100)}%" if key in PERCENT_FACTS else value
             for key, value in moment.facts.items()}
    return json.dumps({
        "roster": identity_context(),
        "viewer": viewer.model_profile(),
        "moment": {"id": moment.id, "type": moment.type, "frame": moment.frame,
                   "team_id": moment.team_id, "player_id": moment.player_id, "facts": facts},
        "units": {"possessionLast5": "percent", "pressBypassBefore": "percent",
                  "pressBypassNow": "percent", "distances": "metres"},
        "fallbackText": render_overlay(moment, viewer, rich=True, provider="template").body,
    }, default=str)


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
    ratios = {ratio for key, value in moment.facts.items() if key in PERCENT_FACTS
              for ratio in (float(value), round(float(value), 2))}
    fractional = any(float(number) in ratios and 0 < float(number) < 1
                     for number in re.findall(r"(?<![\d.])\d+\.\d+(?![\d.])", cleaned))
    accepted = bool(cleaned and not fractional and prose_is_faithful(cleaned, moment))
    result = cleaned if accepted else render_overlay(moment, viewer, rich=True, provider="template").body
    from matchos.telemetry import record
    record("language fact guard", {"text": cleaned, "canonicalFacts": moment.facts},
           {"accepted": accepted, "text": result}, status="complete" if accepted else "fallback")
    return result


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
    checked = faithful_text(text, moment, viewer)
    if checked != " ".join(text.strip().split()):
        logging.getLogger("matchos.model_calls").info("Reasoning text rejected by fact check for moment %s persona=%s; template=%s", moment.id, viewer.persona, checked)
    return checked


async def final_overlay(agent: Agent | None, moment: VerifiedMoment, viewer: ViewerProfile,
                        provider: str, timeout_s: float = 15) -> Overlay:
    """Prepare one complete card; never publish intermediate template copy."""
    fallback = render_overlay(moment, viewer, rich=True, provider="template")
    if agent is None:
        fallback.source = "template"
        return fallback
    try:
        text = await asyncio.wait_for(personalize(agent, moment, viewer), timeout=timeout_s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Framework errors and the overall deadline also need a usable final card.
        logging.getLogger(__name__).warning("Viewer language failed (%s); using persona fallback", type(exc).__name__)
        from matchos.telemetry import record
        record("viewer language fallback", {"momentId": moment.id, "viewer": viewer.model_profile()},
               {"error": type(exc).__name__}, status="fallback")
        fallback.source = "template"
        return fallback
    overlay = render_overlay(moment, viewer, rich=True, provider=provider)
    overlay.body = text
    if viewer.persona == "broadcaster":
        overlay.headline = text
    return overlay
