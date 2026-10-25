"""Exercise cloud spans without Azure credentials, exporters or network access."""

import asyncio
import json

import pytest

from matchos import observability
from matchos.model_logging import ModelCall
from matchos.telemetry import Telemetry, record


@pytest.fixture(autouse=True)
def full_mode_for_legacy_contracts(monkeypatch):
    monkeypatch.setenv("MATCHOS_TRACE_MODE", "full")


@pytest.fixture
def spans(monkeypatch):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = sdk.TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


def test_evidence_and_concurrent_viewers_share_trace_without_mixing_calls(spans, monkeypatch):
    monkeypatch.setenv("MATCHOS_TRACE_CONTENT", "1")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "credential-never-export")
    store = Telemetry()
    with store.capture("Match workflow", {"timestamp_ms": 3840000}):
        record("verified candidates", {"api_key": "hidden"}, {"candidateIds": ["press-1"]}, 2)
        parent = observability.trace_carrier()

    async def viewer(role):
        with store.capture("Viewer editorial selection", {"viewer": {"persona": role}}, parent=parent):
            record("viewer context", {"prompt": role, "text": "credential-never-export"}, None)
            call = ModelCall("editorial selection", "azure-foundry:Decision-1", {"role": role})
            await asyncio.sleep(0.001)
            call.result({"selected": "press-1"})
            record(
                "viewer production",
                {},
                {
                    "selection": {"selected_candidate_id": "press-1"},
                    "producer": {"action": "AIR"},
                    "overlay": {"body": role},
                },
            )

    async def run():
        await asyncio.gather(viewer("fan"), viewer("analyst"))

    asyncio.run(run())
    finished = spans.get_finished_spans()
    assert len({span.context.trace_id for span in finished}) == 1
    roots = [span for span in finished if span.name == "Viewer editorial selection"]
    assert {span.attributes["gen_ai.agent.id"] for span in roots} == {"matchos-fan-v1", "matchos-analyst-v1"}
    for span in roots:
        role = span.attributes["matchos.persona"]
        message = json.loads(span.attributes["gen_ai.output.messages"])[0]
        assert json.loads(message["parts"][0]["content"])["overlay"]["body"] == role
        call = next(
            item
            for item in finished
            if item.parent
            and item.parent.span_id == span.context.span_id
            and item.name.startswith("editorial selection:")
        )
        assert json.loads(call.attributes["matchos.input"])["role"] == role
    exported = json.dumps([dict(span.attributes) for span in finished])
    assert "credential-never-export" not in exported and '"hidden"' not in exported
    assert all(run.get("trace_id") and run.get("span_id") for run in store.snapshot()["runs"])


def test_model_span_includes_fallback_phases_usage_and_precise_end_time(spans):
    store = Telemetry()
    with store.capture("Viewer editorial selection", {"viewer": {"persona": "broadcaster"}}):
        call = ModelCall("reasoning", "azure-foundry:language", {"prompt": "test"})
        call.result({"usage_details": {"input_token_count": 23, "output_token_count": 7}})
        call.result({"error": "invalid response"}, phase="ERROR")
        call.result({"text": "local copy"}, phase="FALLBACK")
        import time

        time.sleep(0.01)
        record("viewer production", {}, {"producer": {"action": "AIR"}, "selection": None})
    finished = spans.get_finished_spans()
    model = next(span for span in finished if span.name.startswith("reasoning:"))
    root = next(span for span in finished if span.name == "Viewer editorial selection")
    assert root.attributes["gen_ai.agent.id"] == "matchos-broadcaster-v1"
    assert model.attributes["matchos.fallback"] is True
    assert model.attributes["gen_ai.usage.input_tokens"] == 23
    assert model.attributes["gen_ai.usage.output_tokens"] == 7
    assert model.status.status_code.name == "ERROR"
    assert {event.name for event in model.events} >= {"request", "response", "error", "fallback"}
    assert model.end_time < root.end_time
    assert store._cloud_calls == {}


def test_content_is_opt_in_and_cancellation_closes_pending_calls(spans, monkeypatch):
    monkeypatch.delenv("MATCHOS_TRACE_CONTENT", raising=False)
    store = Telemetry()
    with pytest.raises(asyncio.CancelledError):
        with store.capture("Viewer editorial selection", {"viewer": {"persona": "fan"}}):
            record("viewer context", {"prompt": "private-input"}, None)
            ModelCall("reasoning", "test-model", {"prompt": "private-input"})
            raise asyncio.CancelledError()
    finished = spans.get_finished_spans()
    assert all(
        "matchos.input" not in span.attributes and "gen_ai.input.messages" not in span.attributes
        for span in finished
    )
    assert "private-input" not in json.dumps([dict(span.attributes) for span in finished])
    assert (
        next(span for span in finished if span.name == "reasoning: test-model").attributes["matchos.status"]
        == "cancelled"
    )
    assert store._cloud_calls == {}


def test_export_configuration_is_opt_in_idempotent_and_does_not_enable_sdk_content(monkeypatch):
    import agent_framework.observability as framework

    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.delenv("MATCHOS_OTEL_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(framework, "configure_otel_providers", lambda **kwargs: calls.append(kwargs))
    assert observability.configure_observability() is False
    assert not calls
    monkeypatch.setenv("MATCHOS_OTEL_ENABLED", "1")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    observability.configure_observability()
    observability.configure_observability()
    assert calls == [{"service_name": "matchos", "enable_sensitive_data": False}]


def test_azure_exporter_is_added_and_configuration_errors_do_not_leak_secrets(monkeypatch, caplog):
    pytest.importorskip("azure.monitor.opentelemetry.exporter")
    import agent_framework.observability as framework
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("MATCHOS_OTEL_ENABLED", "1")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "secret-connection-string")
    exporter = object()
    received = []
    monkeypatch.setattr(AzureMonitorTraceExporter, "from_connection_string", lambda *args, **kwargs: exporter)
    monkeypatch.setattr(framework, "configure_otel_providers", lambda **kwargs: received.append(kwargs))
    assert observability.configure_observability()
    assert received[0]["exporters"] == [exporter]
    assert received[0]["enable_sensitive_data"] is False
    monkeypatch.setattr(observability, "_configured", False)

    def fail(*args, **kwargs):
        raise ValueError("secret-connection-string")

    monkeypatch.setattr(AzureMonitorTraceExporter, "from_connection_string", fail)
    assert not observability.configure_observability()
    assert "secret-connection-string" not in caplog.text


def test_nested_models_and_connection_strings_are_redacted(monkeypatch):
    from matchos.schemas import ViewerProfile
    from matchos.telemetry import safe

    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "hidden-connection")
    result = safe(
        {
            "viewer": ViewerProfile(persona="broadcaster"),
            "text": "hidden-connection",
            "connection_string": "also-secret",
        }
    )
    assert result["viewer"]["persona"] == "broadcaster"
    assert result["text"] == "[REDACTED]" and result["connection_string"] == "[REDACTED]"
