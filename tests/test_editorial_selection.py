"""Editorial selection can choose only evidence-backed candidates or abstain."""
import asyncio
import json

import httpx
import pytest

from matchos.decision_client import GatewayDecisionClient, CloudflareDecisionClient, _parse_selection
from matchos.llm_config import LLMConfig
from matchos.schemas import MatchEvent, PlayerRollup, ViewerProfile, PipelineResult, TriggerResult
from matchos.state_engine import StateEngine
from matchos.verifier import Verifier
from matchos.workflow import new_services, build_workflow, LiveTick
from matchos.simulator import MatchSimulator
from matchos.telemetry import Telemetry


def setup_candidates():
    engine = StateEngine()
    state = engine.state
    state.timestamp_ms = 3_001_000
    state.minute = 50
    state.red.possession_last5 = .7
    state.blue.possession_last5 = .3
    state.red.final_third_entries_last5 = 10
    state.red.final_third_entries_prev5 = 2
    state.deltas.update(red_entries_delta=8)
    state.players['blue_9'] = PlayerRollup(team_id='BLUE', number=9, between_lines_last8=6, between_lines_prev30=2)
    for sequence in range(2):
        engine.events.append(MatchEvent(id=f'pass-{sequence}', match_id='test', sequence=sequence, period=2,
                                       timestamp_ms=state.timestamp_ms - (sequence + 1) * 1000, type='PASS', team_id='BLUE',
                                       player_id='blue_8', target_player_id='blue_9', possession_id=1, outcome='COMPLETE'))
    candidates = Verifier(engine).candidates(state, [], 'blue_9')
    assert {candidate.frame for candidate in candidates.candidates} == {'MOMENTUM_SWING', 'PLAYER_IMPACT'}
    return engine, candidates.candidates


def answers(choice, candidates):
    return {'candidate': {'type': 'choice', 'choice': choice, 'confidence': .85,
                         'probabilities': [{'value': candidate.id, 'probability': .85 if candidate.id == choice else .1} for candidate in candidates] + [{'value': 'NONE', 'probability': .85 if choice == 'NONE' else .05}]},
            'importance': {'type':'score', 'score':4}, 'shouldSurface': {'type':'predicate', 'probability':.9}}


@pytest.mark.parametrize('provider', ['cloudflare','vercel'])
@pytest.mark.parametrize('selection', ['candidate','NONE','unknown','nan','timeout'])
def test_native_editorial_request_and_restricted_output(monkeypatch, provider, selection):
    engine, candidates = setup_candidates()
    target = next(candidate for candidate in candidates if candidate.frame == 'MOMENTUM_SWING')
    requests = []
    def respond(request):
        requests.append(request)
        if selection == 'timeout':
            raise httpx.ReadTimeout('test timeout', request=request)
        reply = answers(target.id if selection == 'candidate' else selection, candidates)
        if selection == 'nan':
            reply['candidate']['confidence'] = 'NaN'
        if provider == 'cloudflare':
            reply['candidate']['probabilities'] = {item['value']:item['probability'] for item in reply['candidate']['probabilities']}
            reply['shouldSurface'] = {'type':'noul', 'noul':.9}
            body = {'success':True, 'result':{'answers':reply}}
        else:
            body = {'answers':[{**value, 'name':name} for name,value in reply.items()]}
        return httpx.Response(200,json=body)
    actual_client = httpx.AsyncClient
    monkeypatch.setattr('matchos.decision_client.httpx.AsyncClient', lambda **kwargs:actual_client(transport=httpx.MockTransport(respond),**kwargs))
    config = LLMConfig(gateway_api_key='test', cloudflare_account_id='account', cloudflare_api_token='test')
    client = CloudflareDecisionClient(config) if provider == 'cloudflare' else GatewayDecisionClient(config)
    viewer = ViewerProfile(persona='fan',team_id='BLUE',player_id='blue_9')
    decision = asyncio.run(client.select(engine.state,candidates,viewer,{'recentUpdates':[{'frame':'PLAYER_IMPACT','secondsAgo':10}], 'triggerReasons':['red_entries']}))
    payload = json.loads(requests[0].content)
    context = payload['state'] if provider == 'cloudflare' else json.loads(payload['input'])
    assert context['viewer']['persona'] == 'fan'
    assert context['recentUpdates'][0]['secondsAgo'] == 10
    momentum = next(candidate for candidate in context['verifiedCandidates'] if candidate['id'] == target.id)
    assert momentum['facts']['possessionLast5'] == '70%'
    assert target.facts['possessionLast5'] == .7
    assert all(candidate['checks'] for candidate in context['verifiedCandidates'])
    question = payload['questions']['candidate'] if provider == 'cloudflare' else next(q for q in payload['questions'] if q['name'] == 'candidate')
    allowed = set(question['criteria']) if provider == 'cloudflare' else {item['value'] for item in question['choices']}
    assert allowed == {candidate.id for candidate in candidates} | {'NONE'}
    if selection == 'candidate':
        assert decision.selected_candidate_id == target.id
        assert decision.primary_frame == target.frame and decision.team_id == 'RED'
    elif selection == 'NONE':
        assert decision.selected_candidate_id is None and decision.should_surface == 0
        assert not decision.provider.startswith('local')
    else:
        assert decision.provider.startswith('local-decision-fallback:')
        assert decision.selected_candidate_id in {candidate.id for candidate in candidates}


@pytest.mark.parametrize('mode',['select','hold','empty'])
def test_workflow_verifies_before_selection_and_respects_exact_choice(monkeypatch,mode):
    monkeypatch.setenv('DECISION_PROVIDER','local')
    monkeypatch.setenv('REASONING_MODEL','local')
    engine, _ = setup_candidates()
    if mode == 'empty':
        engine.state.red.possession_last5 = .5
        engine.state.players['blue_9'].between_lines_last8 = 0
    services = new_services()
    if mode == "empty":
        services.trigger_engine.evaluate = lambda *args: TriggerResult(fired=True, reasons=["substitution"])
    services.state_engine = engine
    services.verifier = Verifier(engine)
    engine.ingest = lambda tick:engine.state
    calls = []
    services.recent_updates = [{'aired_at':__import__('time').monotonic(), 'frame':'PLAYER_IMPACT'}]
    class Selector:
        async def select(self,state,candidates,viewer,context):
            calls.append((candidates,viewer,context))
            assert all(candidate.checks and all(check.passed for check in candidate.checks) for candidate in candidates)
            target = next(candidate for candidate in candidates if candidate.frame == 'MOMENTUM_SWING')
            assert candidates[0].frame == 'PLAYER_IMPACT'  # Model selects a later candidate.
            return _parse_selection(answers(target.id if mode == 'select' else 'NONE',candidates),candidates,'test-editor',1)
    services.decisions = Selector()
    workflow, _ = build_workflow(services)
    viewer = ViewerProfile(persona='fan',player_id='blue_9')
    store = Telemetry()
    async def run():
        tick = MatchSimulator(seed=7,scenario_id='balanced').step(1000)
        with store.capture('Match workflow',{}):
            return [event.data async for event in workflow.run(LiveTick(tick=tick,viewer=viewer),stream=True) if isinstance(event.data,PipelineResult)]
    outputs = asyncio.run(run())
    initial = next(output for output in outputs if output.kind != 'enrichment')
    if mode == 'empty':
        assert calls == [] and initial.decision is None
        assert initial.producer.action == 'REJECT'
    elif mode == 'hold':
        assert len(calls) == 1 and initial.moment is None
        assert initial.producer.action == 'SUPPRESS'
        assert initial.verification is None  # Editorial abstention is not a factual failure.
        assert not any(output.kind == 'enrichment' for output in outputs)
    else:
        assert len(calls) == 1
        assert initial.moment.frame == 'MOMENTUM_SWING'
        assert initial.moment.facts['possessionLast5'] == .7
        assert initial.moment.importance == .8 and initial.moment.confidence == .85
        assert initial.producer.action == 'AIR'
        assert any(output.kind == 'enrichment' for output in outputs)
        assert calls[0][2]['recentUpdates'][0]['frame'] == 'PLAYER_IMPACT'
    names = [step['name'] for step in store.snapshot()['runs'][0]['steps']]
    assert names.index('verified candidates') < names.index('editorial selection')
