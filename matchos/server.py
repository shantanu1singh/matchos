"""MatchOS server. The pitch and the intelligence path share one websocket."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from matchos.teams import identity_context
from matchos.editorial import explain
from matchos.perspective import Perspective
from matchos.llm_config import load_env_files
from matchos.model_logging import configure_model_logging, logger as model_logger
from matchos.scenarios import SCENARIOS
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.session import MatchSession

WEB = Path(__file__).resolve().parent / "web"

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env_files()
    if os.getenv("MATCHOS_OTEL_ENABLED", "").lower() in {"1", "true"}:
        from agent_framework.observability import configure_otel_providers
        configure_otel_providers(service_name="matchos", enable_sensitive_data=False)
    configure_model_logging()
    model_logger.info("Logging enabled → stdout; python=%s; model requests and responses will appear here", sys.executable)
    yield


app = FastAPI(title="MatchOS", lifespan=lifespan)


def _scenario_payload() -> list[dict[str, object]]:
    return [
        {
            "id": scenario.id,
            "label": scenario.label,
            "description": scenario.description,
            "defaultSpeed": scenario.default_speed,
            "startMinute": scenario.start_minute,
        }
        for scenario in SCENARIOS.values()
    ]


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "product": "matchos"}


@app.get("/")
async def index() -> HTMLResponse:
    # Keep markup and assets in sync across edits, including previously cached CSS.
    html = (WEB / "index.html").read_text()
    for filename in ("styles.css", "app.js"):
        version = hashlib.sha256((WEB / filename).read_bytes()).hexdigest()[:12]
        path = f"/assets/{filename}"
        html = html.replace(path, f"{path}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def _run(websocket: WebSocket, session: MatchSession, send, compare=None) -> None:
    await send(
        {
            "type": "status",
            "running": session.running,
            "scenario": session.scenario_id,
            "seed": session.seed,
            "speed": session.speed,
            "viewer": session.viewer.model_dump(),
            "languageProvider": session.services.language_provider,
        }
    )
    while session.running:
        loop_started = asyncio.get_event_loop().time()
        frame, events, outputs = await session.tick()
        await send(
            {
                "type": "frame",
                "frame": frame.model_dump(),
                "events": [event.model_dump() for event in events],
                "state": session.live_state_engine.state.model_dump(),
            }
        )
        for result in outputs:
            if result.kind == "candidates":
                if compare is not None:
                    await compare(session, result)
            elif result.kind == "moment":
                if session.take_air(result):
                    await send({"type": "moment", "result": result.model_dump()})
                else:
                    await send({"type": "trace", "result": result.model_dump()})
            elif result.kind == "enrichment" and session.active and session.active.moment and result.moment_id == session.active.moment.id:
                session.active.overlay = result.overlay
                session.active.enrichment_text = result.enrichment_text
                await send({"type": "enrichment", "result": result.model_dump()})
            elif result.kind == "trace":
                await send({"type": "trace", "result": result.model_dump()})
            if result.kind in {"moment", "trace"}:
                await send({"type": "viewer_update", "index": 0, "profile": session.viewer.model_dump(),
                            "phase": "decision", "result": result.model_dump(), "explanation": explain(result, session.viewer)})
        elapsed = asyncio.get_event_loop().time() - loop_started
        await asyncio.sleep(max(0.0, 0.1 - elapsed))


@app.websocket("/ws")
async def match_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "hello", "scenarios": _scenario_payload(), "roster": identity_context()})
    session: MatchSession | None = None
    task: asyncio.Task | None = None
    paused_task: asyncio.Task | None = None
    perspectives: list[Perspective] = []
    comparison_viewers: list[ViewerProfile] = []
    lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with lock:
            await websocket.send_json(payload)

    async def stop() -> None:
        nonlocal task, paused_task
        if session is not None:
            session.running = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            task = None
        if paused_task is not None:
            paused_task.cancel()
            try:
                await paused_task
            except asyncio.CancelledError:
                pass
            paused_task = None
        for perspective in perspectives:
            await perspective.close()
        perspectives.clear()
        if session is not None:
            await session.close()

    async def configure_views(current: MatchSession) -> None:
        async def publish_viewer(payload):
            if current is not session:
                return
            if payload["index"] == 0:
                result = PipelineResult.model_validate(payload["result"])
                if payload["phase"] == "decision":
                    current._remember(result)
                    if result.kind == "moment" and result.producer.action == "AIR":
                        current.active = result
                        await send({"type": "moment", "result": result.model_dump()})
                    else:
                        await send({"type": "trace", "result": result.model_dump()})
                elif current.active and current.active.moment.id == result.moment.id:
                    current.active.overlay = result.overlay
            await send(payload)

        desired = [current.viewer, *comparison_viewers]
        previous = {perspective.index: perspective for perspective in perspectives}
        updated = []
        for index, viewer in enumerate(desired):
            existing = previous.pop(index, None)
            if existing and existing.viewer == viewer:
                updated.append(existing)
            else:
                if existing:
                    await existing.close()
                updated.append(Perspective(current, viewer, index, publish_viewer))
        for removed in previous.values():
            await removed.close()
        perspectives[:] = updated
        previous_focuses = current.services.additional_focus_ids
        current.services.additional_focus_ids = [current.viewer.player_id, *[viewer.player_id for viewer in comparison_viewers if viewer.persona == "player"]]
        if current.latest_candidates and set(previous_focuses) != set(current.services.additional_focus_ids):
            batch = current.latest_candidates
            refreshed = current.services.verifier.candidates(batch.state, [], current.viewer.player_id, current.services.additional_focus_ids)
            required_players = {current.viewer.player_id, *current.services.additional_focus_ids}
            preserved = [candidate for candidate in batch.candidates.candidates
                         if candidate.frame != "PLAYER_IMPACT" or candidate.player_id in required_players]
            known = {candidate.player_id for candidate in preserved if candidate.frame == "PLAYER_IMPACT"}
            preserved.extend(candidate for candidate in refreshed.candidates
                             if candidate.frame == "PLAYER_IMPACT" and candidate.player_id not in known)
            pool = batch.candidates.model_copy(update={"candidates": preserved})
            current.latest_candidates = batch.model_copy(update={"candidates": pool})
        if current.latest_candidates is not None:
            for perspective in perspectives:
                perspective.offer(current.latest_candidates)

    async def compare(current: MatchSession, batch: PipelineResult) -> None:
        # Offering replaces one pending snapshot; the live feed never awaits models.
        for perspective in perspectives:
            perspective.offer(batch)

    async def deliver_paused(current):
        worker = current._model_task
        while not current.running and (not worker.done() or not current._outputs.empty()):
            if current._outputs.empty():
                await asyncio.sleep(.01)
                continue
            result, profile = current._outputs.get_nowait()
            if result.kind != "candidates" and profile != current.viewer:
                continue
            current._remember(result)
            if result.kind == "candidates":
                await compare(current, result)
            elif result.kind in {"moment", "trace"}:
                if result.kind == "moment":
                    current.take_air(result)
                await send({"type": "viewer_update", "index": 0, "profile": profile.model_dump(), "phase": "decision", "result": result.model_dump(), "explanation": explain(result, profile)})
            elif result.kind == "enrichment":
                await send({"type": "enrichment", "result": result.model_dump()})

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "start":
                await stop()
                viewer = ViewerProfile.model_validate(message.get("viewer") or {})
                session = MatchSession(
                    scenario_id=str(message.get("scenario") or "press_collapse"),
                    seed=int(message.get("seed") or 47129),
                    speed=int(message.get("speed") or 1),
                    viewer=viewer,
                )
                configure_model_logging()
                model_logger.info("SESSION scenario=%s decision=%s reasoning=%s; waiting for live match triggers (warmup and seeking skip historical calls)",
                                  session.scenario_id, session.services.decisions.provider, session.services.language_provider)
                comparison_viewers = [ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]]
                await configure_views(session)
                await session.warmup()
                session.running = True
                task = asyncio.create_task(_run(websocket, session, send, compare))
            elif kind == "telemetry" and session is not None:
                since = int(message.get("since", 0)) if message.get("session") == session.telemetry.id else 0
                await send({"type": "telemetry", **session.telemetry.snapshot(since)})
            elif kind == "seek" and session is not None:
                previous = session
                was_running = previous.running
                await stop()
                await send({"type": "seeking"})
                session = MatchSession(scenario_id=previous.scenario_id, seed=previous.seed,
                                       speed=previous.speed, viewer=previous.viewer)
                await configure_views(session)
                session.running = was_running
                frame = await session.seek_to(int(message.get("timestamp_ms", 0)))
                await send({"type": "seeked", "frame": frame.model_dump(),
                            "state": session.live_state_engine.state.model_dump(),
                            "running": was_running})
                if was_running:
                    task = asyncio.create_task(_run(websocket, session, send, compare))
            elif kind == "views" and session is not None:
                comparison_viewers = [ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]]
                await configure_views(session)
            elif kind == "viewer" and session is not None:
                session.viewer = ViewerProfile.model_validate(message.get("viewer") or {})
                session.active = None
                await configure_views(session)
            elif kind == "control" and session is not None:
                action = message.get("action")
                if action == "pause":
                    # Finish the short feed tick; model work runs independently.
                    session.running = False
                    if task is not None:
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        task = None
                    if session._model_task is not None and (paused_task is None or paused_task.done()):
                        paused_task = asyncio.create_task(deliver_paused(session))
                    await send(
                        {
                            "type": "status",
                            "running": False,
                            "scenario": session.scenario_id,
                            "seed": session.seed,
                            "speed": session.speed,
                        }
                    )
                elif action == "resume" and task is None:
                    session.running = True
                    task = asyncio.create_task(_run(websocket, session, send, compare))
            elif kind == "speed" and session is not None:
                session.speed = int(message.get("speed") or session.speed)
    except WebSocketDisconnect:
        pass
    finally:
        await stop()


app.mount("/assets", StaticFiles(directory=WEB), name="assets")
