"""MatchOS server. The pitch and the intelligence path share one websocket."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from matchos.scenarios import SCENARIOS
from matchos.schemas import ViewerProfile
from matchos.session import MatchSession

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="MatchOS")


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
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


async def _run(websocket: WebSocket, session: MatchSession, send) -> None:
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
                "state": session.services.state_engine.state.model_dump(),
            }
        )
        for result in outputs:
            if result.kind == "moment":
                if session.take_air(result):
                    await send({"type": "moment", "result": result.model_dump()})
                else:
                    await send({"type": "trace", "result": result.model_dump()})
            elif result.kind == "enrichment" and session.active and session.active.moment and result.moment_id == session.active.moment.id:
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
    lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with lock:
            await websocket.send_json(payload)

    async def stop() -> None:
        nonlocal task
        if session is not None:
            session.running = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            task = None

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
                await session.warmup()
                session.running = True
                task = asyncio.create_task(_run(websocket, session, send))
            elif kind == "viewer" and session is not None:
                session.viewer = ViewerProfile.model_validate(message.get("viewer") or {})
                overlay = await session.reframe()
                if overlay is not None and session.active is not None:
                    await send(
                        {
                            "type": "reframe",
                            "overlay": overlay.model_dump(),
                            "moment": session.active.moment.model_dump() if session.active.moment else None,
                        }
                    )
            elif kind == "control" and session is not None:
                action = message.get("action")
                if action == "pause":
                    session.running = False
                    await stop()
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
                    task = asyncio.create_task(_run(websocket, session, send))
            elif kind == "speed" and session is not None:
                session.speed = int(message.get("speed") or session.speed)
    except WebSocketDisconnect:
        await stop()


app.mount("/assets", StaticFiles(directory=WEB), name="assets")
