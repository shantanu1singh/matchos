"""Workers AI schema, provider selection, and safe failure handling."""

import asyncio
import json
import os
from dataclasses import replace

import httpx
import pytest

from matchos.decision_client import CloudflareDecisionClient, GatewayDecisionClient, LocalDecisionClient, build_decision_client
from matchos.llm_config import LLMConfig
from matchos.schemas import PlayerRollup
from matchos.state_engine import StateEngine


def _result():
    return {
        "model": "clef-flash",
        "answers": {
            **{name: {"type": "noul", "noul": value} for name, value in {
                "tacticalShift": .2, "momentumShift": .3, "playerStory": .9, "shouldSurface": .85,
            }.items()},
            "importance": {"type": "score", "score": 4.2, "confidence": .8,
                           "legend": {str(n): str(n) for n in range(6)},
                           "probabilities": {"4": .8, "5": .2}},
            "primaryFrame": {"type": "choice", "choice": "PLAYER_IMPACT", "confidence": .9,
                             "probabilities": {"PLAYER_IMPACT": .9, "GOAL": .1}},
            "team": {"type": "choice", "choice": "RED", "confidence": .9,
                     "probabilities": {"RED": .9, "BLUE": .1, "NONE": 0}},
        },
        "usage": {"input_tokens": 500, "output_tokens": 0},
    }


def _mock_client(monkeypatch, respond):
    actual_client = httpx.AsyncClient
    monkeypatch.setattr("matchos.decision_client.httpx.AsyncClient", lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw))


@pytest.mark.parametrize("model", ["@cf/cloudflare/clef-flash", "@cf/cloudflare/clef"])
@pytest.mark.parametrize("wrapped", [True, False])
def test_cloudflare_native_contract(monkeypatch, model, wrapped):
    requests = []

    def respond(request):
        requests.append(request)
        result = _result()
        return httpx.Response(200, json={"success": True, "result": result, "errors": []} if wrapped else result)

    _mock_client(monkeypatch, respond)
    config = LLMConfig(decision_provider="cloudflare", cloudflare_model=model, cloudflare_account_id="test-account", cloudflare_api_token="test-token")
    state = StateEngine().state
    state.players["blue_9"] = PlayerRollup(team_id="BLUE", number=9)
    decision = asyncio.run(build_decision_client(config).decide(state, ["player_involvement"], "blue_9"))
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == f"https://api.cloudflare.com/client/v4/accounts/test-account/ai/run/{model}"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert payload["model"] == model.rsplit("/", 1)[-1]
    assert payload["state"]["focusPlayerId"] == "blue_9"
    assert payload["questions"]["shouldSurface"]["type"] == "noul"
    assert payload["questions"]["importance"]["criteria"] == ["0", "1", "2", "3", "4", "5"]
    assert "PLAYER_IMPACT" in payload["questions"]["primaryFrame"]["criteria"]
    assert decision.provider == f"cloudflare:{model}"
    assert decision.frame_scores == {"PLAYER_IMPACT": .9, "GOAL": .1}
    assert decision.importance == 4.2 and decision.should_surface == .85
    assert decision.player_id == "blue_9" and decision.team_id == "BLUE"


@pytest.mark.parametrize("failure", ["timeout", "http", "api_error", "missing", "refusal", "nan", "bad_score", "bad_probabilities", "bad_result"])
def test_cloudflare_fallback_and_redacted_diagnostics(monkeypatch, caplog, failure):
    token = "private-cloudflare-token"

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(token, request=request)
        if failure == "http":
            return httpx.Response(403, json={"success": False, "errors": [{"message": f"Rejected token {token}"}]})
        if failure == "api_error":
            return httpx.Response(200, json={"success": False, "errors": [{"message": token}]})
        result = _result()
        if failure == "missing":
            del result["answers"]["team"]
        elif failure == "refusal":
            result["answers"]["shouldSurface"]["type"] = "refusal"
        elif failure == "nan":
            result["answers"]["shouldSurface"]["noul"] = "NaN"
        elif failure == "bad_score":
            result["answers"]["importance"]["score"] = 6
        elif failure == "bad_probabilities":
            result["answers"]["primaryFrame"]["probabilities"] = []
        elif failure == "bad_result":
            result = []
        return httpx.Response(200, json={"success": True, "result": result})

    _mock_client(monkeypatch, respond)
    config = LLMConfig(decision_provider="cloudflare", cloudflare_account_id="test-account", cloudflare_api_token=token)
    decision = asyncio.run(build_decision_client(config).decide(StateEngine().state, ["goal"]))
    assert decision.primary_frame == "GOAL"
    assert decision.provider == "local-decision-fallback:cloudflare:@cf/cloudflare/clef-flash"
    assert token not in caplog.text and token not in repr(config)
    assert "@cf/cloudflare/clef-flash failed" in caplog.text
    if failure == "http":
        assert "HTTP 403" in caplog.text and "[REDACTED]" in caplog.text
    elif failure == "timeout":
        assert "DECISION_TIMEOUT_S=10" in caplog.text


def test_switching_providers_preserves_both_credentials():
    config = LLMConfig(decision_provider="cloudflare", cloudflare_account_id="test-account", cloudflare_api_token="cf-token", gateway_api_key="vercel-token")
    assert isinstance(build_decision_client(config), CloudflareDecisionClient)
    vercel = build_decision_client(replace(config, decision_provider="vercel"))
    assert type(vercel) is GatewayDecisionClient
    assert vercel.api_key == "vercel-token" and vercel.model == "convaiinnovations/laya-free"
    assert isinstance(build_decision_client(replace(config, decision_provider="local")), LocalDecisionClient)
    # Missing selected-provider credentials never sends a request to another provider.
    assert isinstance(build_decision_client(replace(config, cloudflare_api_token="")), LocalDecisionClient)
    assert isinstance(build_decision_client(replace(config, cloudflare_account_id="")), LocalDecisionClient)


def test_cloudflare_settings_load_from_env_local(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr("matchos.llm_config.ENV_DIR", tmp_path)
    (tmp_path / ".env.local").write_text(
        "DECISION_PROVIDER=cloudflare\nCLOUDFLARE_ACCOUNT_ID=test-account\nCLOUDFLARE_AUTH_TOKEN=cf-token\nAI_GATEWAY_API_KEY=vercel-token\n"
    )
    config = LLMConfig.from_env()
    assert isinstance(build_decision_client(config), CloudflareDecisionClient)
    assert config.cloudflare_api_token == "cf-token"
    assert config.gateway_api_key == "vercel-token"
    monkeypatch.setenv("DECISION_PROVIDER", "vercel")
    assert type(build_decision_client(LLMConfig.from_env())) is GatewayDecisionClient


@pytest.mark.parametrize("setting,value", [("DECISION_PROVIDER", "invalid"), ("CLOUDFLARE_MODEL", "invalid")])
def test_invalid_provider_settings_fail_early(monkeypatch, setting, value, tmp_path):
    monkeypatch.setattr("matchos.llm_config.ENV_DIR", tmp_path)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        LLMConfig.from_env()
