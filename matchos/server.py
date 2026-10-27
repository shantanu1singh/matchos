"""MatchOS server. The pitch and the intelligence path share one websocket."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from matchos.access import AccessConfig, AccessMiddleware
from matchos.connection import ConnectionCoordinator
from matchos.demo import DEMO_STOPS
from matchos.llm_config import load_env_files
from matchos.model_logging import configure_model_logging
from matchos.model_logging import logger as model_logger
from matchos.observability import configure_observability, flush_observability
from matchos.report_store import ReportStore
from matchos.scenarios import SCENARIOS
from matchos.session import MatchSession
from matchos.speech import SpeechConfig, SpeechService, SpeechUnavailable
from matchos.teams import identity_context

WEB = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env_files()
    app.state.access = AccessConfig.from_env()
    app.state.max_sessions = int(os.getenv("MATCHOS_MAX_SESSIONS", "0"))
    if app.state.max_sessions < 0:
        raise ValueError("MATCHOS_MAX_SESSIONS must be zero (unlimited) or positive")
    app.state.active_sessions = 0
    app.state.reports = ReportStore()
    configure_observability()
    configure_model_logging()
    try:
        speech_config = SpeechConfig.from_env()
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid Speech configuration; check .env.example. Audio is disabled."
        )
        speech_config = SpeechConfig()
    app.state.speech = SpeechService(speech_config)
    model_logger.info(
        "Logging enabled → stdout; python=%s; model requests and responses will appear here", sys.executable
    )
    try:
        yield
    finally:
        await app.state.speech.close()
        await asyncio.to_thread(flush_observability)


app = FastAPI(title="MatchOS", lifespan=lifespan)
app.add_middleware(AccessMiddleware, state=app.state)


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


@app.get("/api/speech/{speech_id}")
async def speech_audio(speech_id: str):
    service = getattr(app.state, "speech", None)
    if service is None:
        raise HTTPException(503, "Speech is not available")
    try:
        audio, cached = await service.audio(speech_id)
    except SpeechUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return Response(
        audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-MatchOS-Audio-Cache": "hit" if cached else "miss",
        },
    )


@app.get("/")
async def index() -> HTMLResponse:
    # Keep markup and assets in sync across edits, including previously cached CSS.
    html = (WEB / "index.html").read_text()
    for filename in ("styles.css", "audio.js", "telemetry.js", "demo.js", "delivery.js", "app.js"):
        path = f"/assets/{filename}"
        if path not in html:
            continue
        version = hashlib.sha256((WEB / filename).read_bytes()).hexdigest()[:12]
        html = html.replace(path, f"{path}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/broadcast")
async def broadcast_reference():
    html = (WEB / "broadcast.html").read_text()
    for name in ("styles.css", "delivery.js"):
        html = html.replace(
            f"/assets/{name}",
            f"/assets/{name}?v={hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:12]}",
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/evaluations")
async def evaluations():
    try:
        return await app.state.reports.list()
    except Exception:
        raise HTTPException(
            503, "Evaluation archive unavailable. Check storage access and configuration."
        ) from None


@app.get("/api/evaluations/{report_id}")
async def evaluation_report(report_id: str):
    try:
        return await app.state.reports.get(report_id)
    except FileNotFoundError:
        raise HTTPException(404, "Evaluation report not found") from None
    except Exception:
        raise HTTPException(503, "Evaluation report unavailable") from None


@app.websocket("/ws")
async def match_socket(websocket: WebSocket) -> None:
    # One event loop owns this counter; admission has no await before increment.
    if app.state.max_sessions and app.state.active_sessions >= app.state.max_sessions:
        await websocket.close(code=1013, reason="MatchOS is at capacity; please retry shortly.")
        return
    app.state.active_sessions += 1
    try:
        await ConnectionCoordinator(websocket, app.state.speech, session_factory=MatchSession).run(
            {
                "type": "hello",
                "scenarios": _scenario_payload(),
                "roster": identity_context(),
                "demoStops": DEMO_STOPS,
                "speech": app.state.speech.capabilities(),
            }
        )
    finally:
        app.state.active_sessions -= 1


app.mount("/assets", StaticFiles(directory=WEB), name="assets")
