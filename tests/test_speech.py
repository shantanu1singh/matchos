"""Speech transport, approved-copy boundary and bounded nonblocking caching."""

import asyncio
import json
from xml.etree import ElementTree

import httpx
import pytest
from fastapi.testclient import TestClient

from matchos.schemas import (
    Check,
    Overlay,
    PipelineResult,
    ProducerDecision,
    VerificationResult,
    VerifiedMoment,
    ViewerProfile,
)
from matchos.speech import SpeechConfig, SpeechService, SpeechUnavailable, prepare_speech
from matchos.telemetry import Telemetry


def aired(persona="broadcaster", language="en", frame="GOAL", team="RED", body="Liverpool lead 2–1."):
    viewer = ViewerProfile(persona=persona, language=language, team_id="RED")
    moment = VerifiedMoment(
        id="goal-1",
        type="GOAL",
        team_id=team,
        frame=frame,
        importance=0.9,
        confidence=0.9,
        facts={"team": "Liverpool", "scoreRed": 2, "scoreBlue": 1},
        evidence=["goal-event"],
        evidence_detail=[],
        valid_from_ms=3903000,
        expires_at_ms=4023000,
        checks=[Check(name="goal_event", passed=True, detail="Actual feed event")],
    )
    overlay = Overlay(
        moment_id=moment.id,
        persona=persona,
        language=language,
        team_id=viewer.team_id,
        kicker="Goal",
        headline="Liverpool take the lead",
        body=body,
        stats=[],
        source="agent",
        provider="test",
    )
    result = PipelineResult(
        kind="moment",
        moment=moment,
        overlay=overlay,
        verification=VerificationResult(verified=True, moment=moment),
        producer=ProducerDecision(action="AIR", reason="New verified goal"),
    )
    payload = {
        "type": "viewer_update",
        "index": 0,
        "profile": viewer.model_dump(),
        "result": result.model_dump(),
        "explanation": {"status": "update"},
    }
    return result, viewer, payload


def configured(**kwargs):
    return SpeechConfig(enabled=True, key="private-speech-key", region="eastus", **kwargs)


def test_delivery_changes_by_persona_event_language_and_supporter_fortunes():
    specs = {
        role: prepare_speech(*aired(role)[:2], configured()) for role in ("broadcaster", "analyst", "fan")
    }
    assert (
        specs["broadcaster"].style == "excited"
        and specs["analyst"].style == "educational"
        and specs["fan"].style == "joyful"
    )
    assert len({spec.cache_key for spec in specs.values()}) == 3
    negative = prepare_speech(*aired("fan", team="BLUE")[:2], configured())
    assert negative.style == "sad"
    city_dismissal = prepare_speech(*aired("fan", frame="RED_CARD", team="BLUE")[:2], configured())
    assert city_dismissal.style == "joyful"
    hindi = prepare_speech(*aired("fan", language="hi", body="लिवरपूल आगे है।")[:2], configured())
    assert hindi.voice == "hi-IN-Priya:MAI-Voice-2.1-Flash" and "लिवरपूल" in hindi.ssml


def test_ssml_escapes_copy_and_never_reads_delivery_guidance_or_repeated_headlines():
    result, viewer, _ = aired(body="Liverpool take the lead. <break/> & a goal!")
    spec = prepare_speech(result, viewer, configured())
    tree = ElementTree.fromstring(spec.ssml)
    spoken = "".join(tree.itertext())
    assert spoken == result.overlay.body
    assert spoken.count("Liverpool take the lead") == 1
    assert spec.delivery not in spoken and "<break/>" in spoken
    assert not list(tree.iter("{http://www.w3.org/2001/10/synthesis}break"))


@pytest.mark.parametrize("change", ["suppressed", "unverified", "wrong_profile", "too_long"])
def test_only_finished_verified_approved_copy_can_be_registered(change):
    result, viewer, payload = aired()
    if change == "suppressed":
        result.producer.action = "SUPPRESS"
    elif change == "unverified":
        result.verification.verified = False
    elif change == "wrong_profile":
        result.overlay.persona = "analyst"
    else:
        result.overlay.body = "x" * 2000
    with pytest.raises(ValueError):
        prepare_speech(result, viewer, configured())
    payload["result"] = result.model_dump()
    assert SpeechService(configured()).register(payload, Telemetry(), 3904000) is None


def test_cache_singleflight_abandoned_waiter_and_telemetry_remain_independent():
    async def run():
        service = SpeechService(configured())
        store = Telemetry()
        payload = aired()[2]
        link = service.register(payload, store, 3904000)
        assert service.register(payload, store, 3904000)["id"] == link["id"]
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(spec):
            calls.append(spec)
            started.set()
            await release.wait()
            return b"mp3-test-bytes"

        service._request = request
        first = asyncio.create_task(service.audio(link["id"]))
        await started.wait()
        second = asyncio.create_task(service.audio(link["id"]))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert store.snapshot()["runs"][0]["status"] == "running"
        assert not second.done() and len(calls) == 1
        release.set()
        assert await second == (b"mp3-test-bytes", False)
        assert await service.audio(link["id"]) == (b"mp3-test-bytes", True)
        assert len(calls) == 1
        snapshot = json.dumps(store.snapshot())
        assert "speech cache" in snapshot and "private-speech-key" not in snapshot
        await service.close()
        assert not service.cache and not service.jobs

    asyncio.run(run())


def test_failed_calls_are_not_cached_and_expiry_and_memory_bounds_are_enforced():
    async def run():
        service = SpeechService(configured())
        store = Telemetry()
        payload = aired()[2]
        link = service.register(payload, store, 3904000)
        calls = []

        async def request(spec):
            calls.append(spec)
            if len(calls) == 1:
                raise ValueError("provider-body-private-speech-key")
            return b"12345"

        service._request = request
        with pytest.raises(SpeechUnavailable) as error:
            await service.audio(link["id"])
        assert "private-speech-key" not in str(error.value)
        assert not service.cache
        service.max_cache_bytes = 3
        await service.audio(link["id"])
        assert not service.cache  # One oversized cache entry cannot break the bound.
        service.ttl_s = -1
        with pytest.raises(SpeechUnavailable) as error:
            await service.audio(link["id"])
        assert error.value.status == 404
        await service.close()

    asyncio.run(run())


def test_identical_audio_in_two_matches_shares_synthesis_without_crossing_trace_stores():
    async def run():
        service = SpeechService(configured())
        first, second = Telemetry(), Telemetry()
        payload = aired()[2]
        left = service.register(payload, first, 3904000)["id"]
        right = service.register(payload, second, 3904000)["id"]
        assert left != right
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def request(spec):
            calls.append(spec)
            started.set()
            await release.wait()
            return b"mp3"

        service._request = request
        one = asyncio.create_task(service.audio(left))
        await started.wait()
        two = asyncio.create_task(service.audio(right))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(one, two)
        assert len(calls) == 1
        assert any(step.get("call_id") for run in first.snapshot()["runs"] for step in run["steps"])
        assert not any(step.get("call_id") for run in second.snapshot()["runs"] for step in run["steps"])
        assert "speech shared synthesis" in json.dumps(second.snapshot())
        assert await service.audio(right) == (b"mp3", True)
        await service.close()

    asyncio.run(run())


def test_speech_rest_contract_and_http_endpoint_do_not_accept_arbitrary_text(monkeypatch):
    from matchos.server import app

    monkeypatch.setenv("MATCHOS_SPEECH_ENABLED", "1")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "private-speech-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    actual = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"mp3-test-bytes", headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(
        "matchos.speech.httpx.AsyncClient",
        lambda **kwargs: actual(transport=httpx.MockTransport(respond), **kwargs),
    )
    with TestClient(app) as client:
        store = Telemetry()
        link = app.state.speech.register(aired()[2], store, 3904000)
        assert client.get("/api/speech/unknown").status_code == 404
        assert client.post("/api/speech", json={"text": "unapproved"}).status_code in {404, 405}
        response = client.get("/api/speech/" + link["id"])
        assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"mp3-test-bytes"
        assert client.get("/api/speech/" + link["id"]).headers["x-matchos-audio-cache"] == "hit"
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://eastus.tts.speech.microsoft.com/cognitiveservices/v1"
        assert request.headers["ocp-apim-subscription-key"] == "private-speech-key"
        assert request.headers["x-microsoft-outputformat"] == "audio-24khz-48kbitrate-mono-mp3"
        assert "MAI-Voice-2.1-Flash" in request.content.decode()


def test_disabled_speech_keeps_app_and_websocket_usable(monkeypatch):
    from matchos.server import app

    monkeypatch.setenv("MATCHOS_SPEECH_ENABLED", "0")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["speech"]["available"] is False
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/speech/unknown").status_code == 503


@pytest.mark.parametrize("region", ["https://attacker.example", "eastus/path", "eastus@evil"])
def test_speech_region_cannot_redirect_credentials_to_another_host(monkeypatch, region):
    monkeypatch.setenv("AZURE_SPEECH_REGION", region)
    with pytest.raises(ValueError):
        SpeechConfig.from_env()
