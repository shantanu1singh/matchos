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

from matchos.copy import render_overlay
from matchos.language_agent import personalize
from matchos.llm_config import load_env_files
from matchos.model_logging import configure_model_logging, logger as model_logger
from matchos.scenarios import SCENARIOS
from matchos.schemas import ViewerProfile
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
            if result.kind == "moment":
                if session.take_air(result):
                    await send({"type": "moment", "result": result.model_dump()})
                    if compare is not None:
                        await compare(session)
                else:
                    await send({"type": "trace", "result": result.model_dump()})
            elif result.kind == "enrichment" and session.active and session.active.moment and result.moment_id == session.active.moment.id:
                session.active.overlay = result.overlay
                session.active.enrichment_text = result.enrichment_text
                await send({"type": "enrichment", "result": result.model_dump()})
            elif result.kind == "trace":
                await send({"type": "trace", "result": result.model_dump()})
        elapsed = asyncio.get_event_loop().time() - loop_started
        await asyncio.sleep(max(0.0, 0.1 - elapsed))


@app.websocket("/ws")
async def match_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "hello", "scenarios": _scenario_payload()})
    session: MatchSession | None = None
    task: asyncio.Task | None = None
    reframe_task: asyncio.Task | None = None
    comparison_task: asyncio.Task | None = None
    comparison_viewers: list[ViewerProfile] = []
    lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with lock:
            await websocket.send_json(payload)

    async def stop() -> None:
        nonlocal task, reframe_task, comparison_task
        if session is not None:
            session.running = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            task = None
        if reframe_task is not None:
            reframe_task.cancel()
            try:
                await reframe_task
            except asyncio.CancelledError:
                pass
            reframe_task = None
        if comparison_task is not None:
            comparison_task.cancel()
            try:
                await comparison_task
            except asyncio.CancelledError:
                pass
            comparison_task = None
        if session is not None:
            await session.close()

    async def enrich_viewer(current: MatchSession) -> None:
        overlay = await current.reframe()
        if overlay is not None and current.active is not None:
            await send({"type": "reframe", "overlay": overlay.model_dump(),
                        "moment": current.active.moment.model_dump() if current.active.moment else None})

    async def compare(current: MatchSession) -> None:
        nonlocal comparison_task
        if comparison_task is not None:
            comparison_task.cancel()
            try:
                await comparison_task
            except asyncio.CancelledError:
                pass
            comparison_task = None
        if not comparison_viewers or current.active is None or current.active.moment is None:
            return
        moment = current.active.moment
        viewers = [viewer.model_copy(deep=True) for viewer in comparison_viewers]
        overlays = [render_overlay(moment, viewer, rich=True, provider="template") for viewer in viewers]
        for overlay in overlays:
            overlay.source = "template"
        await send({"type": "comparison", "moment": moment.model_dump(),
                    "overlays": [overlay.model_dump() for overlay in overlays]})

        async def enrich(index: int, viewer: ViewerProfile) -> None:
            if current.services.agent is None:
                return
            try:
                with current.telemetry.capture("Persona comparison", {"moment": moment.id, "timestamp_ms": moment.valid_from_ms, "viewer": viewer.model_dump()}):
                    text = await personalize(current.services.agent, moment, viewer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning("Persona enrichment failed (%s); retaining template", type(exc).__name__)
                return
            if current is not session or current.active is None or current.active.moment is not moment:
                return
            overlay = render_overlay(moment, viewer, rich=True, provider=current.services.language_provider)
            overlay.body = text
            if viewer.persona == "broadcaster":
                overlay.headline = text
            await send({"type": "comparison_enrichment", "momentId": moment.id,
                        "index": index, "overlay": overlay.model_dump()})

        async def enrich_all() -> None:
            await asyncio.gather(*(enrich(index, viewer) for index, viewer in enumerate(viewers)))

        comparison_task = asyncio.create_task(enrich_all())

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
                session.running = was_running
                frame = await session.seek_to(int(message.get("timestamp_ms", 0)))
                await send({"type": "seeked", "frame": frame.model_dump(),
                            "state": session.live_state_engine.state.model_dump(),
                            "running": was_running})
                if was_running:
                    task = asyncio.create_task(_run(websocket, session, send, compare))
            elif kind == "views" and session is not None:
                comparison_viewers = [ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]]
                await compare(session)
            elif kind == "viewer" and session is not None:
                if reframe_task is not None:
                    reframe_task.cancel()
                    try:
                        await reframe_task
                    except asyncio.CancelledError:
                        pass
                session.viewer = ViewerProfile.model_validate(message.get("viewer") or {})
                overlay = session.reframe_template()
                if overlay is not None and session.active is not None:
                    await send(
                        {
                            "type": "reframe",
                            "overlay": overlay.model_dump(),
                            "moment": session.active.moment.model_dump() if session.active.moment else None,
                        }
                    )
                reframe_task = asyncio.create_task(enrich_viewer(session))
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
