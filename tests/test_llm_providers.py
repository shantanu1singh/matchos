"""Exercise actual provider adapters with mocked network boundaries."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from matchos.decision_client import GatewayDecisionClient, LocalDecisionClient, build_decision_client
from matchos.language_agent import INSTRUCTIONS, build_language_executor, personalize, prompt_payload, render_from_messages
from matchos.llm_config import LLMConfig
from matchos.schemas import PlayerRollup, VerifiedMoment, ViewerProfile
from matchos.state_engine import StateEngine
from agent_framework import Message


def _answers(frame="PLAYER_IMPACT"):
    return {"answers": [
        {"name": name, "type": "predicate", "probability": probability}
        for name, probability in [("tacticalShift", .2), ("momentumShift", .3), ("playerStory", .9), ("shouldSurface", .85)]
    ] + [
        {"name": "importance", "type": "score", "score": 4.2},
        {"name": "primaryFrame", "type": "choice", "choice": frame, "confidence": .9,
         "probabilities": [{"value": frame, "probability": .9}, {"value": "GOAL", "probability": .1}]},
        {"name": "team", "type": "choice", "choice": "RED"},
    ]}


@pytest.mark.parametrize("model", ["convaiinnovations/laya", "convaiinnovations/laya-free"])
def test_gateway_decision_contract_and_focused_player(monkeypatch, model):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=_answers())

    actual_client = httpx.AsyncClient
    monkeypatch.setattr("matchos.decision_client.httpx.AsyncClient", lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw))
    state = StateEngine().state
    state.players["blue_9"] = PlayerRollup(number=9, team_id="BLUE")
    config = LLMConfig(decision_provider="vercel", gateway_api_key="test-key", decision_model=model)
    client = build_decision_client(config)
    decision = asyncio.run(client.decide(state, ["player_involvement"], "blue_9"))
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://ai-gateway.vercel.sh/v1/decisions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert payload["model"] == model
    assert json.loads(payload["input"])["focusPlayerId"] == "blue_9"
    assert len(payload["questions"]) == 7
    assert decision.provider == f"vercel:{model}"
    assert decision.player_id == "blue_9" and decision.team_id == "BLUE"
    assert decision.importance == 4.2 and decision.frame_scores["GOAL"] == .1


@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "refusal", "missing", "nan", "out_of_range", "unknown_frame", "zero"])
def test_gateway_handles_failed_or_invalid_answers(monkeypatch, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "unauthorized":
            return httpx.Response(401, json={"error": "unauthorized"})
        body = _answers()
        if failure == "refusal":
            body["answers"][0] = {"name": "tacticalShift", "type": "refusal"}
        elif failure == "missing":
            body["answers"].pop()
        elif failure in ("nan", "out_of_range", "zero"):
            body["answers"][0]["probability"] = {"nan": "NaN", "out_of_range": 1.5, "zero": 0}[failure]
        elif failure == "unknown_frame":
            body["answers"][5]["choice"] = "FAKE"
        return httpx.Response(200, json=body)

    actual_client = httpx.AsyncClient
    monkeypatch.setattr("matchos.decision_client.httpx.AsyncClient", lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw))
    decision = asyncio.run(GatewayDecisionClient(LLMConfig(gateway_api_key="test-key")).decide(StateEngine().state, ["goal"]))
    if failure == "zero":
        assert decision.tactical_shift == 0
        assert decision.provider == "vercel:convaiinnovations/laya-free"
    else:
        assert decision.provider.startswith("local-decision-fallback:")
        assert decision.primary_frame == "GOAL"


def _moment():
    return VerifiedMoment(
        id="test-moment", type="TACTICAL_SHIFT", team_id="BLUE", frame="PRESS_BREAKDOWN",
        importance=.9, confidence=.94, facts={"defendingTeam": "Blue", "attackingTeam": "Red",
        "pressBypassBefore": .29, "pressBypassNow": .71, "pressBypassSample": 8,
        "defensiveLineBeforeM": 45, "defensiveLineNowM": 38.2, "defensiveLineDeltaM": -6.8,
        "opponentFinalThirdEntries": 7, "finalThirdEntriesBefore": 2},
        evidence=[], evidence_detail=[], valid_from_ms=0, expires_at_ms=40000,
    )


@pytest.mark.parametrize("reply", ["Red are playing through Blue's press.", "They made 999 passes.", "error", "empty", "timeout"])
def test_reasoning_through_agent_executor_and_fact_guard(monkeypatch, reply):
    import litellm

    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if reply == "error":
            raise RuntimeError("provider failure")
        if reply == "timeout":
            await asyncio.sleep(1)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="" if reply == "empty" else reply))])

    monkeypatch.setattr(litellm, "acompletion", complete)
    config = LLMConfig(reasoning_provider="litellm", vertex_project="test-project", reasoning_timeout_s=.02)
    executor, agent, provider = build_language_executor(config)
    moment, viewer = _moment(), ViewerProfile()
    text = asyncio.run(personalize(agent, moment, viewer))
    assert executor.id == "language_agent"
    assert provider == "litellm:vertex_ai/gemini-3.8-flash"
    call = calls[0]
    assert call["model"] == "vertex_ai/gemini-3.8-flash"
    assert call["vertex_project"] == "test-project" and call["vertex_location"] == "global"
    assert call["num_retries"] == 0
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][0]["content"] == INSTRUCTIONS
    assert json.loads(call["messages"][-1]["content"])["moment"]["id"] == moment.id
    if reply == "Red are playing through Blue's press.":
        assert text == reply
    else:
        assert text == render_from_messages([Message("user", [prompt_payload(moment, viewer)])])


def test_reasoning_stream_with_replacement_provider(monkeypatch):
    import litellm

    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Blue's press has dropped."))])

    monkeypatch.setattr(litellm, "acompletion", complete)
    config = LLMConfig(reasoning_provider="litellm", reasoning_model="openai/example", reasoning_api_key="test-key", reasoning_base_url="https://example.test/v1")
    _, agent, _ = build_language_executor(config)

    async def run():
        stream = agent.run(prompt_payload(_moment(), ViewerProfile()), stream=True)
        updates = [update async for update in stream]
        final = await stream.get_final_response()
        return updates, final

    updates, final = asyncio.run(run())
    assert updates and final.text == "Blue's press has dropped."
    assert calls[0]["model"] == "openai/example"
    assert calls[0]["api_key"] == "test-key"
    assert calls[0]["api_base"] == "https://example.test/v1"
    assert "vertex_project" not in calls[0]


def test_missing_litellm_preserves_streaming_response(monkeypatch, caplog):
    original_import = importlib.import_module

    def without_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise ModuleNotFoundError("No module named 'litellm'", name="litellm")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", without_litellm)
    _, agent, _ = build_language_executor(LLMConfig(reasoning_provider="litellm", vertex_project="test-project"))
    moment, viewer = _moment(), ViewerProfile()

    async def run():
        stream = agent.run(prompt_payload(moment, viewer), stream=True)
        updates = [update async for update in stream]
        return updates, await stream.get_final_response()

    updates, response = asyncio.run(run())
    assert updates
    assert response.text == render_from_messages([Message("user", [prompt_payload(moment, viewer)])])
    assert "Reasoning dependency litellm is missing" in caplog.text
    assert "install the project requirements" in caplog.text


def test_config_defaults_and_provider_swap(monkeypatch, tmp_path):
    # User secret files must not affect an offline defaults test.
    monkeypatch.setattr("matchos.llm_config.ENV_DIR", tmp_path)
    for name in ["DECISION_PROVIDER", "REASONING_PROVIDER", "AZURE_FOUNDRY_ENDPOINT", "AZURE_DECISION_ENDPOINT", "AZURE_REASONING_ENDPOINT", "CLOUDFLARE_MODEL", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AUTH_TOKEN", "AI_GATEWAY_API_KEY", "DECISION_MODEL", "REASONING_MODEL", "REASONING_API_KEY", "REASONING_BASE_URL", "VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT", "VERTEXAI_LOCATION"]:
        monkeypatch.delenv(name, raising=False)
    config = LLMConfig.from_env()
    assert config.decision_model == "convaiinnovations/laya-free"
    assert config.decision_provider == "azure"
    assert config.reasoning_provider == "azure"
    assert config.azure_decision_deployment == "Decision-1"
    assert config.azure_reasoning_deployment == "gpt-6-luna"
    assert config.cloudflare_model == "@cf/cloudflare/clef-flash"
    assert config.reasoning_model == "vertex_ai/gemini-3.8-flash"
    assert not config.reasoning_enabled
    assert isinstance(build_decision_client(config), LocalDecisionClient)
    monkeypatch.setenv("REASONING_PROVIDER", "litellm")
    monkeypatch.setenv("REASONING_MODEL", "openai/example-model")
    monkeypatch.setenv("REASONING_API_KEY", "test-key")
    monkeypatch.setenv("REASONING_BASE_URL", "https://example.test/v1")
    config = LLMConfig.from_env()
    assert config.reasoning_enabled
    assert config.completion_options()["api_base"] == "https://example.test/v1"
    assert "vertex_project" not in config.completion_options()
    assert "test-key" not in repr(config)


def test_gateway_logs_actionable_error_without_api_key(monkeypatch, caplog):
    key = "private-test-key"

    def respond(request):
        return httpx.Response(403, json={"error": {
            "type": "customer_verification_required",
            "message": f"Add a credit card. Rejected credential: {key}",
        }})

    actual_client = httpx.AsyncClient
    monkeypatch.setattr("matchos.decision_client.httpx.AsyncClient", lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw))
    decision = asyncio.run(GatewayDecisionClient(LLMConfig(gateway_api_key=key)).decide(StateEngine().state, ["goal"]))
    assert decision.provider.startswith("local-decision-fallback:")
    assert "HTTP 403" in caplog.text and "Add a credit card" in caplog.text
    assert key not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.parametrize("config", [LLMConfig(), LLMConfig(reasoning_provider="litellm", vertex_project="test-project")])
def test_chat_client_uses_framework_invocation_layer(config, caplog):
    _, agent, _ = build_language_executor(config)
    assert "does not support function invoking" not in caplog.text
    if not config.reasoning_enabled:
        async def run():
            stream = agent.run(prompt_payload(_moment(), ViewerProfile()), stream=True)
            return await stream.get_final_response()

        response = asyncio.run(run())
        assert response.text == render_from_messages([Message("user", [prompt_payload(_moment(), ViewerProfile())])])


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "inf"])
def test_invalid_timeout_fails_early(monkeypatch, value):
    monkeypatch.setenv("DECISION_TIMEOUT_S", value)
    with pytest.raises(ValueError):
        LLMConfig.from_env()
