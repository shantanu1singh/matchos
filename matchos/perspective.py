"""A bounded background editorial worker for one viewer of a shared match."""
from __future__ import annotations
import asyncio
import logging
import time
from copy import deepcopy

from matchos.editorial import explain
from matchos.language_agent import final_overlay
from matchos.producer import Producer
from matchos.schemas import PipelineResult, ProducerDecision, VerificationResult


class Perspective:
    def __init__(self, session, viewer, index, send):
        self.session, self.viewer, self.index, self.send = session, viewer.model_copy(deep=True), index, send
        self.producer = Producer()
        self.recent = []
        self.active = None
        self.on_air_at = 0
        self.pending = None
        self.task = None
        self.closed = False
        self.last_batch = None

    def offer(self, batch):
        if self.closed or batch.candidates is None or batch.state is None:
            return
        batch_id = (batch.state.timestamp_ms, tuple(item.id for item in batch.candidates.candidates))
        if batch_id == self.last_batch:
            return
        self.last_batch = batch_id
        self.pending = batch
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
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

    async def emit(self, result, phase="decision"):
        if not self.closed:
            await self.send({"type": "viewer_update", "index": self.index, "profile": self.viewer.model_dump(),
                             "phase": phase, "result": result.model_dump(mode="json"),
                             "explanation": explain(result, self.viewer)})

    async def run(self):
        while self.pending is not None and not self.closed:
            batch, self.pending = self.pending, None
            try:
                with self.session.telemetry.capture("Viewer editorial selection", {
                    "timestamp_ms": batch.state.timestamp_ms, "viewer": self.viewer.model_dump(), "viewIndex": self.index}):
                    await self.process(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning("Viewer selection failed (%s); frames continue", type(exc).__name__)
                result = PipelineResult(kind="trace", candidates=batch.candidates, state=batch.state,
                                        producer=ProducerDecision(action="SUPPRESS", reason="This perspective could not update. Watching for the next moment."))
                await self.emit(result)

    async def process(self, batch):
        from matchos.telemetry import record
        candidates = [candidate for candidate in batch.candidates.candidates
                      if candidate.expires_at_ms > self.session.live_state_engine.state.timestamp_ms
                      and not self.producer.has_aired(candidate)]
        result = PipelineResult(kind="trace", candidates=batch.candidates, state=batch.state, trigger=batch.trigger)
        if not candidates:
            if batch.candidates.candidates:
                result.producer = ProducerDecision(action="SUPPRESS", reason="Available stories have already aired or their live window has passed.")
            else:
                result.producer = ProducerDecision(action="REJECT", reason="No story passed the evidence checks.")
            await self.emit(result)
            return
        now = time.monotonic()
        context = {"triggerReasons": batch.trigger.reasons if batch.trigger else [],
                   "recentUpdates": [{**{key: value for key, value in item.items() if key != "aired_at"},
                                      "secondsAgo": round(now - item["aired_at"], 1)} for item in self.recent]}
        result.decision = await self.session.services.decisions.select(batch.state, candidates, self.viewer, context)
        selected = next((candidate for candidate in candidates if candidate.id == result.decision.selected_candidate_id), None)
        if result.decision.selected_candidate_id and selected is None:
            raise ValueError("Unverified candidate selected")
        if selected is None:
            result.producer = ProducerDecision(action="SUPPRESS", reason="Decision model chose no update.")
        elif selected.expires_at_ms <= self.session.live_state_engine.state.timestamp_ms:
            result.producer = ProducerDecision(action="SUPPRESS", reason="The match moved past this story's live window while waiting for the decision model.")
        else:
            result.moment = selected.model_copy(update={"importance": result.decision.importance / 5,
                                                       "confidence": result.decision.primary_frame_confidence})
            result.verification = VerificationResult(verified=True, moment=result.moment, checks=result.moment.checks)
            # Check eligibility without recording an air time before language is ready.
            result.producer = deepcopy(self.producer).decide(result.moment, True, result.decision.should_surface, self.viewer)
            now = time.monotonic()
            if (result.producer.action == "AIR" and self.active and now - self.on_air_at < 40
                    and self.active.expires_at_ms > batch.state.timestamp_ms
                    and result.moment.frame not in {"GOAL", "COMEBACK", "RED_CARD"}):
                result.producer = ProducerDecision(action="SUPPRESS", reason="A graphic is already on air for this viewer.")
            if result.producer.action == "AIR":
                overlay = await final_overlay(self.session.services.agent, result.moment, self.viewer,
                                              self.session.services.language_provider,
                                              getattr(self.session.services, "reasoning_timeout_s", 15))
                if self.closed:
                    return
                if result.moment.expires_at_ms <= self.session.live_state_engine.state.timestamp_ms:
                    result.producer = ProducerDecision(action="SUPPRESS", reason="The match moved past this story's live window while waiting for its final text.")
                else:
                    result.producer = self.producer.decide(result.moment, True, result.decision.should_surface, self.viewer)
                    if result.producer.action == "AIR":
                        result.kind, result.overlay = "moment", overlay
                        now = time.monotonic()
                        self.active, self.on_air_at = result.moment, now
                        self.recent = (self.recent + [{"aired_at": now, "frame": result.moment.frame,
                                                      "momentId": result.moment.id, "headline": overlay.headline}])[-10:]
        record("viewer production", {"viewer": self.viewer.model_dump(), "candidateIds": [candidate.id for candidate in candidates]},
               {"selection": result.decision.model_dump(), "producer": result.producer.model_dump(), "explanation": explain(result, self.viewer)})
        await self.emit(result)
