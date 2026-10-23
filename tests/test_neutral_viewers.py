"""Neutral roles must not inherit hidden team/player preferences."""
import asyncio
import json

import httpx
import pytest

from matchos.copy import render_overlay
from matchos.decision_client import CloudflareDecisionClient, GatewayDecisionClient, LocalDecisionClient
from matchos.editorial import explain
from matchos.language_agent import prompt_payload
from matchos.llm_config import LLMConfig
from matchos.schemas import PipelineResult, ProducerDecision, VerifiedCandidates, ViewerProfile
from test_editorial_selection import answers, setup_candidates


@pytest.mark.parametrize('persona', ['analyst', 'broadcaster'])
@pytest.mark.parametrize('team', ['RED', 'BLUE'])
@pytest.mark.parametrize('provider', ['cloudflare', 'vercel'])
def test_neutrality_reaches_native_decisions_and_language(monkeypatch, persona, team, provider):
    engine, candidates = setup_candidates()
    target = candidates[0]
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        reply = answers(target.id, candidates)
        if provider == 'cloudflare':
            reply['candidate']['probabilities'] = {item['value']: item['probability'] for item in reply['candidate']['probabilities']}
            reply['shouldSurface'] = {'type': 'noul', 'noul': .9}
            body = {'success': True, 'result': {'answers': reply}}
        else:
            body = {'answers': [{**value, 'name': name} for name, value in reply.items()]}
        return httpx.Response(200, json=body)
    actual = httpx.AsyncClient
    monkeypatch.setattr('matchos.decision_client.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(respond), **kw))
    viewer = ViewerProfile(persona=persona, team_id=team, player_id='blue_9')
    assert viewer.team_id is None  # Legacy clients cannot restore an allegiance.
    config = LLMConfig(gateway_api_key='test', cloudflare_account_id='test', cloudflare_api_token='test')
    client = CloudflareDecisionClient(config) if provider == 'cloudflare' else GatewayDecisionClient(config)
    decision = asyncio.run(client.select(engine.state, candidates, viewer, {}))
    assert decision.selected_candidate_id == target.id
    request = requests[0]
    editorial = request['state'] if provider == 'cloudflare' else json.loads(request['input'])
    language = json.loads(prompt_payload(target, viewer))
    for payload in (editorial, language):
        assert payload['viewer']['scope'] == 'whole_match'
        assert 'team_id' not in payload['viewer']
        assert 'player_id' not in payload['viewer']
        assert payload['roster']['teams'].keys() == {'RED', 'BLUE'}
    overlay = render_overlay(target, viewer, rich=True, provider='test')
    assert overlay.team_id is None
    decision.editorial_reason = 'TEAM_RELEVANCE'
    result = PipelineResult(kind='moment', candidates=VerifiedCandidates(candidates=candidates), decision=decision,
                            moment=target, producer=ProducerDecision(action='AIR', reason='Accepted'))
    assert "viewer's team" not in explain(result, viewer)['reason']


def test_fan_keeps_liverpool_and_player_keeps_only_focus_in_prompts():
    assert ViewerProfile(persona='fan', team_id=None).model_profile() == {
        'persona': 'fan', 'team_id': 'RED', 'language': 'en', 'scope': 'supported_team'}
    assert ViewerProfile(persona='player', player_id='blue_9').model_profile() == {
        'persona': 'player', 'language': 'en', 'player_id': 'blue_9', 'scope': 'focused_player'}


def test_local_fallback_can_choose_different_evidence_for_player_and_analyst():
    engine, candidates = setup_candidates()
    client = LocalDecisionClient()
    async def run():
        analyst = await client.select(engine.state, candidates, ViewerProfile(persona='analyst', player_id='blue_9'), {})
        player = await client.select(engine.state, candidates, ViewerProfile(persona='player', player_id='blue_9'), {})
        assert analyst.primary_frame == 'MOMENTUM_SWING'
        assert player.primary_frame == 'PLAYER_IMPACT'
        for persona in ('analyst', 'broadcaster'):
            first = await client.select(engine.state, candidates, ViewerProfile(persona=persona, team_id='RED', player_id='blue_9'), {})
            other = await client.select(engine.state, candidates, ViewerProfile(persona=persona, team_id='BLUE', player_id='red_2'), {})
            assert first.candidate_scores == other.candidate_scores
    asyncio.run(run())


def test_local_fallback_holds_repeats_but_still_surfaces_a_major_event():
    engine, candidates = setup_candidates()
    context = {'recentUpdates': [{'frame': candidate.frame, 'secondsAgo': 5} for candidate in candidates]}
    viewer = ViewerProfile(persona='broadcaster')
    async def run():
        client = LocalDecisionClient()
        held = await client.select(engine.state, candidates, viewer, context)
        assert held.selected_candidate_id is None and held.should_surface == 0
        goal = candidates[0].model_copy(update={'id': 'goal-now', 'frame': 'GOAL'})
        aired = await client.select(engine.state, [*candidates, goal], viewer, context)
        assert aired.selected_candidate_id == 'goal-now'
    asyncio.run(run())
