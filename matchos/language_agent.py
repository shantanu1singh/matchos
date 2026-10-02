"""Language agent: a renderer, not a source of football truth.

Microsoft Agent Framework wraps either a Foundry GPT deployment or a local
fact-locked client in the same Agent. The enrichment executor discards any
reply that introduces a number the verified moment does not contain.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence

from agent_framework import (
    Agent,
    AgentExecutor,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)

from matchos.copy import prose_is_faithful, render_overlay
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


class LocalFactsChatClient(BaseChatClient):
    """Offline renderer used when no Foundry project is configured."""

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

            return ResponseStream(chunks())

        async def once() -> ChatResponse:
            return ChatResponse(messages=[Message("assistant", [text])], model="matchos-fact-renderer")

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


def build_language_executor() -> tuple[AgentExecutor, Agent, str]:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    model = os.getenv("FOUNDRY_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    provider = "local-renderer"
    if endpoint:
        from azure.identity import DefaultAzureCredential
        from agent_framework.foundry import FoundryChatClient

        client = FoundryChatClient(
            project_endpoint=endpoint,
            model=model,
            credential=DefaultAzureCredential(),
        )
        provider = f"foundry:{model}"
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
