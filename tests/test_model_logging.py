"""Terminal diagnostics must correlate calls without leaking credentials."""

import json
import logging

from matchos.model_logging import ModelCall, _safe_json


def test_model_call_logs_request_response_and_redacts_secrets(monkeypatch, caplog):
    monkeypatch.setenv("EXAMPLE_API_KEY", "native-secret")
    with caplog.at_level(logging.INFO, logger="matchos.model_calls"):
        call = ModelCall(
            "reasoning",
            "vertex_ai/example",
            {"viewer": "analyst", "api_key": "secret-field"},
            secrets=("configured-secret",),
        )
        call.result(
            {"text": "configured-secret native-secret", "headers": {"Authorization": "Bearer ignored-secret"}}
        )
    assert "REQUEST vertex_ai/example" in caplog.text
    assert "RESPONSE vertex_ai/example" in caplog.text
    assert caplog.text.count(f"[{call.id}]") == 2
    assert '"viewer": "analyst"' in caplog.text
    assert "[REDACTED]" in caplog.text
    for secret in ("native-secret", "configured-secret", "secret-field", "ignored-secret"):
        assert secret not in caplog.text


def test_redaction_handles_json_escaped_tokens():
    token = 'key"with\\escapes'
    payload = json.loads(_safe_json({"text": token}, (token,)))
    assert payload["text"] == "[REDACTED]"


def test_model_logs_use_stdout_and_recover_disabled_logger(capsys):
    from matchos.model_logging import logger

    logger.disabled = True
    logger.setLevel(logging.ERROR)
    call = ModelCall("decision", "test-model", {"minute": 36})
    call.result({"primaryFrame": "RED_CARD"})
    output = capsys.readouterr()
    assert "REQUEST test-model" in output.out
    assert "RESPONSE test-model" in output.out
    assert "MODEL" not in output.err


def test_server_startup_confirms_model_logging_in_stdout(capsys):
    import sys

    from fastapi.testclient import TestClient

    from matchos.server import app

    with TestClient(app):
        output = capsys.readouterr().out
        assert "MODEL Logging enabled" in output
        assert "stdout" in output
        assert sys.executable in output
