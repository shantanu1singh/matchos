"""Readable, correlated model diagnostics without transport credentials."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

logger = logging.getLogger("matchos.model_calls")


def configure_model_logging() -> None:
    """Bind diagnostics to the current terminal, even after SDK logging setup."""
    logger.disabled = False
    logger.setLevel(logging.INFO)
    handler = next((item for item in logger.handlers if item.name == "matchos-model-terminal"), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("matchos-model-terminal")
        handler.setFormatter(logging.Formatter("%(asctime)s MODEL %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
    # Rebind when a host, SDK or test changes the active stdout stream.
    handler.stream = sys.stdout
    handler.setLevel(logging.INFO)


configure_model_logging()

_SENSITIVE = {"authorization", "api-key", "api_key", "apikey", "access_token", "refresh_token", "credentials",
              "password", "token", "secret", "client_secret", "private_key", "connection_string"}


def _safe_json(payload: object, secrets: tuple[str, ...] = ()) -> str:
    def clean(value):
        if hasattr(value, "model_dump"):
            return clean(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE else clean(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value

    text = json.dumps(clean(payload), ensure_ascii=False, default=str)
    native_secrets = tuple(value for key, value in os.environ.items()
                           if key.endswith(("API_KEY", "API_TOKEN", "AUTH_TOKEN", "SECRET_ACCESS_KEY",
                                            "ACCESS_KEY_ID", "SESSION_TOKEN", "CLIENT_SECRET", "CONNECTION_STRING", "SPEECH_KEY", "PASSWORD")) and value)
    for secret in sorted(set(secrets + native_secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            text = text.replace(escaped, "[REDACTED]")
    return text


class ModelCall:
    def __init__(self, kind: str, model: str, payload: object, *, secrets: tuple[str, ...] = ()):
        configure_model_logging()
        self.id = uuid.uuid4().hex[:8]
        self.started = time.perf_counter()
        self.secrets = secrets
        self.model = model
        from matchos.telemetry import record
        record(f"{kind}: {model}", payload, None, status="request", call_id=self.id, secrets=secrets)
        detail = _safe_json(payload, secrets) if os.getenv("MATCHOS_MODEL_LOG_CONTENT", "1") == "1" else "[content omitted]"
        logger.info("[%s] %s REQUEST %s %s", self.id, kind, model, detail)

    def result(self, payload: object, *, phase: str = "RESPONSE") -> None:
        configure_model_logging()
        from matchos.telemetry import record
        record(self.model, None, payload, (time.perf_counter() - self.started) * 1000,
               status=phase.lower(), call_id=self.id, secrets=self.secrets)
        # Retain safe error categories/status for operations without logging prompts.
        summary = ({key: payload[key] for key in ("error", "status") if key in payload}
                   if isinstance(payload, dict) and phase == "ERROR" else {})
        detail = (_safe_json(payload, self.secrets) if os.getenv("MATCHOS_MODEL_LOG_CONTENT", "1") == "1"
                  else _safe_json(summary, self.secrets) if summary else "[content omitted]")
        logger.info("[%s] %s %s %.0fms %s", self.id, phase, self.model,
                    (time.perf_counter() - self.started) * 1000, detail)
