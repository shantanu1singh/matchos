"""Guided checkpoints replay real evidence, then request the normal shared workflow."""
from matchos.schemas import PipelineResult, Tick, ViewerProfile
from matchos.workflow import LiveTick

DEMO_STOPS = (
    {"id": "press", "title": "The press opens up", "timestamp_ms": 64 * 60000,
     "description": "Inspect the pressing evidence. Which change is worth a broadcast cue, tactical analysis or a fan update?"},
    {"id": "goal", "title": "Liverpool take the lead", "timestamp_ms": 65 * 60000 + 4000,
     "description": "The same goal changes the score for everyone. Compare a neutral headline, a supporter reaction and the analyst's context."},
    {"id": "card", "title": "City lose a player", "timestamp_ms": 68 * 60000 + 1000,
     "description": "A verified dismissal creates a player advantage. Notice how each perspective explains it without inventing a tactical consequence."},
)


def demo_viewers() -> list[ViewerProfile]:
    return [ViewerProfile(persona="broadcaster"), ViewerProfile(persona="fan", team_id="RED"),
            ViewerProfile(persona="analyst")]


async def review_checkpoint(session) -> PipelineResult:
    """Review a frozen replay, using real state, verification, and framework telemetry."""
    frame = session.simulator._frame()
    tick = Tick(match_id=session.simulator.match_id, frame=frame, events=[], samples=[])
    with session.telemetry.capture("Guided checkpoint evidence", {
        "timestamp_ms": frame.timestamp_ms, "demoCheckpoint": session.demo_step,
        "viewer": session.viewer.model_dump(),
    }):
        async for event in session.workflow.run(
            LiveTick(tick=tick, viewer=session.viewer, review_requested=True), stream=True
        ):
            if isinstance(event.data, PipelineResult) and event.data.kind == "candidates":
                session._remember(event.data)
                return event.data
    raise RuntimeError("Checkpoint produced no verified candidate pool")
