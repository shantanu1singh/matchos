"""Versioned publication packets and bounded, viewer-specific aired-story history."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from matchos.schemas import PipelineResult, ViewerProfile


class StoryOutput:
    def __init__(self):
        self.stream_id = uuid.uuid4().hex
        self.sequence = 0
        self.profiles = {}
        self.history = {}

    def reset(self, *, retain=False):
        self.stream_id = uuid.uuid4().hex
        self.sequence = 0
        if not retain:
            self.profiles.clear()
            self.history.clear()
        return {"type": "story_reset", "stream_id": self.stream_id, "retain_history": retain}

    def configure(self, viewers):
        for index in list(self.profiles):
            if index >= len(viewers) or self.profiles[index] != viewers[index].model_profile():
                self.profiles.pop(index, None)
                self.history.pop(index, None)
        for index, viewer in enumerate(viewers):
            self.profiles[index] = viewer.model_profile()

    def publish(self, result: PipelineResult, viewer: ViewerProfile, index: int, now_ms: int):
        if not (
            result.kind == "moment"
            and result.producer
            and result.producer.action == "AIR"
            and result.overlay
            and result.moment
            and result.moment.expires_at_ms > now_ms
        ):
            return None
        moment, overlay = result.moment, result.overlay
        profile = viewer.model_profile()
        if self.profiles.get(index) != profile:
            self.history.pop(index, None)
        self.profiles[index] = profile
        key = json.dumps(
            [profile, moment.frame, moment.valid_from_ms, sorted(moment.evidence), moment.facts],
            sort_keys=True,
        )
        story_id = hashlib.sha256(key.encode()).hexdigest()[:24]
        history = self.history.setdefault(index, [])
        if any(item["story_id"] == story_id for item in history):
            return None
        self.sequence += 1
        packet = {
            "schema_version": "matchos.insight.v1",
            "event": "story.published",
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "story_id": story_id,
            "published_at": datetime.now(UTC).isoformat(),
            "published_at_match_ms": now_ms,
            "viewer_index": index,
            "viewer": profile,
            "frame": moment.frame,
            "clock": {
                "timebase": "match-elapsed-ms",
                "valid_from_ms": moment.valid_from_ms,
                "expires_at_ms": moment.expires_at_ms,
            },
            "content": {
                "headline": overlay.headline,
                "body": overlay.body,
                "kicker": overlay.kicker,
                "stats": overlay.stats,
                "graphic": overlay.graphic,
            },
            "evidence": {
                "facts": moment.facts,
                "source_event_ids": moment.evidence,
                "source_events": [item.model_dump(mode="json") for item in moment.evidence_detail],
                "checks": [item.model_dump(mode="json") for item in moment.checks],
            },
            "provenance": {
                "decision_provider": result.decision.provider if result.decision else None,
                "copy_provider": overlay.provider,
                "copy_source": overlay.source,
                "selection_confidence": moment.confidence,
            },
        }
        history.append(packet)
        self.history[index] = history[-60:]
        return packet

    def snapshot(self, now_ms=0):
        views = []
        titles = {
            "en": "Your match so far",
            "es": "Tu partido hasta ahora",
            "fr": "Votre match jusqu’ici",
            "hi": "आपका मैच अब तक",
        }
        for index, profile in self.profiles.items():
            stories = sorted(
                (item for item in self.history.get(index, []) if item["published_at_match_ms"] <= now_ms),
                key=lambda p: p["published_at_match_ms"],
            )
            # Keep the latest development of each thread, with major events kept separately.
            threads = {}
            for packet in stories:
                key = (
                    packet["story_id"]
                    if packet["frame"] in {"GOAL", "COMEBACK", "RED_CARD"}
                    else (
                        packet["frame"],
                        packet["evidence"]["facts"].get("defendingTeam"),
                        packet["evidence"]["facts"].get("leadingTeam"),
                    )
                )
                threads[key] = packet
            recap = sorted(threads.values(), key=lambda p: p["published_at_match_ms"])[-4:]
            views.append(
                {
                    "index": index,
                    "viewer": profile,
                    "stories": stories,
                    "recap": {
                        "title": titles.get(profile["language"], titles["en"]),
                        "through_ms": now_ms,
                        "source": "aired-story-extract",
                        "items": [
                            {
                                "story_id": p["story_id"],
                                "timestamp_ms": p["published_at_match_ms"],
                                "headline": p["content"]["headline"],
                                "body": p["content"]["body"],
                            }
                            for p in recap
                        ],
                    },
                }
            )
        return {"type": "story_history", "stream_id": self.stream_id, "views": views}
