"""Opening context remains a model decision, with explicit publication gates."""
import asyncio
from types import SimpleNamespace

import pytest

from matchos.decision_client import _editorial_state, _parse_selection
from matchos.perspective import Perspective
from matchos.producer import Producer
from matchos.schemas import PipelineResult, VerifiedCandidates, ViewerProfile
from matchos.telemetry import Telemetry
from test_editorial_selection import setup_candidates, answers


def test_broadcaster_policy_distinguishes_opening_from_followup():
    engine, candidates = setup_candidates()
    viewer = ViewerProfile(persona='broadcaster', team_id='RED')
    opening = _editorial_state(engine.state, candidates, viewer, {})
    assert opening['viewingContext']['hasAiredStory'] is False
    assert 'No story has aired' in opening['editorialPolicy']['guidance']
    assert 'substantial increase in final-third entries' in opening['editorialPolicy']['guidance']
    assert 'Still choose NONE' in opening['editorialPolicy']['guidance']
    assert 'team_id' not in opening['viewer']
    assert opening['editorialPolicy']['minimumPublicationScore'] == .5
    assert opening['editorialPolicy']['minimumImportance'] == 2.75
    later = _editorial_state(engine.state, candidates, viewer,
                             {'recentUpdates': [{'frame': 'MOMENTUM_SWING', 'secondsAgo': 30}]})
    assert later['viewingContext']['hasAiredStory'] is True
    assert later['viewingContext']['secondsSinceLastAir'] == 30
    assert 'already received match context' in later['editorialPolicy']['guidance']
    assert 'No story has aired' not in later['editorialPolicy']['guidance']


def test_worker_tracks_actual_first_air_and_keeps_model_abstention():
    async def run():
        engine, candidates = setup_candidates()
        target = next(c for c in candidates if c.frame == 'MOMENTUM_SWING')
        contexts, messages = [], []
        class Selector:
            async def select(self, state, offered, viewer, context):
                contexts.append(context)
                choice = target.id if len(contexts) == 2 else 'NONE'
                return _parse_selection(answers(choice, offered), offered, 'test', 1)
        session = SimpleNamespace(services=SimpleNamespace(decisions=Selector(), agent=None,
                                  language_provider='template'), telemetry=Telemetry(), live_state_engine=engine)
        async def send(message):
            messages.append(message)
        worker = Perspective(session, ViewerProfile(persona='broadcaster'), 0, send)
        worker.started_at -= 120
        batch = PipelineResult(kind='candidates', candidates=VerifiedCandidates(candidates=candidates), state=engine.state)
        await worker.process(batch)
        assert messages[-1]['result']['producer']['action'] == 'SUPPRESS'
        assert worker.recent == []
        await worker.process(batch)
        assert messages[-1]['result']['producer']['action'] == 'AIR'
        await worker.process(batch)
        first, still_waiting, aired = [c['viewingContext'] for c in contexts]
        assert first['hasAiredStory'] is False and first['secondsWaitingForFirstStory'] >= 120
        assert first['secondsSinceLastAir'] is None
        assert still_waiting['hasAiredStory'] is False
        assert aired['hasAiredStory'] is True and aired['secondsSinceLastAir'] >= 0
        assert aired['secondsWaitingForFirstStory'] is None
        assert contexts[-1]['recentUpdates'][0]['momentId'] == target.id
        assert len(worker.recent) == 1
        assert Perspective(session, worker.viewer, 0, send).recent == []
        await worker.close()
    asyncio.run(run())


@pytest.mark.parametrize(('score', 'display'), [(.42, '42%'), (.499999, '49.9999%')])
def test_surface_hold_reports_exact_score_without_forcing_first_story(score, display):
    _, candidates = setup_candidates()
    target = next(c for c in candidates if c.frame == 'MOMENTUM_SWING')
    decision = Producer().decide(target, True, score, ViewerProfile(persona='broadcaster'))
    assert decision.action == 'SUPPRESS'
    assert f'{display} for publication; at least 50%' in decision.reason


def test_broadcaster_importance_hold_explains_gate_and_preserves_cadence():
    _, candidates = setup_candidates()
    target = next(c for c in candidates if c.frame == 'MOMENTUM_SWING')
    viewer = ViewerProfile(persona='broadcaster')
    producer = Producer(clock=lambda: 1000)
    held = producer.decide(target.model_copy(update={'importance': .5}), True, .9, viewer)
    assert held.action == 'SUPPRESS'
    assert 'importance 2.5/5' in held.reason and 'at least 2.75/5' in held.reason
    accepted = producer.decide(target.model_copy(update={'importance': .55}), True, .5, viewer)
    assert accepted.action == 'AIR'
    assert 'Same story aired' in producer.decide(target, True, .9, viewer).reason
