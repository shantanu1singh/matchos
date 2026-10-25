"""Compact cloud topology and payload boundaries, using real SDK processors."""

import asyncio
import json

import pytest
from runtime_helpers import viewer_dependencies

from matchos import observability
from matchos.model_logging import ModelCall
from matchos.telemetry import Telemetry, quiet, record


@pytest.fixture
def compact_spans(monkeypatch):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("MATCHOS_TRACE_MODE", "compact")
    monkeypatch.setenv("MATCHOS_TRACE_CONTENT", "1")
    provider = sdk.TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(observability.compact_processor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    yield provider, exporter
    provider.shutdown()


def test_quiet_ticks_and_no_candidate_reviews_stay_local(compact_spans):
    provider, exporter = compact_spans
    store = Telemetry()
    for _ in range(5):
        with store.capture("Match workflow", {"timestamp_ms": 3000000}):
            with provider.get_tracer("agent_framework").start_as_current_span("executor.state"):
                record("state", {"raw_tracking": [1, 2, 3]}, {"score_red": 1})
                record("trigger", {}, {"fired": False, "reasons": []})
                quiet()
    with store.capture("Viewer editorial selection", {"viewer": {"persona": "analyst"}}):
        record("viewer context", {"candidates": {"candidates": []}}, None)
        record("viewer production", {}, {"selection": None, "producer": {"action": "REJECT"}})
    provider.force_flush()
    assert not exporter.get_finished_spans()
    snapshot = store.snapshot()
    assert snapshot["counters"] == {"quiet_ticks": 5, "skipped_viewer_reviews": 1}
    quiet_run = next(run for run in snapshot["runs"] if run.get("quiet"))
    assert quiet_run["steps"][0]["input"]["raw_tracking"] == [1, 2, 3]
    assert not store._cloud_evidence


def test_story_trace_is_small_connected_and_keeps_all_persona_outcomes(compact_spans):
    provider, exporter = compact_spans
    store = Telemetry()
    marker = "raw-tracking-only-in-local-inspector"
    state = {"score_red": 1, "score_blue": 1, "timestamp_ms": 3840000, "players": {"raw": marker}}
    pool = {
        "candidates": [
            {
                "id": "press-1",
                "frame": "PRESS_BREAKDOWN",
                "team_id": "RED",
                "facts": {"bypassPercent": "88%"},
                "evidence": ["pass-1"],
                "evidence_detail": [{"raw": marker}],
            }
        ],
        "checks": [{"name": "supported_bypass", "passed": True, "detail": "8/9 passes bypassed"}],
    }
    with store.capture("Match workflow", {"timestamp_ms": 3840000}) as workflow:
        with provider.get_tracer("agent_framework").start_as_current_span("executor.verifier"):
            record("state", {"frame": marker}, state)
            record("trigger", {}, {"fired": True, "reasons": ["press_bypass"]})
            record("verified candidates", {"state": state}, pool)
            parent = observability.trace_carrier()
        assert parent["traceparent"].split("-")[2] == workflow["span_id"]

    async def viewer(role):
        with store.capture("Viewer editorial selection", {"viewer": {"persona": role}}, parent=parent):
            with provider.get_tracer("agent_framework").start_as_current_span("SDK plumbing"):
                record(
                    "viewer context", {"viewer": {"persona": role}, "state": state, "candidates": pool}, None
                )
                call = ModelCall(
                    "editorial selection", "azure-foundry:Decision-1", {"candidateIds": ["press-1"]}
                )
                await asyncio.sleep(0)
                selected = None if role == "analyst" else "press-1"
                call.result({"selected_candidate_id": selected})
                record("editorial selection", {}, {"selected_candidate_id": selected})
                if selected:
                    language = ModelCall(
                        "reasoning", "azure-foundry:gpt-6-luna", {"facts": {"bypassPercent": "88%"}}
                    )
                    language.result({"text": "Liverpool play through the press."})
                record(
                    "viewer production",
                    {},
                    {
                        "selection": {"selected_candidate_id": selected},
                        "producer": {
                            "action": "AIR" if selected else "SUPPRESS",
                            "reason": "Worth showing" if selected else "No interruption",
                        },
                        "moment": {"frame": "PRESS_BREAKDOWN", "evidence_detail": marker}
                        if selected
                        else None,
                        "overlay": {"headline": "Liverpool play through", "body": "The press opens up."}
                        if selected
                        else None,
                    },
                )

    async def run():
        await asyncio.gather(*(viewer(role) for role in ("broadcaster", "analyst", "fan")))

    asyncio.run(run())
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 10  # workflow + evidence + 3 personas + 5 model calls
    assert len({span.context.trace_id for span in spans}) == 1
    ids = {span.context.span_id for span in spans}
    assert all(span.parent is None or span.parent.span_id in ids for span in spans)
    assert all(span.instrumentation_scope.name == "matchos" for span in spans)
    evidence = next(span for span in spans if span.name == "Evidence")
    assert {event.name for event in evidence.events} >= {"state", "trigger", "verified candidates"}
    assert "88%" in evidence.attributes["matchos.output"]
    root = next(span for span in spans if span.attributes.get("gen_ai.operation.name") == "invoke_workflow")
    assert root.name == "64:00 · Press Breakdown" and root.attributes["matchos.candidate_count"] == 1
    personas = [span for span in spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent"]
    assert {span.attributes["gen_ai.agent.id"] for span in personas} == {
        "matchos-broadcaster-v1",
        "matchos-analyst-v1",
        "matchos-fan-v1",
    }
    assert {span.attributes["matchos.publication"] for span in personas} == {"AIR", "SUPPRESS"}
    cloud = json.dumps([dict(span.attributes) for span in spans])
    assert marker not in cloud
    assert marker in json.dumps(store.snapshot())
    assert not store._cloud_calls and not store._cloud_evidence


@pytest.mark.parametrize("exception", [ValueError, asyncio.CancelledError])
def test_errors_and_cancellations_are_retained_with_model_phases(compact_spans, monkeypatch, exception):
    provider, exporter = compact_spans
    monkeypatch.setenv("MATCHOS_TRACE_CONTENT", "0")
    store = Telemetry()
    with pytest.raises(exception):
        with store.capture("Viewer editorial selection", {"viewer": {"persona": "fan"}}):
            record("viewer context", {"prompt": "private-content"}, None)
            call = ModelCall("reasoning", "test-model", {"prompt": "private-content"})
            call.result({"error": "failure"}, phase="ERROR")
            call.result({"text": "local copy"}, phase="FALLBACK")
            raise exception()
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert not any(span.attributes.get("matchos.quiet") for span in spans)
    model = next(span for span in spans if span.name.startswith("reasoning:"))
    assert {event.name for event in model.events} >= {"request", "error", "fallback"}
    assert "private-content" not in json.dumps([dict(span.attributes) for span in spans])
    assert not store._cloud_calls


def test_compact_setup_filters_both_azure_and_otlp_and_disables_sdk_content(monkeypatch):
    import agent_framework.observability as framework
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("MATCHOS_OTEL_ENABLED", "1")
    monkeypatch.delenv("MATCHOS_TRACE_MODE", raising=False)  # compact is the default
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "not-a-real-connection")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    azure, collector = InMemorySpanExporter(), InMemorySpanExporter()
    monkeypatch.setattr(AzureMonitorTraceExporter, "from_connection_string", lambda *a, **kw: azure)
    monkeypatch.setattr(otlp, "OTLPSpanExporter", lambda: collector)
    providers, instrumentation = [], []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    monkeypatch.setattr(framework, "enable_instrumentation", lambda **kw: instrumentation.append(kw))
    assert observability.configure_observability()
    provider = providers[0]
    try:
        monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
        with Telemetry().capture("A diagnostic", {}):
            with provider.get_tracer("agent_framework").start_as_current_span("SDK plumbing"):
                pass
        provider.force_flush()
        assert len(azure.get_finished_spans()) == len(collector.get_finished_spans()) == 1
        assert instrumentation == [{"enable_sensitive_data": False}]
    finally:
        provider.shutdown()


def test_compact_without_destination_does_not_claim_export_is_enabled(monkeypatch):
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("MATCHOS_OTEL_ENABLED", "1")
    monkeypatch.setenv("MATCHOS_TRACE_MODE", "compact")
    for name in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert observability.configure_observability() is False


def test_real_checkpoint_and_three_workers_keep_one_trace_without_sdk_orphans(compact_spans, monkeypatch):
    import agent_framework.observability as framework

    from matchos.demo import DEMO_STOPS, demo_viewers, review_checkpoint
    from matchos.perspective import Perspective
    from matchos.session import MatchSession

    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_PROVIDER", "local")
    monkeypatch.setattr(framework.OBSERVABILITY_SETTINGS, "enable_instrumentation", True)
    provider, exporter = compact_spans
    results = []

    async def run():
        session = MatchSession("press_collapse", 47129, 1, demo_viewers()[0], running=False, demo_step=1)
        workers = []

        async def send(payload):
            results.append(payload)

        try:
            await session.seek_to(DEMO_STOPS[1]["timestamp_ms"])
            batch = await review_checkpoint(session)
            for index, viewer in enumerate(demo_viewers()):
                worker = Perspective(viewer_dependencies(session), viewer, index, send)
                workers.append(worker)
                worker.offer(batch)
            await asyncio.gather(*(worker.task for worker in workers))
            assert len(results) == 3
            assert all(result["result"]["producer"]["action"] == "AIR" for result in results)
            assert len(session.telemetry.snapshot()["runs"]) == 4
        finally:
            for worker in workers:
                await worker.close()
            await session.close()

    asyncio.run(run())
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len({span.context.trace_id for span in spans}) == 1
    ids = {span.context.span_id for span in spans}
    assert all(span.parent is None or span.parent.span_id in ids for span in spans)
    assert len([span for span in spans if span.name == "Evidence"]) == 1
    assert (
        len([span for span in spans if span.attributes.get("gen_ai.operation.name") == "invoke_agent"]) == 3
    )
    assert not any(
        span.name in {"state", "trigger", "verified candidates", "viewer context", "viewer production"}
        for span in spans
    )
