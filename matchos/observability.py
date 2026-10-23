"""Opt-in Foundry/Application Insights export; no network work on the frame loop."""
from __future__ import annotations

import json
import logging
import os

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)
_configured = False


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def configure_observability() -> bool:
    """Called once, after dotenv loading. Native SDK content logging stays off.

    MatchOS exports its own redacted payloads only with MATCHOS_TRACE_CONTENT=1.
    The Azure exporter uses the framework's background BatchSpanProcessor.
    """
    global _configured
    if _configured or not enabled("MATCHOS_OTEL_ENABLED"):
        return _configured
    try:
        from agent_framework.observability import configure_otel_providers
        exporters = []
        connection = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
        if connection:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            exporters.append(AzureMonitorTraceExporter.from_connection_string(
                connection, disable_offline_storage=True))
        kwargs = {"service_name": "matchos", "enable_sensitive_data": False}
        if exporters:
            kwargs.update(exporters=exporters, enable_console_exporters=False)
        configure_otel_providers(**kwargs)
    except (ImportError, ValueError) as exc:
        # Do not log exception text: connection strings can be included in it.
        logger.warning("Cloud tracing unavailable (%s). Install requirements-observability.txt and check tracing settings; local telemetry remains available.", type(exc).__name__)
        return False
    _configured = True
    logger.info("MatchOS tracing enabled: %s; redacted content=%s",
                "Application Insights / Foundry" if connection else "configured OpenTelemetry exporters",
                enabled("MATCHOS_TRACE_CONTENT"))
    return True


def flush_observability() -> None:
    if _configured:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)


def trace_carrier() -> dict[str, str]:
    carrier = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier


def parent_context(carrier):
    return TraceContextTextMapPropagator().extract(carrier) if carrier else None


def content_attribute(span, name: str, payload, secrets=()) -> None:
    if not span.is_recording() or not enabled("MATCHOS_TRACE_CONTENT"):
        return
    from matchos.model_logging import _safe_json
    # Bound each cloud field; the local telemetry export retains the full data.
    text = _safe_json(payload, secrets)
    span.set_attribute(name, text[:12000] + ("… [truncated]" if len(text) > 12000 else ""))


def message_attribute(span, name: str, payload, role: str) -> None:
    if not span.is_recording() or not enabled("MATCHOS_TRACE_CONTENT"):
        return
    from matchos.model_logging import _safe_json
    text = _safe_json(payload)
    # Keep the message envelope valid JSON even when its text needs truncation.
    span.set_attribute(name, json.dumps([{"role": role, "parts": [
        {"type": "text", "content": text[:12000] + ("… [truncated]" if len(text) > 12000 else "")}
    ]}], ensure_ascii=False))
