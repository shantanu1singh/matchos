"""Guided reviews evaluate real checkpoint evidence alongside short live clips."""

import asyncio

from matchos.runtime import SendMessage
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.session import MatchSession

DEMO_PLAYBACK_MS = 12_000
DEMO_AFTER_REVIEW_MS = 3_000

DEMO_STOPS = (
    {
        "id": "press",
        "title": "The press opens up",
        "timestamp_ms": 64 * 60000,
        "description": "Inspect the pressing evidence. Which change is worth a broadcast cue, tactical analysis or a fan update?",
    },
    {
        "id": "goal",
        "title": "Liverpool take the lead",
        "timestamp_ms": 65 * 60000 + 4000,
        "description": "The same goal changes the score for everyone. Compare a neutral headline, a supporter reaction and the analyst's context.",
    },
    {
        "id": "card",
        "title": "City lose a player",
        "timestamp_ms": 68 * 60000 + 1000,
        "description": "A verified dismissal creates a player advantage. Notice how each perspective explains it without inventing a tactical consequence.",
    },
)


def demo_viewers() -> list[ViewerProfile]:
    return [
        ViewerProfile(persona="broadcaster"),
        ViewerProfile(persona="fan", team_id="RED", language="es"),
        ViewerProfile(persona="analyst"),
    ]


async def play_checkpoint(session: MatchSession, settled: asyncio.Event, send: SendMessage) -> None:
    """Play at 1x until both the minimum clip and a post-review reading beat finish."""
    stop_at = session.simulator.clock_ms + DEMO_PLAYBACK_MS
    review_finished = False
    session.running = True
    try:
        await send(
            {
                "type": "demo_status",
                "step": session.demo_step,
                "requestId": session.demo_request_id,
                "phase": "playing",
                "running": True,
            }
        )
        loop = asyncio.get_running_loop()
        while True:
            if settled.is_set() and not review_finished:
                stop_at = max(stop_at, session.simulator.clock_ms + DEMO_AFTER_REVIEW_MS)
                review_finished = True
            if review_finished and session.simulator.clock_ms >= stop_at:
                break
            started = loop.time()
            frame, events, _ = await session.tick(evaluate=False)
            await send(
                {
                    "type": "frame",
                    "frame": frame.model_dump(),
                    "events": [event.model_dump() for event in events],
                    "state": session.live_state_engine.state.model_dump(),
                }
            )
            await asyncio.sleep(max(0.0, 0.1 - (loop.time() - started)))
    finally:
        session.running = False


async def review_checkpoint(session: MatchSession) -> PipelineResult:
    """Review a frozen replay through the session's public evidence interface."""
    return await session.review()
