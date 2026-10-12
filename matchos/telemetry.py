"""Bounded, session-local execution traces; capture never awaits network I/O."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid

_current = ContextVar("matchos_trace", default=None)


def safe(value, secrets=()):
    from matchos.model_logging import _safe_json
    import json
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

    def touch(self, run):
        self.revision += 1
        run["revision"] = self.revision

    @contextmanager
    def capture(self, label, metadata):
        from opentelemetry import trace
        with trace.get_tracer("matchos").start_as_current_span(label):
            with self._capture(label, metadata) as run:
                yield run

    @contextmanager
    def _capture(self, label, metadata):
        run = {"id": uuid.uuid4().hex[:12], "label": label, "metadata": safe(metadata),
               "status": "running", "steps": [], "started": time.perf_counter()}
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
        active = [run for run in self.active.values() if run["label"] != "Match workflow"
                  or any(step["name"] == "trigger" and step["output"].get("fired") for step in run["steps"])]
        retained = [*self.runs, *active]
        if self.latest_quiet:
            retained.append(self.latest_quiet)
        return {"session": self.id, "revision": self.revision,
                "retainedIds": [run["id"] for run in retained],
                "runs": [{k: v for k, v in run.items() if k != "started"}
                         for run in retained if run["revision"] > since]}


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
        run["steps"].append({"name": name, "status": status, "call_id": call_id,
                             "offset_ms": round((time.perf_counter() - run["started"]) * 1000 - duration_ms, 2),
                             "duration_ms": round(duration_ms, 2),
                             "input": safe(inputs, secrets), "output": safe(outputs, secrets)})
    # Defensive bound for repeated provider events within a single run.
    run["steps"] = run["steps"][-40:]
    store.touch(run)
    from opentelemetry import trace
    trace.get_current_span().add_event(name, {"matchos.status": status, "matchos.duration_ms": duration_ms, "matchos.call_id": call_id or ""})


def quiet():
    current = _current.get()
    if current:
        current[1]["quiet"] = True
