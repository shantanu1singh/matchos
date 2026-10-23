"""The default demo's turning points must exist in the feed and reach personas."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from matchos.copy import prose_is_faithful, render_overlay
from matchos.decision_client import LocalDecisionClient, _editorial_state
from matchos.language_agent import prompt_payload
from matchos.perspective import Perspective
from matchos.producer import Producer
from matchos.schemas import PipelineResult, VerifiedCandidates, ViewerProfile
from matchos.scenarios import style_for
from matchos.session import MatchSession
from matchos.simulator import MatchSimulator
from matchos.state_engine import StateEngine
from matchos.telemetry import Telemetry
from matchos.verifier import Verifier, MAJOR_STORY_TTL_MS


@pytest.fixture(scope='module')
def replay():
    sim = MatchSimulator(seed=47129, scenario_id='press_collapse')
    engine = StateEngine()
    for tick in sim.collect_warmup(): engine.ingest(tick)
    events, snapshots = [], []
    while sim.clock_ms < 75 * 60000:
        tick = sim.step(5000)
        state = engine.ingest(tick)
        events.extend(tick.events)
        if any(event.type == 'CARD' or (event.type == 'SHOT' and event.outcome == 'GOAL') for event in tick.events):
            pool = Verifier(engine).candidates(state, tick.events, 'red_7')
            snapshots.append((state.model_copy(deep=True), pool, tick))
    return sim, engine, events, snapshots


def test_press_collapse_has_one_added_goal_one_dismissal_and_real_match_effects(replay):
    sim, engine, events, snapshots = replay
    goals = [e for e in events if e.type == 'SHOT' and e.outcome == 'GOAL' and e.qualifiers.get('scripted')]
    assert [(e.minute, e.second, e.player_id) for e in goals] == [(65, 3, 'red_9')]
    assert any(e.type == 'PASS' and e.target_player_id == 'red_9' and e.qualifiers.get('scripted') for e in events)
    cards = [e for e in events if e.type == 'CARD' and e.qualifiers.get('card') == 'red']
    assert [(e.minute, e.player_id) for e in cards] == [(68, 'blue_8')]
    assert (sim.score_red, sim.score_blue) == (2, 1)
    assert [(s.blue.red_cards, s.blue.players_on_pitch) for s, _, _ in snapshots] == [(0,11),(1,10)]
    assert engine.state.red.players_on_pitch == 11
    assert not sim.players['blue_8'].on_pitch and sim.players['blue_5'].on_pitch
    assert all(p.player_id != 'blue_8' for p in sim._frame().players)
    assert len(sim._on_pitch('BLUE')) == 10
    assert not any(e.player_id == 'blue_8' and e.timestamp_ms > 68 * 60000 for e in events)
    before, after = style_for('press_collapse',67,'BLUE'), style_for('press_collapse',75,'BLUE')
    assert after['press'] < before['press'] and after['defensive_line'] < before['defensive_line'] and after['width'] < before['width']


def test_events_have_current_verified_context_and_persona_specific_copy(replay):
    _, engine, _, snapshots = replay
    assert {m.frame for m in snapshots[0][1].candidates} >= {'GOAL'}
    assert 'COMEBACK' not in {m.frame for m in snapshots[0][1].candidates}
    for index, (state, pool, tick) in enumerate(snapshots):
        frame = 'GOAL' if index == 0 else 'RED_CARD'
        moment = next(m for m in pool.candidates if m.frame == frame)
        assert moment.evidence and all(check.passed for check in moment.checks)
        assert moment.facts['scoreRed'] == 2 and moment.facts['scoreBlue'] == 1
        assert moment.facts['playersBlue'] == 11 - index
        assert moment.facts['redCardsBlue'] == index
        assert moment.expires_at_ms - moment.evidence_detail[0].timestamp_ms == MAJOR_STORY_TTL_MS
        bodies = []
        for persona in ('analyst','fan','broadcaster'):
            viewer = ViewerProfile(persona=persona)
            overlay = render_overlay(moment, viewer, rich=True, provider='test')
            bodies.append(overlay.body)
            assert prose_is_faithful(overlay.body, moment)
            if index == 1:
                assert '10' in overlay.body
            model = _editorial_state(state, pool.candidates, viewer, {})
            assert model['playersOnPitch']['BLUE'] == 11-index
            assert model['redCards']['BLUE'] == index
            assert json.loads(prompt_payload(moment, viewer))['moment']['facts']['playersBlue'] == 11-index
        assert len(set(bodies)) == 3
        # A busy viewer can still recover a recent major event from rolling evidence.
        later = state.model_copy(update={'timestamp_ms': state.timestamp_ms + 20000})
        refreshed = Verifier(engine).candidates(later, [], 'red_7')
        again = next(m for m in refreshed.candidates if m.frame == frame)
        assert again.evidence == moment.evidence
        assert again.expires_at_ms == moment.expires_at_ms  # No endlessly renewed deadline.


def test_each_persona_airs_each_turning_point_once_even_when_pool_ids_change(replay):
    _, _, _, snapshots = replay
    async def run():
        for persona in ('analyst','fan','broadcaster'):
            live = SimpleNamespace(state=snapshots[0][0])
            session = SimpleNamespace(services=SimpleNamespace(decisions=LocalDecisionClient(),agent=None,
                        language_provider='template'), live_state_engine=live, telemetry=Telemetry())
            messages = []
            async def send(message): messages.append(message)
            worker = Perspective(session, ViewerProfile(persona=persona), 0, send)
            for state, pool, _ in snapshots:
                live.state = state
                batch = PipelineResult(kind='candidates', candidates=pool, state=state)
                await worker.process(batch)
                aired = messages[-1]
                assert aired['explanation']['status'] == 'update'
                assert aired['result']['moment']['frame'] == ('GOAL' if state.blue.red_cards == 0 else 'RED_CARD')
                major = next(m for m in pool.candidates if m.frame == aired['result']['moment']['frame'])
                duplicate = major.model_copy(update={'id':'another-pool-id'})
                await worker.process(batch.model_copy(update={'candidates': VerifiedCandidates(candidates=[duplicate])}))
                assert messages[-1]['explanation']['status'] == 'no_interruption'
            assert len([m for m in messages if m['explanation']['status'] == 'update']) == 2
            assert len(worker.recent) == 2
            await worker.close()
    asyncio.run(run())


def test_direct_producer_deduplicates_the_dismissal(replay):
    _, _, _, snapshots = replay
    producer, viewer = Producer(), ViewerProfile(persona='analyst')
    first = next(m for m in snapshots[1][1].candidates if m.frame == 'RED_CARD')
    assert producer.decide(first, True, 1, viewer).action == 'AIR'
    assert producer.decide(first.model_copy(update={'id':'new-id'}), True, 1, viewer).action == 'SUPPRESS'


def test_seek_rebuilds_goal_score_cards_and_player_counts(monkeypatch, replay):
    monkeypatch.setenv('DECISION_PROVIDER','local')
    monkeypatch.setenv('REASONING_MODEL','local')
    async def run():
        session = MatchSession('press_collapse',47129,1,ViewerProfile(persona='analyst'))
        try:
            frame = await session.seek_to(75 * 60000)
            assert frame == replay[0]._frame()
            assert session.live_state_engine.state.blue.red_cards == 1
            assert session.live_state_engine.state.blue.players_on_pitch == 10
            assert session.services.state_engine.state == session.live_state_engine.state
        finally:
            await session.close()
    asyncio.run(run())


def test_seek_just_after_a_dismissal_recovers_it_without_waiting_for_a_tactical_trigger(monkeypatch):
    monkeypatch.setenv('DECISION_PROVIDER','local')
    monkeypatch.setenv('REASONING_MODEL','local')
    async def run():
        session = MatchSession('press_collapse',47129,1,ViewerProfile(persona='analyst'))
        try:
            await session.seek_to(68 * 60000 + 1000)
            # Ensure the recovered event itself is sufficient to open the gate.
            from matchos.schemas import TriggerResult
            session.services.trigger_engine.evaluate = lambda *args: TriggerResult(fired=False)
            await session.tick()
            await asyncio.wait_for(session._model_task, 2)
            batches = []
            while not session._outputs.empty():
                result, _ = session._outputs.get_nowait()
                if result.kind == 'candidates': batches.append(result)
            assert len(batches) == 1 and 'red_card' in batches[0].trigger.reasons
            card = next(m for m in batches[0].candidates.candidates if m.frame == 'RED_CARD')
            assert card.player_id == 'blue_8' and card.facts['playersBlue'] == 10
        finally:
            await session.close()
    asyncio.run(run())
