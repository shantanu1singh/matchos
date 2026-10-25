"""Bounded, session-local execution traces; capture never awaits network I/O."""

from __future__ import annotations

import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from matchos.observability import (
    capture_span,
    compact_tracing,
    content_attribute,
    message_attribute,
    parent_context,
)

_current = ContextVar("matchos_trace", default=None)


def safe(value, secrets=()):
    import json

    from matchos.model_logging import _safe_json

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(_safe_json(value, secrets))


class Telemetry:
    def __init__(self, capacity=40):
        self.id = uuid.uuid4().hex
        self.runs = deque(maxlen=capacity)
        self.active = {}
        self.latest_quiet = None
        self.revision = 0
        self._cloud_calls = {}
        self._cloud_evidence = {}
        self.counters = {"quiet_ticks": 0, "skipped_viewer_reviews": 0}

    def touch(self, run):
        self.revision += 1
        run["revision"] = self.revision

    @contextmanager
    def capture(self, label, metadata, *, parent=None):
        # Provider exception strings can contain arbitrary content. Record only their
        # class/status below, rather than OTel's automatic exception stack capture.
        with trace.get_tracer("matchos").start_as_current_span(
            label,
            context=parent_context(parent),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            viewer = metadata.get("viewer", {})
            attributes = {"matchos.session.id": self.id}
            for key, value in metadata.items():
                if isinstance(value, (str, int, float, bool)):
                    attributes[f"matchos.{key}"] = value
            if viewer:
                attributes["matchos.persona"] = viewer.get("persona", "unknown")
            if label == "Viewer editorial selection":
                role = viewer.get("persona", "unknown")
                attributes.update(
                    {
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": f"matchos-{role}",
                        "gen_ai.agent.id": f"matchos-{role}-v1",
                        "gen_ai.conversation.id": self.id,
                    }
                )
            span.set_attributes(attributes)
            compact = compact_tracing()
            span.set_attribute("matchos.trace.mode", "compact" if compact else "full")
            if not compact:
                content_attribute(span, "matchos.metadata", metadata)
            elif label == "Viewer editorial selection":
                span.update_name(f"{viewer.get('persona', 'Viewer').capitalize()} · Evaluating")
            elif label in {"Match workflow", "Guided checkpoint evidence"}:
                span.set_attribute("gen_ai.operation.name", "invoke_workflow")
            cloud_token = capture_span.set(span)
            run = None
            try:
                with self._capture(label, metadata) as run:
                    ctx = span.get_span_context()
                    if ctx.is_valid:
                        run.update(trace_id=f"{ctx.trace_id:032x}", span_id=f"{ctx.span_id:016x}")
                    if (
                        compact
                        and label in {"Match workflow", "Guided checkpoint evidence"}
                        and span.is_recording()
                    ):
                        self._cloud_evidence[run["id"]] = trace.get_tracer("matchos").start_span(
                            "Evidence", context=trace.set_span_in_context(span)
                        )
                    yield run
            finally:
                if run is not None:
                    no_review = (
                        label == "Viewer editorial selection"
                        and run["status"] == "complete"
                        and not any(
                            step["call_id"] or step["name"] == "editorial selection" for step in run["steps"]
                        )
                    )
                    if no_review:
                        self.counters["skipped_viewer_reviews"] += 1
                    if compact:
                        skip = run["status"] == "complete" and (run.get("quiet", False) or no_review)
                        span.set_attribute("matchos.quiet", skip)
                        run["cloud_export"] = "skipped_quiet" if skip else "compact"
                        for name, value in self.counters.items():
                            span.set_attribute(f"matchos.{name}", value)
                    span.set_attribute("matchos.status", run["status"])
                    if run["status"] == "error":
                        span.set_status(StatusCode.ERROR, run.get("error", "error"))
                    evidence = self._cloud_evidence.pop(run["id"], None)
                    if evidence is not None:
                        evidence.set_attribute(
                            "matchos.quiet", run["status"] == "complete" and run.get("quiet", False)
                        )
                        if run["status"] == "error":
                            evidence.set_status(StatusCode.ERROR, run.get("error", "error"))
                        evidence.end()
                    for key in [key for key in self._cloud_calls if key[0] == run["id"]]:
                        call = self._cloud_calls.pop(key)
                        if call["status"] == "request":
                            call["span"].set_attribute(
                                "matchos.status",
                                run["status"] if run["status"] != "complete" else "incomplete",
                            )
                        call["span"].end(end_time=call["end"] or time.time_ns())
                capture_span.reset(cloud_token)

    @contextmanager
    def _capture(self, label, metadata):
        run = {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "metadata": safe(metadata),
            "status": "running",
            "steps": [],
            "started": time.perf_counter(),
        }
        self.active[run["id"]] = run
        self.touch(run)
        token = _current.set((self, run))
        try:
            yield run
        except BaseException as exc:
            run["status"] = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
            run["error"] = type(exc).__name__
            raise
        else:
            run["status"] = "complete"
        finally:
            run["duration_ms"] = round((time.perf_counter() - run["started"]) * 1000, 2)
            self.touch(run)
            if run.get("quiet"):
                self.latest_quiet = run
                # Avoid letting quiet ticks evict meaningful runs.
            else:
                self.runs.append(run)
            self.active.pop(run["id"], None)
            _current.reset(token)

    def snapshot(self, since=0):
        active = [
            run
            for run in self.active.values()
            if run["label"] != "Match workflow"
            or any(step["name"] == "trigger" and step["output"].get("fired") for step in run["steps"])
        ]
        retained = [*self.runs, *active]
        if self.latest_quiet:
            retained.append(self.latest_quiet)
        return {
            "session": self.id,
            "revision": self.revision,
            "counters": dict(self.counters),
            "retainedIds": [run["id"] for run in retained],
            "runs": [
                {k: v for k, v in run.items() if k != "started"}
                for run in retained
                if run["revision"] > since
            ],
        }


def record(name, inputs, outputs, duration_ms=0, *, status="complete", call_id=None, secrets=()):
    current = _current.get()
    if current is None:
        return
    store, run = current
    previous = next((step for step in run["steps"] if call_id and step["call_id"] == call_id), None)
    if previous is not None:
        previous.update(status=status, duration_ms=round(duration_ms, 2), output=safe(outputs, secrets))
        previous.setdefault("responses", []).append({"phase": status, "output": safe(outputs, secrets)})
    else:
        run["steps"].append(
            {
                "name": name,
                "status": status,
                "call_id": call_id,
                "offset_ms": round((time.perf_counter() - run["started"]) * 1000 - duration_ms, 2),
                "duration_ms": round(duration_ms, 2),
                "input": safe(inputs, secrets),
                "output": safe(outputs, secrets),
            }
        )
    # Defensive bound for repeated provider events within a single run.
    run["steps"] = run["steps"][-40:]
    store.touch(run)
    _export_step(store, run, name, inputs, outputs, duration_ms, status, call_id, secrets)


def _export_step(store, run, name, inputs, outputs, duration_ms, status, call_id, secrets):
    """Model spans remain open through response parsing and possible fallback.

    They end at their last recorded phase, even when the enclosing viewer later
    waits for language. Everything is queued by the SDK, never exported inline.
    """
    compact = compact_tracing()
    root = capture_span.get() if compact else trace.get_current_span()
    if root is None:
        root = trace.get_current_span()
    if not root.is_recording():
        return
    target = store._cloud_evidence.get(run["id"], root) if compact and not call_id else root
    target.add_event(
        name, {"matchos.status": status, "matchos.duration_ms": duration_ms, "matchos.call_id": call_id or ""}
    )
    now = time.time_ns()
    tracer = trace.get_tracer("matchos")
    if call_id:
        key = (run["id"], call_id)
        call = store._cloud_calls.get(key)
        if call is None:
            kind, _, model = name.partition(": ")
            provider = "azure.ai.inference" if model.startswith("azure-foundry:") else model.split(":", 1)[0]
            span = tracer.start_span(
                name,
                context=trace.set_span_in_context(root),
                kind=SpanKind.CLIENT,
                attributes={
                    "matchos.call.id": call_id,
                    "matchos.call.kind": kind,
                    "gen_ai.operation.name": "chat"
                    if kind == "reasoning"
                    else "speech"
                    if kind == "speech"
                    else "matchos.select_story",
                    "gen_ai.provider.name": provider,
                    "gen_ai.request.model": model,
                },
            )
            call = store._cloud_calls[key] = {"span": span, "end": None, "status": status}
            content_attribute(span, "matchos.input", inputs, secrets)
        span = call["span"]
        call.update(end=None if status == "request" else now, status=status)
        span.set_attribute("matchos.status", status)
        span.add_event(status, {"matchos.duration_ms": duration_ms})
        if status != "request":
            content_attribute(span, "matchos.output", outputs, secrets)
        if status in {"error", "fallback"}:
            span.set_status(StatusCode.ERROR, "provider failure" if status == "error" else "local fallback")
        if status == "fallback":
            span.set_attribute("matchos.fallback", True)
            root.set_attribute("matchos.fallback", True)
        if isinstance(outputs, dict):
            usage = outputs.get("usage") or outputs.get("usage_details") or {}
            if not isinstance(usage, dict):
                usage = {}
            for suffix, names in {
                "input_tokens": ("input_tokens", "prompt_tokens", "input_token_count"),
                "output_tokens": ("output_tokens", "completion_tokens", "output_token_count"),
            }.items():
                for field in names:
                    if isinstance(usage.get(field), int):
                        span.set_attribute(f"gen_ai.usage.{suffix}", usage[field])
                        break
    elif not compact:
        span = tracer.start_span(
            name,
            start_time=now - int(max(0, duration_ms) * 1_000_000),
            attributes={"matchos.status": status, "matchos.duration_ms": duration_ms},
        )
        content_attribute(span, "matchos.input", inputs, secrets)
        content_attribute(span, "matchos.output", outputs, secrets)
        span.end(end_time=now)
    if compact:
        _compact_summary(store, run, root, name, inputs, outputs, secrets)
        return
    if name == "viewer context":
        message_attribute(root, "gen_ai.input.messages", inputs, "user")
    elif name == "viewer production":
        payload = safe(outputs, secrets)
        message_attribute(root, "gen_ai.output.messages", payload, "assistant")
        root.set_attribute("matchos.publication", payload.get("producer", {}).get("action", "unknown"))
        selected = (payload.get("selection") or {}).get("selected_candidate_id")
        root.set_attribute("matchos.selected_candidate", selected or "NONE")


def quiet():
    current = _current.get()
    if current:
        if not current[1].get("quiet"):
            current[0].counters["quiet_ticks"] += 1
        current[1]["quiet"] = True


def _compact_summary(store, run, root, name, inputs, outputs, secrets):
    """Export evidence once; keep complete unabridged steps in local telemetry."""
    if name == "state":
        state = outputs.model_dump() if hasattr(outputs, "model_dump") else outputs or {}
        for key in ("score_red", "score_blue", "timestamp_ms"):
            if key in state:
                root.set_attribute(f"matchos.{key}", state[key])
    elif name == "trigger":
        trigger = safe(outputs, secrets)
        root.set_attribute("matchos.trigger.reasons", trigger.get("reasons", []))
    elif name == "verified candidates":
        pool = safe(outputs, secrets)
        candidates = pool.get("candidates", [])
        root.set_attribute("matchos.candidate_count", len(candidates))
        root.set_attribute("matchos.candidate_ids", [item["id"] for item in candidates])
        clock = run["metadata"].get("timestamp_ms", 0) // 1000
        frames = list(
            dict.fromkeys(item.get("frame", "Story").replace("_", " ").title() for item in candidates)
        )
        root.update_name(f"{clock // 60:02}:{clock % 60:02} · {' / '.join(frames) or 'No verified story'}")
        evidence = store._cloud_evidence.pop(run["id"], None)
        if evidence is not None:
            evidence.set_attribute("matchos.candidate_count", len(candidates))
            checks = pool.get("checks", [])
            evidence.set_attribute("matchos.checks_passed", sum(bool(c.get("passed")) for c in checks))
            evidence.set_attribute("matchos.checks_total", len(checks))
            content_attribute(
                evidence,
                "matchos.output",
                {
                    "candidates": [
                        {
                            key: item[key]
                            for key in ("id", "frame", "team_id", "facts", "evidence")
                            if key in item
                        }
                        for item in candidates
                    ],
                    "checks": checks,
                },
                secrets,
            )
            evidence.end()
    elif name == "viewer context":
        viewer = inputs.get("viewer", {})
        pool = inputs.get("candidates")
        pool = pool.model_dump() if hasattr(pool, "model_dump") else pool or {}
        message_attribute(
            root,
            "gen_ai.input.messages",
            {
                "viewer": viewer,
                "timestamp_ms": run["metadata"].get("timestamp_ms"),
                "candidateIds": [item["id"] for item in pool.get("candidates", [])],
            },
            "user",
        )
    elif name == "viewer production":
        payload = safe(outputs, secrets)
        selection, producer = payload.get("selection") or {}, payload.get("producer") or {}
        overlay, moment = payload.get("overlay") or {}, payload.get("moment") or {}
        action = producer.get("action", "unknown")
        root.set_attribute("matchos.publication", action)
        root.set_attribute("matchos.selected_candidate", selection.get("selected_candidate_id") or "NONE")
        role = run["metadata"].get("viewer", {}).get("persona", "Viewer").capitalize()
        frame = moment.get("frame", "No story").replace("_", " ").title()
        root.update_name(f"{role} · {action} · {frame}")
        content_attribute(root, "matchos.reason", producer.get("reason", ""), secrets)
        if overlay.get("headline"):
            content_attribute(root, "matchos.headline", overlay["headline"], secrets)
        message_attribute(
            root,
            "gen_ai.output.messages",
            {
                "selected_candidate_id": selection.get("selected_candidate_id"),
                "producer": producer,
                "overlay": {key: overlay[key] for key in ("headline", "body") if key in overlay},
            },
            "assistant",
        )
