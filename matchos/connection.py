"""Own one browser connection's playback, perspectives and guided-demo lifecycle.

HTTP transport and application resources are composed in server.py. This class
coordinates only its own session and tasks; it does not own global app state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from matchos.demo import DEMO_STOPS, demo_viewers, play_checkpoint, review_checkpoint
from matchos.model_logging import configure_model_logging
from matchos.model_logging import logger as model_logger
from matchos.perspective import Perspective
from matchos.runtime import SendMessage
from matchos.schemas import PipelineResult, ViewerProfile
from matchos.session import MatchSession
from matchos.speech import SpeechService
from matchos.story_output import StoryOutput

CandidateSink = Callable[[MatchSession, PipelineResult], Awaitable[None]]


async def run_playback(
    session: MatchSession, send: SendMessage, compare: CandidateSink | None = None
) -> None:
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
        loop_started = asyncio.get_running_loop().time()
        frame, events, outputs = await session.tick()
        await send(
            {
                "type": "frame",
                "frame": frame.model_dump(),
                "events": [event.model_dump() for event in events],
                "state": session.state.model_dump(),
            }
        )
        for result in outputs:
            if result.kind == "candidates" and compare is not None:
                await compare(session, result)
        elapsed = asyncio.get_running_loop().time() - loop_started
        await asyncio.sleep(max(0.0, 0.1 - elapsed))


class ConnectionCoordinator:
    def __init__(
        self,
        websocket: WebSocket,
        speech: SpeechService,
        *,
        session_factory: Callable[..., MatchSession] = MatchSession,
    ) -> None:
        self.websocket = websocket
        self.speech = speech
        self.session_factory = session_factory
        self.session: MatchSession | None = None
        self.task: asyncio.Task[None] | None = None
        self.paused_task: asyncio.Task[None] | None = None
        self.demo_task: asyncio.Task[None] | None = None
        self.demo_results: dict[int, dict] = {}
        self.perspectives: list[Perspective] = []
        self.comparison_viewers: list[ViewerProfile] = []
        self.lock = asyncio.Lock()
        self.story_output = StoryOutput()

    def require_session(self) -> MatchSession:
        if self.session is None:
            raise RuntimeError("A match must be started before this operation")
        return self.session

    async def run(self, hello: dict) -> None:
        await self.websocket.accept()
        await self.send(hello)
        try:
            while True:
                await self.dispatch(await self.websocket.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            await self.stop()

    async def dispatch(self, message: dict) -> None:
        kind = message.get("type")
        handlers = {
            "start": self.handle_start,
            "demo": self.handle_demo,
            "telemetry": self.handle_telemetry,
            "seek": self.handle_seek,
            "views": self.handle_views,
            "viewer": self.handle_viewer,
            "control": self.handle_control,
            "speed": self.handle_speed,
            "stories": self.handle_stories,
        }
        handler = handlers.get(kind)
        if handler is not None and (kind in {"start", "demo"} or self.session is not None):
            await handler(message)

    async def send(self, payload: dict) -> None:
        if payload.get("type") == "viewer_update" and self.session is not None:
            payload["speech"] = self.speech.register(
                payload, self.session.telemetry, self.session.state.timestamp_ms
            )
        async with self.lock:
            await self.websocket.send_json(payload)

    async def stop(self) -> None:
        if self.session is not None:
            self.session.running = False
        tasks = [task for task in (self.demo_task, self.task, self.paused_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logging.getLogger(__name__).debug("Connection worker closed after %s", type(exc).__name__)
        self.demo_task = self.task = self.paused_task = None
        for perspective in self.perspectives:
            await perspective.close()
        self.perspectives.clear()
        if self.session is not None:
            await self.session.close()

    async def configure_views(self, current: MatchSession) -> None:
        """Reuse unchanged viewers and cancel only replaced or removed workers."""

        async def publish_viewer(payload):
            if current is not self.session:
                return
            if payload["index"] == 0:
                result = PipelineResult.model_validate(payload["result"])
                current.record_result(result)
                if result.kind == "moment" and result.producer.action == "AIR":
                    current.active = result
                    await self.send({"type": "moment", "result": result.model_dump()})
                else:
                    await self.send({"type": "trace", "result": result.model_dump()})
            await self.send(payload)
            result = PipelineResult.model_validate(payload["result"])
            packet = self.story_output.publish(
                result,
                ViewerProfile.model_validate(payload["profile"]),
                payload["index"],
                current.state.timestamp_ms,
            )
            if packet:
                await self.send({"type": "broadcast_packet", "packet": packet})
                await self.handle_stories({})
            if current.demo_step is not None:
                self.demo_results[payload["index"]] = payload
                await self.send(
                    {
                        "type": "demo_progress",
                        "step": current.demo_step,
                        "requestId": current.demo_request_id,
                        "settled": len(self.demo_results),
                        "total": len(self.perspectives),
                    }
                )

        desired = [current.viewer, *self.comparison_viewers]
        self.story_output.configure(desired)
        previous = {perspective.index: perspective for perspective in self.perspectives}
        updated = []
        for index, viewer in enumerate(desired):
            existing = previous.pop(index, None)
            if existing and existing.viewer == viewer:
                updated.append(existing)
            else:
                if existing:
                    await existing.close()
                updated.append(Perspective(current.viewer_dependencies, viewer, index, publish_viewer))
        for removed in previous.values():
            await removed.close()
        self.perspectives[:] = updated
        current.configure_focuses(self.comparison_viewers)
        await self.handle_stories({})
        if current.latest_candidates is not None:
            for perspective in self.perspectives:
                perspective.offer(current.latest_candidates)

    async def compare(self, current: MatchSession, batch: PipelineResult) -> None:
        if current is not self.session:
            return
        for perspective in self.perspectives:
            perspective.offer(batch)

    async def deliver_paused(self, current: MatchSession) -> None:
        while current is self.session and not current.running and current.processing:
            for result in current.drain_results():
                if result.kind == "candidates":
                    await self.compare(current, result)
            await asyncio.sleep(0.01)

    async def run_demo(self, current: MatchSession, step: int) -> None:
        """Review frozen evidence while a short live clip continues independently."""
        try:
            checkpoint = DEMO_STOPS[step]
            await self.send(
                {
                    "type": "demo_status",
                    "step": step,
                    "requestId": current.demo_request_id,
                    "phase": "preparing",
                }
            )
            frame = await current.seek_to(checkpoint["timestamp_ms"])
            await self.handle_stories({})
            await self.send(
                {
                    "type": "demo_snapshot",
                    "step": step,
                    "requestId": current.demo_request_id,
                    "frame": frame.model_dump(),
                    "state": current.state.model_dump(),
                    "events": [event.model_dump() for event in list(current.live_state_engine.events)[-5:]],
                    "viewers": [viewer.model_dump() for viewer in demo_viewers()],
                }
            )
            batch = await review_checkpoint(current)
            await self.send(
                {
                    "type": "demo_status",
                    "step": step,
                    "requestId": current.demo_request_id,
                    "phase": "evaluating",
                    "candidates": batch.candidates.model_dump(mode="json"),
                }
            )
            await self.compare(current, batch)
            decision_timeout = getattr(
                getattr(current.services.decisions, "config", None), "decision_timeout_s", 10
            )
            settled = asyncio.Event()

            async def finish_reviews():
                await asyncio.wait_for(
                    asyncio.gather(
                        *[
                            perspective.task
                            for perspective in self.perspectives
                            if perspective.task is not None
                        ]
                    ),
                    timeout=decision_timeout + current.services.reasoning_timeout_s + 5,
                )
                settled.set()

            workers = [
                asyncio.create_task(finish_reviews()),
                asyncio.create_task(play_checkpoint(current, settled, self.send)),
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                current.running = False
            await self.send(
                {
                    "type": "demo_status",
                    "step": step,
                    "requestId": current.demo_request_id,
                    "phase": "ready",
                    "running": False,
                    "results": [self.demo_results[index] for index in sorted(self.demo_results)],
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning("Guided demo checkpoint failed (%s)", type(exc).__name__)
            await self.send(
                {
                    "type": "demo_status",
                    "step": step,
                    "requestId": current.demo_request_id,
                    "phase": "error",
                    "running": False,
                    "message": "This checkpoint could not finish. Retry it or continue to the next one.",
                }
            )

    async def handle_demo(self, message: dict) -> None:
        step = message.get("step", 0)
        if type(step) is not int or not 0 <= step < len(DEMO_STOPS):
            await self.send({"type": "demo_status", "phase": "error", "message": "Unknown demo checkpoint."})
            return
        previous_telemetry = (
            self.session.telemetry
            if self.session is not None and self.session.demo_step is not None
            else None
        )
        await self.stop()
        viewers = demo_viewers()
        await self.send(
            self.story_output.reset(retain=previous_telemetry is not None and step > self.session.demo_step)
        )
        self.session = self.session_factory(
            "press_collapse", 47129, 1, viewers[0], running=False, demo_step=step
        )
        self.session.demo_request_id = message.get("requestId")
        if previous_telemetry is not None:
            self.session.telemetry = previous_telemetry
        self.comparison_viewers = viewers[1:]
        self.demo_results = {}
        await self.configure_views(self.session)
        self.demo_task = asyncio.create_task(self.run_demo(self.session, step))

    async def handle_start(self, message: dict) -> None:
        await self.stop()
        await self.send(self.story_output.reset())
        viewer = ViewerProfile.model_validate(message.get("viewer") or {})
        self.session = self.session_factory(
            scenario_id=str(message.get("scenario") or "press_collapse"),
            seed=int(message.get("seed") or 47129),
            speed=int(message.get("speed") or 1),
            viewer=viewer,
        )
        configure_model_logging()
        model_logger.info(
            "SESSION scenario=%s decision=%s reasoning=%s; waiting for live match triggers (warmup and seeking skip historical calls)",
            self.session.scenario_id,
            self.session.services.decisions.provider,
            self.session.services.language_provider,
        )
        self.comparison_viewers = [
            ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]
        ]
        await self.session.warmup()
        await self.configure_views(self.session)
        self.session.running = True
        self.task = asyncio.create_task(run_playback(self.session, self.send, self.compare))

    async def handle_telemetry(self, message: dict) -> None:
        current = self.require_session()
        since = int(message.get("since", 0)) if message.get("session") == current.telemetry.id else 0
        await self.send({"type": "telemetry", **current.telemetry.snapshot(since)})

    async def handle_stories(self, message: dict) -> None:
        if self.session is not None:
            await self.send(self.story_output.snapshot(self.session.state.timestamp_ms))

    async def handle_seek(self, message: dict) -> None:
        previous = self.require_session()
        was_running = previous.running
        await self.stop()
        await self.send(self.story_output.reset())
        await self.send({"type": "seeking"})
        self.session = self.session_factory(
            scenario_id=previous.scenario_id, seed=previous.seed, speed=previous.speed, viewer=previous.viewer
        )
        await self.configure_views(self.session)
        self.session.running = was_running
        frame = await self.session.seek_to(int(message.get("timestamp_ms", 0)))
        await self.send(
            {
                "type": "seeked",
                "frame": frame.model_dump(),
                "state": self.session.state.model_dump(),
                "running": was_running,
            }
        )
        if was_running:
            self.task = asyncio.create_task(run_playback(self.session, self.send, self.compare))

    async def handle_views(self, message: dict) -> None:
        current = self.require_session()
        if current.demo_step is not None:
            return
        self.comparison_viewers = [
            ViewerProfile.model_validate(viewer) for viewer in message.get("views", [])[:2]
        ]
        await self.configure_views(current)

    async def handle_viewer(self, message: dict) -> None:
        current = self.require_session()
        if current.demo_step is not None:
            return
        current.viewer = ViewerProfile.model_validate(message.get("viewer") or {})
        current.active = None
        await self.configure_views(current)

    async def handle_control(self, message: dict) -> None:
        current = self.require_session()
        if current.demo_step is not None:
            return
        action = message.get("action")
        if action == "pause":
            current.running = False
            if self.task is not None:
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                self.task = None
            if current.processing and (self.paused_task is None or self.paused_task.done()):
                self.paused_task = asyncio.create_task(self.deliver_paused(current))
            await self.send(
                {
                    "type": "status",
                    "running": False,
                    "scenario": current.scenario_id,
                    "seed": current.seed,
                    "speed": current.speed,
                }
            )
        elif action == "resume" and self.task is None:
            current.running = True
            self.task = asyncio.create_task(run_playback(current, self.send, self.compare))

    async def handle_speed(self, message: dict) -> None:
        current = self.require_session()
        if current.demo_step is not None:
            return
        current.speed = int(message.get("speed") or current.speed)
