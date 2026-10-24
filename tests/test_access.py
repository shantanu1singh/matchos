import base64
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from matchos.access import AccessConfig, COOKIE, MAX_AGE
from matchos.server import app

PASSWORD = 'test-password-with-enough-entropy'


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_ENABLED', '1')
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_USERNAME', 'judges')
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_PASSWORD', PASSWORD)
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_SECURE_COOKIE', '1')
    with TestClient(app, base_url='https://testserver') as client:
        yield client


def test_gate_covers_page_assets_audio_docs_and_keeps_health_public(protected):
    for path in ('/', '/assets/app.js', '/api/speech/example', '/docs', '/openapi.json'):
        response = protected.get(path)
        assert response.status_code == 401
        assert response.headers['www-authenticate'].startswith('Basic realm="MatchOS"')
    assert protected.get('/api/health').status_code == 200
    assert protected.get('/', auth=('judges', 'wrong')).status_code == 401
    assert protected.get('/', headers={'Authorization': 'Basic !@#$'}).status_code == 401
    assert protected.get('/', headers={'Authorization': 'Bearer token'}).status_code == 401
    assert protected.get('/', headers={'Cookie': f'{COOKIE}=forged'}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with protected.websocket_connect('wss://testserver/ws'):
            pass
    assert app.state.active_sessions == 0


def test_browser_login_cookie_authenticates_websocket_and_audio(protected):
    response = protected.get('/', auth=('judges', PASSWORD))
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=Strict' in cookie
    assert PASSWORD not in cookie and 'private' in response.headers['cache-control']
    assert protected.get('/assets/app.js').status_code == 200
    # Authenticated audio reaches the audio handler rather than the gate.
    assert protected.get('/api/speech/missing').status_code != 401
    with protected.websocket_connect('wss://testserver/ws', headers={'Origin': 'https://testserver'}) as socket:
        assert socket.receive_json()['type'] == 'hello'
    with pytest.raises(WebSocketDisconnect):
        with protected.websocket_connect('wss://testserver/ws', headers={'Origin': 'https://evil.example'}):
            pass


def test_expired_future_tampered_and_rotated_cookies_are_rejected():
    config = AccessConfig(True, 'judges', PASSWORD)
    stamp = str(int(time.time()))
    assert config.cookie_valid(f'{COOKIE}={config.ticket()}')
    for timestamp in (str(int(time.time()) - MAX_AGE - 1), str(int(time.time()) + 60)):
        assert not config.cookie_valid(f'{COOKIE}={timestamp}.{config.signature(timestamp)}')
    assert not config.cookie_valid(f'{COOKIE}={stamp}.forged')
    assert not AccessConfig(True, 'judges', 'another-long-password').cookie_valid(f'{COOKIE}={config.ticket()}')
    assert not AccessConfig(True, 'other-user', PASSWORD).cookie_valid(f'{COOKIE}={config.ticket()}')


def test_valid_basic_credentials_can_authenticate_nonbrowser_socket(protected):
    credentials = base64.b64encode(f'judges:{PASSWORD}'.encode()).decode()
    with protected.websocket_connect('wss://testserver/ws', headers={'Authorization': f'Basic {credentials}'}) as socket:
        assert socket.receive_json()['type'] == 'hello'


@pytest.mark.parametrize('password', ['', 'short'])
def test_enabled_gate_fails_startup_on_missing_or_weak_credentials(monkeypatch, password):
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_ENABLED', '1')
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_USERNAME', 'judges')
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_PASSWORD', password)
    with pytest.raises(ValueError, match='at least 16'):
        AccessConfig.from_env()


def test_disabled_gate_preserves_local_development(monkeypatch):
    monkeypatch.setenv('MATCHOS_BASIC_AUTH_ENABLED', '0')
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        with client.websocket_connect('/ws') as socket:
            assert socket.receive_json()['type'] == 'hello'
