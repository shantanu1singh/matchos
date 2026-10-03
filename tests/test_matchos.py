"""Intelligence and workflow tests that do not need a browser or a model key."""

from __future__ import annotations

import asyncio
import math

from matchos.copy import prose_is_faithful, render_overlay
from matchos.pitch import Point
from matchos.schemas import MatchEvent, MomentDecision, ViewerProfile
from matchos.simulator import MatchSimulator
from matchos.state_engine import StateEngine
from matchos.trigger_engine import TriggerEngine
from matchos.verifier import Verifier
from matchos.workflow import LiveTick, build_workflow


def _pass(sequence: int, timestamp: int, start_x: float, end_x: float, line: float, outcome: str = "COMPLETE") -> MatchEvent:
    return MatchEvent(
        id=f"event-{sequence}",
        match_id="m-test",
        sequence=sequence,
        period=2,
        timestamp_ms=timestamp,
        type="PASS",
        team_id="RED",
        player_id="red_8",
        target_player_id="red_7",
        possession_id=1,
        location=Point(x=start_x, y=34),
        end_location=Point(x=end_x, y=34),
        outcome=outcome,
        qualifiers={
            "defending_team": "BLUE",
            "defending_first_line_x": line,
            "defending_line_x": line + 18,
            "entered_final_third": end_x >= 70 and start_x < 70 and outcome == "COMPLETE",
        },
    )


def test_same_seed_replays_the_same_events() -> None:
    def signatures(seed: int) -> list[tuple]:
        sim = MatchSimulator(seed=seed, scenario_id="press_collapse")
        ticks = sim.collect_warmup()
        ticks.append(sim.step(5_000))
        return [(event.type, event.team_id, event.timestamp_ms, event.outcome) for tick in ticks for event in tick.events]

    assert signatures(47129) == signatures(47129)
    assert signatures(47129) != signatures(99)


def test_verifier_rejects_a_thin_press_sample() -> None:
    engine = StateEngine()
    verifier = Verifier(engine)
    state = engine.state
    state.timestamp_ms = 60 * 60_000
    state.blue.press_bypass_rate = 0.8
    state.blue.press_bypass_prev = 0.2
    state.blue.press_bypass_sample = 3
    decision = MomentDecision(
        tactical_shift=0.9,
        momentum_shift=0.1,
        player_story=0.1,
        importance=4.0,
        primary_frame="PRESS_BREAKDOWN",
        primary_frame_confidence=0.9,
        should_surface=0.9,
        frame_scores={"PRESS_BREAKDOWN": 0.9},
        provider="test",
        latency_ms=1,
    )
    result = verifier.verify(state, decision, [])
    assert result.verified is False


def test_templates_only_use_verified_numbers() -> None:
    from matchos.schemas import VerifiedMoment

    moment = VerifiedMoment(
        id="moment-1",
        type="TACTICAL_SHIFT",
        team_id="BLUE",
        frame="PRESS_BREAKDOWN",
        importance=0.9,
        confidence=0.94,
        facts={
            "defendingTeam": "Blue",
            "attackingTeam": "Red",
            "pressBypassBefore": 0.29,
            "pressBypassNow": 0.71,
            "pressBypassSample": 8,
            "defensiveLineBeforeM": 45.0,
            "defensiveLineNowM": 38.2,
            "defensiveLineDeltaM": -6.8,
            "opponentFinalThirdEntries": 7,
            "finalThirdEntriesBefore": 2,
        },
        evidence=["event-1"],
        evidence_detail=[],
        valid_from_ms=0,
        expires_at_ms=40_000,
    )
    for persona in ("casual", "analyst", "fan", "player", "broadcaster"):
        for language in ("en", "hi"):
            for rich in (False, True):
                overlay = render_overlay(
                    moment,
                    ViewerProfile(persona=persona, language=language, team_id="BLUE", player_id="red_7"),
                    rich=rich,
                    provider="test",
                )
                assert prose_is_faithful(f"{overlay.headline} {overlay.body}", moment)


def test_workflow_stays_quiet_without_a_trigger() -> None:
    workflow, _services = build_workflow()
    sim = MatchSimulator(seed=7, scenario_id="balanced")
    tick = sim.step(500)

    async def run():
        result = await workflow.run(LiveTick(tick=tick, viewer=ViewerProfile()))
        return [item for item in result.get_outputs() if getattr(item, "kind", None) == "moment"]

    assert asyncio.run(run()) == []


def test_scripted_stories_emit_real_events() -> None:
    comeback = MatchSimulator(seed=47129, scenario_id="late_comeback")
    comeback.collect_warmup()
    scripted: list[str] = []
    while comeback.clock_ms < 89 * 60_000:
        tick = comeback.step(5_000)
        scripted.extend(
            event.player_id or ""
            for event in tick.events
            if event.type == "SHOT" and event.outcome == "GOAL" and event.qualifiers.get("scripted")
        )
    # Both scripted chances land. Open play can add a goal for either side.
    assert scripted == ["red_9", "red_7"]
    assert comeback.score_red >= 2
    assert comeback.score_blue >= 2

    impact = MatchSimulator(seed=11, scenario_id="substitute_impact")
    impact.collect_warmup()
    saw_sub = False
    while impact.clock_ms < 66 * 60_000:
        tick = impact.step(2_000)
        saw_sub = saw_sub or any(event.type == "SUBSTITUTION" and event.player_id == "red_17" for event in tick.events)
    assert saw_sub and impact.players["red_17"].on_pitch

    chaos = MatchSimulator(seed=5, scenario_id="red_card")
    chaos.collect_warmup()
    saw_card = False
    while chaos.clock_ms < 37 * 60_000:
        tick = chaos.step(2_000)
        saw_card = saw_card or any(event.type == "CARD" and event.qualifiers.get("card") == "red" for event in tick.events)
    assert saw_card and chaos.players["blue_8"].on_pitch is False


def test_all_lab_scenarios_keep_tracking_coherent() -> None:
    """Every Lab scenario should use the same physically coherent tracking model."""
    for scenario_id, seed in [
        ("balanced", 7),
        ("press_collapse", 47129),
        ("substitute_impact", 11),
        ("late_comeback", 47129),
        ("red_card", 5),
        ("random", 19),
    ]:
        sim = MatchSimulator(seed=seed, scenario_id=scenario_id)
        sim.collect_warmup()
        previous = {
            pid: (player.x, player.y)
            for pid, player in sim.players.items()
            if player.on_pitch
        }

        # Two simulated minutes is enough to cross the nearby scripted transition
        # for each Lab while keeping the smoke test cheap.
        for _ in range(120):
            sim.step(1_000)
            assert 0.0 <= sim.ball.x <= 105.0
            assert 0.0 <= sim.ball.y <= 68.0

            for pid, player in sim.players.items():
                if not player.on_pitch:
                    previous.pop(pid, None)
                    continue

                assert 1.0 <= player.x <= 104.0
                assert 1.5 <= player.y <= 66.5
                assert math.hypot(player.vx, player.vy) <= 9.0

                # At one-second sampling a player should not appear to teleport.
                if pid in previous:
                    ox, oy = previous[pid]
                    assert math.hypot(player.x - ox, player.y - oy) < 9.0
                previous[pid] = (player.x, player.y)


def test_trigger_requires_sample_size() -> None:
    engine = TriggerEngine()
    sim = MatchSimulator(seed=1, scenario_id="balanced")
    tick = sim.step(100)
    fired = engine.evaluate(tick, {"blue_press_bypass_delta": 0.4, "blue_line_delta_m": -6.0}, {"blue": 2, "blue_rate": 0.71})
    assert fired.fired is False
    fired = engine.evaluate(tick, {"blue_press_bypass_delta": 0.4, "blue_line_delta_m": -6.0}, {"blue": 6, "blue_rate": 0.71})
    assert fired.fired is True


def test_press_collapse_is_detected_from_the_feed() -> None:
    sim = MatchSimulator(seed=47129, scenario_id="press_collapse")
    engine = StateEngine()
    for tick in sim.collect_warmup():
        engine.ingest(tick)
    found = None
    while sim.clock_ms < 63 * 60_000 and found is None:
        tick = sim.step(5_000)
        state = engine.ingest(tick)
        delta = state.deltas.get("blue_press_bypass_delta")
        line = state.deltas.get("blue_line_delta_m")
        if (
            delta is not None
            and delta >= 0.25
            and state.blue.press_bypass_sample >= 5
            and (state.blue.press_bypass_rate or 0) >= 0.6
            and line is not None
            and line <= -5
        ):
            found = state
    assert found is not None, _debug_state(engine)
    assert found.blue.press_bypass_rate is not None
    assert found.blue.press_bypass_prev is not None
    assert found.blue.press_bypass_rate - found.blue.press_bypass_prev >= 0.25


def test_workflow_airs_press_breakdown() -> None:
    from matchos.producer import Producer
    from matchos.workflow import new_services

    sim = MatchSimulator(seed=47129, scenario_id="press_collapse")
    services = new_services()
    # Space producer rules on the match clock so a goal does not swallow the rest of a fast replay.
    services.producer = Producer(clock=lambda: sim.clock_ms / 1000.0)
    workflow, services = build_workflow(services)
    for tick in sim.collect_warmup():
        services.state_engine.ingest(tick)
    viewer = ViewerProfile(persona="casual", team_id="BLUE", language="en")

    async def run():
        found = None
        while sim.clock_ms < 63 * 60_000 and found is None:
            tick = sim.step(5_000)
            result = await workflow.run(LiveTick(tick=tick, viewer=viewer))
            outputs = list(result.get_outputs())
            moment = next((item for item in outputs if getattr(item, "kind", None) == "moment" and item.moment and item.moment.frame == "PRESS_BREAKDOWN"), None)
            if moment is not None:
                enrichment = next((item for item in outputs if getattr(item, "kind", None) == "enrichment"), None)
                found = (moment, enrichment)
        return found

    found = asyncio.run(run())
    assert found, "expected a verified press-breakdown overlay"
    moment_result, enrichment = found
    moment = moment_result.moment
    assert moment.facts["pressBypassNow"] >= 0.6
    assert moment.facts["pressBypassNow"] - moment.facts["pressBypassBefore"] >= 0.25
    assert moment.facts["defensiveLineDeltaM"] <= -5
    assert len(moment.evidence) >= 2
    assert moment_result.overlay.source == "template"
    assert prose_is_faithful(moment_result.overlay.body, moment)
    assert enrichment is not None
    assert enrichment.overlay.source == "agent"
    assert prose_is_faithful(enrichment.enrichment_text, moment)


def test_pass_planner_uses_current_player_positions() -> None:
    """A pass target must be feasible in the visible world, not only in formation shape."""
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    sim.carrier_id = "red_8"
    sim.possession = "RED"
    red_8 = sim.players["red_8"]
    red_8.x, red_8.y = 42.0, 30.0
    sim.ball = Point(x=42.0, y=30.0)

    # Make red 9 the only realistically reachable teammate. Other players keep
    # their roles/formation slots but are physically far from the carrier.
    for player in sim.players.values():
        if player.team_id == "RED" and player.player_id != "red_8":
            player.x, player.y = 94.0, 60.0
            player.vx = player.vy = 0.0
    red_9 = sim.players["red_9"]
    red_9.x, red_9.y = 56.0, 30.0

    target = sim._select_target(red_8, long_ball=False)
    assert target is not None
    assert target.player_id == "red_9"

    receive = sim._receive_point(target, "RED")
    assert math.hypot(receive.x - red_9.x, receive.y - red_9.y) < 6.0

    red_9.x, red_9.y = 94.0, 60.0
    assert sim._select_target(red_8, long_ball=False) is None


def test_players_hold_shape_inside_a_soft_tactical_zone() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    player = sim.players["red_6"]
    anchor_x, anchor_y = sim._target(player)
    player.x, player.y = anchor_x + 1.0, anchor_y
    player.vx = player.vy = 0.0
    before = (player.x, player.y)

    sim._move_players(0.05)

    assert math.hypot(player.x - before[0], player.y - before[1]) < 0.15


def test_interceptor_must_be_near_the_actual_pass_lane() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    start = Point(x=35.0, y=34.0)
    end = Point(x=70.0, y=34.0)

    # Put every blue outfield player far from the lane except blue 6.
    for player in sim.players.values():
        if player.team_id == "BLUE" and player.role != "GK":
            player.x, player.y = 80.0, 60.0
    blue_6 = sim.players["blue_6"]
    blue_6.x, blue_6.y = 55.0, 35.0

    challenge = sim._named_interceptor(start, end, "RED")
    assert challenge is not None
    point, player_id = challenge
    assert player_id == "blue_6"
    assert math.hypot(blue_6.x - point.x, blue_6.y - point.y) < 8.0


def test_kickoff_resets_ball_at_centre_without_a_fake_flight() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    sim.possession = "BLUE"
    sim.carrier_id = None
    sim.ball = Point(x=105.0, y=34.0)

    sim._kickoff()

    assert sim.flight is None
    assert sim.carrier_id is not None
    assert abs(sim.ball.x - 52.5) < 0.01
    assert abs(sim.ball.y - 34.0) < 0.01


def test_movement_does_not_teleport() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    prev_ball = (sim.ball.x, sim.ball.y)
    prev_players = {pid: (player.x, player.y) for pid, player in sim.players.items() if player.on_pitch}
    worst_ball = 0.0
    worst_player = 0.0
    for _ in range(100):
        sim._advance(50)
        worst_ball = max(worst_ball, math.hypot(sim.ball.x - prev_ball[0], sim.ball.y - prev_ball[1]))
        prev_ball = (sim.ball.x, sim.ball.y)
        for pid, player in sim.players.items():
            if not player.on_pitch or pid not in prev_players:
                prev_players[pid] = (player.x, player.y)
                continue
            ox, oy = prev_players[pid]
            worst_player = max(worst_player, math.hypot(player.x - ox, player.y - oy))
            prev_players[pid] = (player.x, player.y)
    assert worst_player < 0.9, worst_player
    assert worst_ball < 1.5, worst_ball


def _debug_state(engine: StateEngine) -> str:
    state = engine.state
    return (
        f"minute={state.minute} bypass={state.blue.press_bypass_rate} "
        f"prev={state.blue.press_bypass_prev} n={state.blue.press_bypass_sample} "
        f"line={state.blue.defensive_line_m}/{state.blue.defensive_line_prev_m} "
        f"deltas={state.deltas}"
    )
