"""Production limits must release capacity and keep diagnostics without content."""
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from matchos.model_logging import ModelCall
from matchos.server import app


def test_session_admission_limit_releases_on_disconnect(monkeypatch):
    monkeypatch.setenv('MATCHOS_MAX_SESSIONS', '1')
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as first:
            assert first.receive_json()['type'] == 'hello'
            assert app.state.active_sessions == 1
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('/ws'):
                    pass
        with client.websocket_connect('/ws') as replacement:
            assert replacement.receive_json()['type'] == 'hello'
            assert app.state.active_sessions == 1
        assert app.state.active_sessions == 0


def test_production_model_logs_keep_metadata_without_prompt_response(monkeypatch, caplog):
    monkeypatch.setenv('MATCHOS_MODEL_LOG_CONTENT', '0')
    with caplog.at_level(logging.INFO, logger='matchos.model_calls'):
        call = ModelCall('decision', 'test-model', {'prompt': 'private prompt'})
        call.result({'text': 'private response'})
        call.result({'error': 'ReadTimeout', 'status': 504, 'body': 'private body'}, phase='ERROR')
    assert 'REQUEST test-model' in caplog.text and 'RESPONSE test-model' in caplog.text
    assert 'ReadTimeout' in caplog.text and '504' in caplog.text
    for value in ('private prompt', 'private response', 'private body'):
        assert value not in caplog.text


def test_build_context_contains_only_runtime_files(tmp_path):
    from scripts.deploy_azure import build_context
    build_context(tmp_path)
    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()}
    assert 'Dockerfile' in files and 'matchos/server.py' in files and 'matchos/web/app.js' in files
    assert not any(part in {'.env', '.env.local', '.git', 'lib', 'bin', '__pycache__'}
                   for name in files for part in name.split('/'))
