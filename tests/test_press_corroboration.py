"""A counterattack ratio alone must not become a press-collapse narrative."""

import asyncio

from matchos.copy import render_overlay
from matchos.schemas import MatchEvent, Point, ViewerProfile
from matchos.session import MatchSession
from matchos.state_engine import StateEngine
from matchos.verifier import Verifier


def fixture():
    engine = StateEngine()
    state = engine.state
    state.timestamp_ms = 3700000
    state.minute, state.second = 61, 40
    state.score_red = state.score_blue = 1
    state.red.press_bypass_rate, state.red.press_bypass_prev = 0.875, 1 / 3
    state.red.press_bypass_sample = 8
    state.red.defensive_line_prev_m, state.red.defensive_line_m = 33.94, 33.51
    state.red.final_third_entries_prev5, state.red.final_third_entries_last5 = 2, 13
    state.blue.press_bypass_rate, state.blue.press_bypass_prev = 0.9, 0.4
    state.blue.press_bypass_sample = 10
    state.blue.defensive_line_prev_m, state.blue.defensive_line_m = 37.15, 30.02
    state.blue.final_third_entries_prev5 = state.blue.final_third_entries_last5 = 4
    for defending in ("RED", "BLUE"):
        attacking = "BLUE" if defending == "RED" else "RED"
        for sequence in (1, 2):
            engine.events.append(
                MatchEvent(
                    id=f"{attacking}-{sequence}",
                    match_id="test",
                    sequence=sequence,
                    period=2,
                    timestamp_ms=state.timestamp_ms - sequence * 1000,
                    type="PASS",
                    team_id=attacking,
                    player_id=f"{attacking.lower()}_8",
                    target_player_id=f"{attacking.lower()}_9",
                    possession_id=1,
                    location=Point(x=70 if defending == "RED" else 35, y=30),
                    end_location=Point(x=50 if defending == "RED" else 55, y=30),
                    outcome="COMPLETE",
                    qualifiers={
                        "defending_team": defending,
                        "defending_first_line_x": 65 if defending == "RED" else 40,
                    },
                )
            )
    return engine, state


def press(engine, state):
    pool = Verifier(engine).candidates(state, [], "red_7")
    return next((m for m in pool.candidates if m.frame == "PRESS_BREAKDOWN"), None), pool


def test_large_counterattack_ratio_cannot_displace_corroborated_press_failure():
    engine, state = fixture()
    moment, pool = press(engine, state)
    assert moment.team_id == "BLUE"
    assert moment.facts["attackingTeam"] == "Liverpool"
    assert moment.facts["defendingTeam"] == "Manchester City"
    assert all(event.team_id == "RED" for event in moment.evidence_detail)
    assert any(check.name == "red_press_corroboration" and not check.passed for check in pool.checks)
    assert not any(check.name.startswith("red_") for check in moment.checks)
    assert (
        "Liverpool are getting through Manchester City's press"
        in render_overlay(moment, ViewerProfile(persona="broadcaster"), rich=True, provider="test").headline
    )


def test_rising_ratio_without_retreat_or_added_penetration_is_not_a_collapse():
    engine, state = fixture()
    state.blue.defensive_line_prev_m = state.blue.defensive_line_m
    state.red.final_third_entries_prev5 = state.red.final_third_entries_last5
    assert press(engine, state)[0] is None


def test_supported_city_attack_remains_eligible_without_any_team_bias():
    engine, state = fixture()
    state.blue.defensive_line_prev_m = state.blue.defensive_line_m
    state.red.final_third_entries_prev5 = state.red.final_third_entries_last5
    state.blue.final_third_entries_last5 = 10
    moment, _ = press(engine, state)
    assert moment.team_id == "RED"
    assert moment.facts["attackingTeam"] == "Manchester City"
    assert all(event.team_id == "BLUE" for event in moment.evidence_detail)
    assert not any(check.name.startswith("blue_") for check in moment.checks)


def test_default_lab_rejects_screenshot_story_but_keeps_liverpool_development():
    async def run():
        session = MatchSession(
            "press_collapse", 47129, 1, ViewerProfile(persona="broadcaster"), running=False
        )
        try:
            await session.seek_to(61 * 60000 + 40000)
            engine = session.services.state_engine
            state = engine.state
            assert state.red.possession_last5 > state.blue.possession_last5
            assert state.blue.final_third_entries_last5 == state.blue.final_third_entries_prev5 == 4
            moment, pool = press(engine, state)
            assert moment is None
            assert state.red.final_third_entries_last5 > state.red.final_third_entries_prev5
            # Do not force an alternative story when its own thresholds aren't met.
            assert not any(m.frame == "PRESS_BREAKDOWN" for m in pool.candidates)
            while session.simulator.clock_ms < 64 * 60000:
                engine.ingest(session.simulator.step(1000))
            moment, _ = press(engine, engine.state)
            assert moment.facts["attackingTeam"] == "Liverpool"
            assert moment.facts["defendingTeam"] == "Manchester City"
        finally:
            await session.close()

    asyncio.run(run())
