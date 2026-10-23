"""Exercise Microsoft clients through their HTTP boundaries, without Azure credentials."""
import asyncio
from dataclasses import replace
import json
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from azure.core import AsyncPipelineClient
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest import AsyncHttpResponse
from azure.core.credentials import AccessToken

from matchos.azure_foundry import FoundryDecisionClient, TOKEN_SCOPE
from matchos.decision_client import build_decision_client, LocalDecisionClient
from matchos.language_agent import build_language_executor, personalize, prompt_payload, render_from_messages
from matchos.llm_config import LLMConfig
from matchos.schemas import ViewerProfile
from matchos.state_engine import StateEngine
from matchos.telemetry import Telemetry
from matchos.verifier import Verifier
from agent_framework import Message


def configured(**kwargs):
    return LLMConfig(azure_endpoint="https://matchos.services.ai.azure.com", azure_api_key="secret-azure-key", **kwargs)


def candidate():
    engine = StateEngine()
    state = engine.state
    state.timestamp_ms = 3_001_000
    state.minute = 50
    state.red.possession_last5 = .7
    state.red.final_third_entries_last5 = 10
    state.red.final_third_entries_prev5 = 2
    state.deltas.update(red_entries_delta=8)
    return state, Verifier(engine).candidates(state, [], None).candidates[0]


def decision_transport(monkeypatch, respond):
    requests = []

    class Transport(AsyncHttpTransport):
        async def open(self): pass
        async def close(self): pass
        async def __aexit__(self, *args): await self.close()

        async def send(self, request, **kwargs):
            requests.append(request)
            status, body = await respond(request)
            response = MagicMock(spec=AsyncHttpResponse)
            response.request = request
            response.status_code = status
            response.headers = {"content-type": "application/json"}
            response.content_type = "application/json"
            response.json.return_value = body
            response.text.return_value = json.dumps(body)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock()
            if status >= 400:
                response.raise_for_status.side_effect = HttpResponseError(response=response)
            return response

    monkeypatch.setattr("matchos.azure_foundry.AsyncPipelineClient",
                        lambda *a, **kw: AsyncPipelineClient(*a, transport=Transport(), **kw))
    return requests


@pytest.mark.parametrize("mode", ["select", "NONE", "unknown", "nan", "401", "timeout"])
def test_decision_sdk_contract_and_fallback(monkeypatch, caplog, mode):
    state, moment = candidate()

    async def respond(request):
        if mode == "timeout":
            await asyncio.sleep(1)
        choice = moment.id if mode == "select" else mode
        return (401 if mode == "401" else 200), {"answers": {
            "candidate": {"type": "choice", "choice": choice, "probabilities": {moment.id: .9, "NONE": .1},
                          "confidence": "NaN" if mode == "nan" else .9},
            "importance": {"type": "score", "score": 4.2},
            "shouldSurface": {"type": "noul", "noul": .95},
        }}

    requests = decision_transport(monkeypatch, respond)
    config = configured(decision_timeout_s=.04)
    client = build_decision_client(config)
    assert isinstance(client, FoundryDecisionClient)
    telemetry = Telemetry()

    async def run():
        with telemetry.capture("Azure editorial", {}):
            return await client.select(state, [moment], ViewerProfile(persona="analyst"), {"recentUpdates": []})

    decision = asyncio.run(run())
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url == config.azure_endpoint + "/providers/microsoft/v1/systemone"
    assert request.headers["api-key"] == "secret-azure-key"
    assert "Authorization" not in request.headers
    assert payload["model"] == "Decision-1"
    assert payload["questions"]["shouldSurface"]["type"] == "noul"
    assert set(payload["questions"]["candidate"]["criteria"]) == {moment.id, "NONE"}
    assert payload["state"]["verifiedCandidates"][0]["facts"]["possessionLast5"] == "70%"
    assert len(requests) == 1  # No SDK retries extending the editorial deadline.
    if mode in {"select", "NONE"}:
        assert decision.provider == "azure-foundry:Decision-1"
        assert decision.selected_candidate_id == (moment.id if mode == "select" else None)
    else:
        assert decision.provider == "local-decision-fallback:azure-foundry:Decision-1"
    assert "REQUEST azure-foundry:Decision-1" in caplog.text
    assert "secret-azure-key" not in caplog.text
    assert telemetry.snapshot()["runs"][0]["steps"][0]["input"]["model"] == "Decision-1"
    if mode == "401":
        assert "HTTP 401" in caplog.text


def reasoning_http(monkeypatch, reply, *, status=200):
    requests = []

    async def send(self, request, **kwargs):
        requests.append(request)
        if reply == "timeout":
            await asyncio.sleep(1)
        body = {"id": "resp_test", "object": "response", "created_at": 1,
                "model": "gpt-6-luna", "status": "completed", "parallel_tool_calls": False,
                "tool_choice": "auto", "tools": [], "output": [{"id": "msg_test", "type": "message",
                    "role": "assistant", "status": "completed", "content": [
                        {"type": "output_text", "text": reply, "annotations": []}]}]}
        if status >= 400:
            body = {"error": {"message": "secret-azure-key", "type": "authentication_error"}}
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return requests


@pytest.mark.parametrize("reply,status", [("Liverpool have the momentum.", 200), ("999 passes", 200),
                                           ("", 200), ("ignored", 401), ("timeout", 200)])
def test_native_framework_reasoning_and_fact_guard(monkeypatch, caplog, reply, status):
    requests = reasoning_http(monkeypatch, reply, status=status)
    _, moment = candidate()
    viewer = ViewerProfile(persona="analyst")
    executor, agent, provider = build_language_executor(configured(reasoning_timeout_s=.1))
    text = asyncio.run(personalize(agent, moment, viewer))
    assert executor.id == "language_agent" and provider == "azure-foundry:gpt-6-luna"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/openai/v1/responses"
    assert request.headers["api-key"] == "secret-azure-key"
    payload = json.loads(request.content)
    assert payload["model"] == "gpt-6-luna"
    assert payload["store"] is False
    assert "MatchOS language renderer" in payload["instructions"]
    assert moment.id in json.dumps(payload["input"])
    if reply == "Liverpool have the momentum.":
        assert text == reply
    else:
        assert text == render_from_messages([Message("user", [prompt_payload(moment, viewer)])])
    assert "REQUEST azure-foundry:gpt-6-luna" in caplog.text
    assert "secret-azure-key" not in caplog.text
    if status == 401:
        assert '"status": 401' in caplog.text


def test_entra_for_both_clients_and_streaming(monkeypatch):
    credentials = []

    class Credential:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): await self.close()
        async def close(self): self.closed = True
        async def get_token(self, *scopes, **kwargs):
            assert scopes == (TOKEN_SCOPE,)
            return AccessToken("entra-secret-token", 4_000_000_000)

    def create():
        credential = Credential()
        credentials.append(credential)
        return credential

    monkeypatch.setattr("matchos.azure_foundry.DefaultAzureCredential", create)
    async def respond(request):
        assert request.headers["Authorization"] == "Bearer entra-secret-token"
        assert "api-key" not in request.headers
        return 200, {}

    decision_transport(monkeypatch, respond)
    requests = reasoning_http(monkeypatch, "Liverpool have the momentum.")
    config = replace(configured(), azure_api_key="", azure_auth_mode="entra")
    _, moment = candidate()
    _, agent, _ = build_language_executor(config)

    async def run():
        await FoundryDecisionClient(config)._request({"model": "Decision-1"})
        stream = agent.run(prompt_payload(moment, ViewerProfile()), stream=True)
        return await stream.get_final_response()

    response = asyncio.run(run())
    assert response.text == "Liverpool have the momentum."
    assert requests[0].headers["Authorization"] == "Bearer entra-secret-token"
    assert "api-key" not in requests[0].headers
    assert len(credentials) == 2 and all(credential.closed for credential in credentials)


def test_cancellation_does_not_turn_into_a_fallback(monkeypatch):
    async def respond(request):
        raise asyncio.CancelledError()
    decision_transport(monkeypatch, respond)
    state, moment = candidate()
    async def run():
        await FoundryDecisionClient(configured()).select(state, [moment], ViewerProfile(), {})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


def test_azure_env_loading_and_separate_resources(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {"AZURE_REASONING_DEPLOYMENT": "shell-deployment"})
    monkeypatch.setattr("matchos.llm_config.ENV_DIR", tmp_path)
    (tmp_path / ".env").write_text("AZURE_FOUNDRY_ENDPOINT=https://base.services.ai.azure.com/\nAZURE_FOUNDRY_API_KEY=base-key\n")
    (tmp_path / ".env.local").write_text("AZURE_FOUNDRY_API_KEY=local-secret\nAZURE_REASONING_DEPLOYMENT=local-deployment\nAZURE_DECISION_ENDPOINT=https://decisions.services.ai.azure.com\nAZURE_DECISION_API_KEY=decision-secret\n")
    config = LLMConfig.from_env()
    assert config.decision_provider == config.reasoning_provider == "azure"
    assert config.azure_reasoning_deployment == "shell-deployment"
    assert config.azure_resource_endpoint("decision") == "https://decisions.services.ai.azure.com"
    assert config.azure_resource_endpoint("reasoning") == "https://base.services.ai.azure.com"
    assert config.azure_key("decision") == "decision-secret" and config.azure_key("reasoning") == "local-secret"
    assert "secret" not in repr(config)
    assert config.reasoning_enabled
    assert not replace(config, reasoning_provider="local").reasoning_enabled
    assert isinstance(build_decision_client(replace(config, azure_api_key="", azure_decision_api_key="")), LocalDecisionClient)


@pytest.mark.parametrize("name,value", [("REASONING_PROVIDER", "typo"), ("AZURE_AUTH_MODE", "typo"),
    ("AZURE_FOUNDRY_ENDPOINT", "https://example.test/api/projects/project"),
    ("AZURE_REASONING_ENDPOINT", "https://example.test/openai/v1"),
    ("AZURE_DECISION_ENDPOINT", "http://example.test")])
def test_bad_azure_configuration_fails_early(monkeypatch, tmp_path, name, value):
    monkeypatch.setattr(os, "environ", {name: value})
    monkeypatch.setattr("matchos.llm_config.ENV_DIR", tmp_path)
    with pytest.raises(ValueError, match=name):
        LLMConfig.from_env()
