"""Bounded scheduling and delivery for one viewer's Agent Framework workflow."""

from __future__ import annotations

import asyncio
import logging
import time

from matchos.editorial import explain
from matchos.publication import PublicationPolicy
from matchos.runtime import SendMessage, ViewerDependencies
from matchos.schemas import PipelineResult, ProducerDecision, VerifiedMoment, ViewerProfile
from matchos.viewer_workflow import build_viewer_workflow


class Perspective:
    def __init__(
        self, dependencies: ViewerDependencies, viewer: ViewerProfile, index: int, send: SendMessage
    ) -> None:
        self.dependencies = dependencies
        self.viewer = viewer.model_copy(deep=True)
        self.index, self.send = index, send
        self.publication = PublicationPolicy()
        self.recent: list[dict] = []
        self.active: VerifiedMoment | None = None
        self.on_air_at = 0
        self.pending: PipelineResult | None = None
        self.task: asyncio.Task[None] | None = None
        self.closed = False
        self.last_batch: tuple[int, tuple[str, ...]] | None = None
        self.started_at = time.monotonic()
        self.workflow = build_viewer_workflow(self)

    def offer(self, batch: PipelineResult) -> None:
        if self.closed or batch.candidates is None or batch.state is None:
            return
        batch_id = (batch.state.timestamp_ms, tuple(item.id for item in batch.candidates.candidates))
        if batch_id == self.last_batch:
            return
        self.last_batch = batch_id
        self.pending = batch
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        self.closed = True
        self.pending = None
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logging.getLogger(__name__).debug("Viewer worker closed after %s", type(exc).__name__)

    async def emit(self, result: PipelineResult, phase: str = "decision") -> None:
        if not self.closed:
            await self.send(
                {
                    "type": "viewer_update",
                    "index": self.index,
                    "profile": self.viewer.model_dump(),
                    "phase": phase,
                    "result": result.model_dump(mode="json"),
                    "explanation": explain(result, self.viewer),
                }
            )

    async def run(self) -> None:
        while self.pending is not None and not self.closed:
            batch, self.pending = self.pending, None
            try:
                with self.dependencies.telemetry.capture(
                    "Viewer editorial selection",
                    {
                        "timestamp_ms": batch.state.timestamp_ms,
                        "viewer": self.viewer.model_dump(),
                        "viewIndex": self.index,
                        "demoCheckpoint": self.dependencies.demo_step,
                    },
                    parent=batch.trace_context,
                ):
                    await self.process(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Viewer selection failed (%s); frames continue", type(exc).__name__
                )
                self.workflow = build_viewer_workflow(self)
                result = PipelineResult(
                    kind="trace",
                    candidates=batch.candidates,
                    state=batch.state,
                    producer=ProducerDecision(
                        action="SUPPRESS",
                        reason="This perspective could not update. Watching for the next moment.",
                    ),
                )
                await self.emit(result)

    async def process(self, batch: PipelineResult) -> None:
        """Schedule one framework run; the graph owns the editorial sequence."""
        async for event in self.workflow.run(batch, stream=True):
            if event.type == "output" and isinstance(event.data, PipelineResult) and not self.closed:
                await self.emit(event.data)
