"""MatchOS server. The pitch and the intelligence path share one websocket."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from matchos.teams import identity_context
from matchos.access import AccessConfig, AccessMiddleware
from matchos.editorial import explain
from matchos.perspective import Perspective
from matchos.llm_config import load_env_files
from matchos.model_logging import configure_model_logging, logger as model_logger
from matchos.observability import configure_observability, flush_observability
from matchos.scenarios import SCENARIOS
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.session import MatchSession
from matchos.demo import DEMO_STOPS, demo_viewers, play_checkpoint, review_checkpoint
from matchos.speech import SpeechConfig, SpeechService, SpeechUnavailable

WEB = Path(__file__).resolve().parent / "web"

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env_files()
    app.state.access = AccessConfig.from_env()
    app.state.max_sessions = int(os.getenv("MATCHOS_MAX_SESSIONS", "0"))
    if app.state.max_sessions < 0:
        raise ValueError("MATCHOS_MAX_SESSIONS must be zero (unlimited) or positive")
    app.state.active_sessions = 0
    configure_observability()
    configure_model_logging()
    try:
        speech_config = SpeechConfig.from_env()
    except ValueError:
        logging.getLogger(__name__).warning("Invalid Speech configuration; check .env.example. Audio is disabled.")
        speech_config = SpeechConfig()
    app.state.speech = SpeechService(speech_config)
    model_logger.info("Logging enabled → stdout; python=%s; model requests and responses will appear here", sys.executable)
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
    return Response(audio, media_type="audio/mpeg", headers={
        "Cache-Control": "private, max-age=3600", "X-MatchOS-Audio-Cache": "hit" if cached else "miss"})


@app.get("/")
async def index() -> HTMLResponse:
    # Keep markup and assets in sync across edits, including previously cached CSS.
    html = (WEB / "index.html").read_text()
    for filename in ("styles.css", "audio.js", "app.js"):
        path = f"/assets/{filename}"
        if path not in html:
            continue
        version = hashlib.sha256((WEB / filename).read_bytes()).hexdigest()[:12]
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
    # One event loop owns this counter; admission has no await before increment.
    if app.state.max_sessions and app.state.active_sessions >= app.state.max_sessions:
        await websocket.close(code=1013, reason="MatchOS is at capacity; please retry shortly.")
        return
    app.state.active_sessions += 1
    try:
        await _serve_match_socket(websocket)
    finally:
        app.state.active_sessions -= 1


async def _serve_match_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "hello", "scenarios": _scenario_payload(), "roster": identity_context(),
                               "demoStops": DEMO_STOPS, "speech": app.state.speech.capabilities()})
    session: MatchSession | None = None
    task: asyncio.Task | None = None
    paused_task: asyncio.Task | None = None
    demo_task: asyncio.Task | None = None
    demo_results: dict[int, dict] = {}
    perspectives: list[Perspective] = []
    comparison_viewers: list[ViewerProfile] = []
    lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        if payload.get("type") == "viewer_update" and session is not None:
            payload["speech"] = app.state.speech.register(payload, session.telemetry, session.live_state_engine.state.timestamp_ms)
        async with lock:
            await websocket.send_json(payload)

    async def stop() -> None:
        nonlocal task, paused_task, demo_task
        if session is not None:
            session.running = False
        if demo_task is not None:
            demo_task.cancel()
            try:
                await demo_task
            except asyncio.CancelledError:
                pass
            demo_task = None
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
            if current.demo_step is not None:
                demo_results[payload["index"]] = payload
                await send({"type": "demo_progress", "step": current.demo_step,
                            "requestId": current.demo_request_id,
                            "settled": len(demo_results), "total": len(perspectives)})

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

    async def run_demo(current: MatchSession, step: int) -> None:
        try:
            checkpoint = DEMO_STOPS[step]
            await send({"type": "demo_status", "step": step, "requestId": current.demo_request_id, "phase": "preparing"})
            frame = await current.seek_to(checkpoint["timestamp_ms"])
            await send({"type": "demo_snapshot", "step": step, "requestId": current.demo_request_id, "frame": frame.model_dump(),
                        "state": current.live_state_engine.state.model_dump(),
                        "events": [event.model_dump() for event in list(current.live_state_engine.events)[-5:]],
                        "viewers": [viewer.model_dump() for viewer in demo_viewers()]})
            batch = await review_checkpoint(current)
            await send({"type": "demo_status", "step": step, "requestId": current.demo_request_id, "phase": "evaluating",
                        "candidates": batch.candidates.model_dump(mode="json")})
            await compare(current, batch)
            decision_timeout = getattr(getattr(current.services.decisions, "config", None), "decision_timeout_s", 10)
            settled = asyncio.Event()

            async def finish_reviews():
                await asyncio.wait_for(asyncio.gather(*[perspective.task for perspective in perspectives
                                                       if perspective.task is not None]),
                                       timeout=decision_timeout + current.services.reasoning_timeout_s + 5)
                settled.set()

            workers = [asyncio.create_task(finish_reviews()),
                       asyncio.create_task(play_checkpoint(current, settled, send))]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                current.running = False
            await send({"type": "demo_status", "step": step, "requestId": current.demo_request_id, "phase": "ready",
                        "running": False,
                        "results": [demo_results[index] for index in sorted(demo_results)]})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning("Guided demo checkpoint failed (%s)", type(exc).__name__)
            await send({"type": "demo_status", "step": step, "requestId": current.demo_request_id, "phase": "error",
                        "running": False,
                        "message": "This checkpoint could not finish. Retry it or continue to the next one."})

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "demo":
                step = message.get("step", 0)
                if type(step) is not int or not 0 <= step < len(DEMO_STOPS):
                    await send({"type": "demo_status", "phase": "error", "message": "Unknown demo checkpoint."})
                    continue
                # Keep the demo's trace history across its three checkpoint clips.
                previous_telemetry = session.telemetry if session is not None and session.demo_step is not None else None
                await stop()
                viewers = demo_viewers()
                session = MatchSession("press_collapse", 47129, 1, viewers[0], running=False, demo_step=step)
                session.demo_request_id = message.get("requestId")
                if previous_telemetry is not None:
                    session.telemetry = previous_telemetry
                comparison_viewers = viewers[1:]
                demo_results = {}
                await configure_views(session)
                demo_task = asyncio.create_task(run_demo(session, step))
            elif kind == "start":
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
                if session.demo_step is not None:
                    continue
                comparison_viewers = [ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]]
                await configure_views(session)
            elif kind == "viewer" and session is not None:
                if session.demo_step is not None:
                    continue
                session.viewer = ViewerProfile.model_validate(message.get("viewer") or {})
                session.active = None
                await configure_views(session)
            elif kind == "control" and session is not None:
                if session.demo_step is not None:
                    continue
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
                if session.demo_step is not None:
                    continue
                session.speed = int(message.get("speed") or session.speed)
    except WebSocketDisconnect:
        pass
    finally:
        await stop()


app.mount("/assets", StaticFiles(directory=WEB), name="assets")
