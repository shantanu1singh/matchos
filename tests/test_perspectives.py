import asyncio
from types import SimpleNamespace

from matchos.editorial import explain
from matchos.perspective import Perspective
from matchos.schemas import PipelineResult, VerifiedCandidates, ViewerProfile, ProducerDecision
from matchos.telemetry import Telemetry
from matchos.decision_client import _parse_selection
from test_editorial_selection import setup_candidates, answers


def test_worker_coalesces_without_cancelling_inflight_selection_and_cancels_on_close():
    async def run():
        engine, candidates = setup_candidates()
        started, release = asyncio.Event(), asyncio.Event()
        calls, messages = [], []
        class Selector:
            async def select(self, state, offered, viewer, context):
                calls.append(state.timestamp_ms)
                if len(calls) == 1:
                    started.set()
                    await release.wait()
                return _parse_selection(answers('NONE',offered),offered,'test',1)
        session = SimpleNamespace(services=SimpleNamespace(decisions=Selector(), agent=None), telemetry=Telemetry(), live_state_engine=engine)
        async def send(message): messages.append(message)
        worker = Perspective(session,ViewerProfile(persona='casual'),2,send)
        def batch(stamp):
            state = engine.state.model_copy(deep=True)
            state.timestamp_ms = stamp
            return PipelineResult(kind='candidates', candidates=VerifiedCandidates(candidates=candidates), state=state)
        worker.offer(batch(1000))
        await asyncio.wait_for(started.wait(),1)
        for stamp in range(2000,11000,1000): worker.offer(batch(stamp))
        assert calls == [1000]
        release.set()
        await asyncio.wait_for(worker.task,1)
        assert calls == [1000,10000]
        assert all(message['explanation']['status'] == 'no_interruption' for message in messages)
        assert worker.recent == []
        await worker.close()
        worker.offer(batch(12000))
        assert worker.pending is None
    asyncio.run(run())


def test_explanation_labels_model_preferences_and_preserves_exact_evidence():
    _, candidates = setup_candidates()
    chosen = next(candidate for candidate in candidates if candidate.frame == 'MOMENTUM_SWING')
    reply = answers(chosen.id,candidates)
    reply['reason'] = {'type':'choice','choice':'TEAM_RELEVANCE'}
    decision = _parse_selection(reply,candidates,'cloudflare:test',1)
    result = PipelineResult(kind='moment', candidates=VerifiedCandidates(candidates=candidates), decision=decision,
                            moment=chosen, producer=ProducerDecision(action='AIR',reason='Accepted'))
    before = chosen.model_dump()
    description = explain(result, ViewerProfile(team_id='RED'))
    assert description['reasonSource'] == 'model'
    assert "viewer's team" in description['reason']
    assert description['evidence'] == [check.detail for check in chosen.checks][:2]
    assert description['held'][0]['id'] != chosen.id
    assert chosen.model_dump() == before
    # An unsupported model reason cannot masquerade as evidence of team relevance.
    other = explain(result,ViewerProfile(team_id='BLUE'))
    assert "viewer's team" not in other['reason']
    result.producer = ProducerDecision(action='SUPPRESS',reason='Same story aired inside the last 90 seconds.')
    held = explain(result,ViewerProfile())
    assert held['reasonSource'] == 'policy'
    assert held['reason'] == result.producer.reason
    assert chosen.id in [item['id'] for item in held['held']]


def test_additional_player_focus_candidates_share_one_pool():
    from matchos.schemas import PlayerRollup
    from matchos.verifier import Verifier
    engine, _ = setup_candidates()
    engine.state.players['red_7'] = PlayerRollup(team_id='RED',number=7,between_lines_last8=6)
    for event in list(engine.events):
        engine.events.append(event.model_copy(update={'id':'red-'+event.id,'team_id':'RED','target_player_id':'red_7'}))
    pool = Verifier(engine).candidates(engine.state,[],'red_7',['blue_9','blue_9'])
    focuses = [candidate.player_id for candidate in pool.candidates if candidate.frame == 'PLAYER_IMPACT']
    assert focuses == ['red_7','blue_9']
    assert len({candidate.id for candidate in pool.candidates}) == len(pool.candidates)
