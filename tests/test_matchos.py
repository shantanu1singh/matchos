"""Intelligence and workflow tests that do not need a browser or a model key."""

from __future__ import annotations

import asyncio
import math

from matchos.copy import prose_is_faithful, render_overlay
from matchos.pitch import Point
from matchos.schemas import (
    MatchEvent,
    MatchState,
    MomentDecision,
    PlayerRollup,
    VerifiedMoment,
    ViewerProfile,
)
from matchos.simulator import MatchSimulator
from matchos.state_engine import StateEngine
from matchos.trigger_engine import TriggerEngine
from matchos.verifier import Verifier
from matchos.workflow import LiveTick, build_shared_workflow


def _pass(
    sequence: int, timestamp: int, start_x: float, end_x: float, line: float, outcome: str = "COMPLETE"
) -> MatchEvent:
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
        return [
            (event.type, event.team_id, event.timestamp_ms, event.outcome)
            for tick in ticks
            for event in tick.events
        ]

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
    workflow, _services = build_shared_workflow()
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
        saw_sub = saw_sub or any(
            event.type == "SUBSTITUTION" and event.player_id == "red_17" for event in tick.events
        )
    assert saw_sub and impact.players["red_17"].on_pitch

    chaos = MatchSimulator(seed=5, scenario_id="red_card")
    chaos.collect_warmup()
    saw_card = False
    while chaos.clock_ms < 37 * 60_000:
        tick = chaos.step(2_000)
        saw_card = saw_card or any(
            event.type == "CARD" and event.qualifiers.get("card") == "red" for event in tick.events
        )
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
        previous = {pid: (player.x, player.y) for pid, player in sim.players.items() if player.on_pitch}

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


def test_player_decision_uses_the_selected_player() -> None:
    from matchos.decision_client import LocalDecisionClient

    state = MatchState(
        match_id="m-player-focus",
        timestamp_ms=30 * 60_000,
        period=1,
        minute=30,
        second=0,
        score_red=0,
        score_blue=0,
        possession_team="BLUE",
        players={
            "blue_9": PlayerRollup(
                team_id="BLUE",
                number=9,
                between_lines_last8=5,
                between_lines_prev30=2,
            )
        },
    )

    decision = asyncio.run(LocalDecisionClient().decide(state, ["player_involvement"], "blue_9"))
    assert decision.primary_frame == "PLAYER_IMPACT"
    assert decision.player_id == "blue_9"
    assert decision.team_id == "BLUE"


def test_selected_player_can_fire_the_player_trigger() -> None:
    engine = TriggerEngine()
    sim = MatchSimulator(seed=1, scenario_id="balanced")
    tick = sim.step(100)
    fired = engine.evaluate(
        tick,
        {"focus_player_involvement_delta": 1.0},
        {"focus_between": 4},
    )
    assert fired.fired is True
    assert "player_involvement" in fired.reasons


def test_trigger_requires_sample_size() -> None:
    engine = TriggerEngine()
    sim = MatchSimulator(seed=1, scenario_id="balanced")
    tick = sim.step(100)
    fired = engine.evaluate(
        tick, {"blue_press_bypass_delta": 0.4, "blue_line_delta_m": -6.0}, {"blue": 2, "blue_rate": 0.71}
    )
    assert fired.fired is False
    fired = engine.evaluate(
        tick, {"blue_press_bypass_delta": 0.4, "blue_line_delta_m": -6.0}, {"blue": 6, "blue_rate": 0.71}
    )
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
    from matchos.demo import review_checkpoint
    from matchos.perspective import Perspective
    from matchos.session import MatchSession

    async def run():
        session = MatchSession("press_collapse", 47129, 1, ViewerProfile(persona="analyst"), running=False)
        messages = []

        async def send(message):
            messages.append(message)

        worker = Perspective(session, session.viewer, 0, send)
        try:
            await session.seek_to(64 * 60_000)
            batch = await review_checkpoint(session)
            await worker.process(batch)
            result = messages[0]["result"]
            assert result["producer"]["action"] == "AIR"
            assert result["moment"]["frame"] == "PRESS_BREAKDOWN"
            moment = VerifiedMoment.model_validate(result["moment"])
            assert all(check.passed for check in moment.checks)
            assert moment.facts["pressBypassNow"] - moment.facts["pressBypassBefore"] >= 0.25
            assert (
                moment.facts["defensiveLineDeltaM"] <= -5
                or moment.facts["opponentFinalThirdEntries"] - moment.facts["finalThirdEntriesBefore"] >= 5
            )
            assert len(moment.evidence) >= 2
            assert prose_is_faithful(result["overlay"]["body"], moment)
        finally:
            await worker.close()
            await session.close()

    asyncio.run(run())


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


def test_foul_creates_a_free_kick_and_can_show_a_yellow_card() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    carrier = sim.players["red_8"]
    carrier.x, carrier.y = 52.0, 34.0
    sim.ball = Point(x=52.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    sim.players["blue_6"].x, sim.players["blue_6"].y = 53.0, 34.0

    # Force the optional card branch with a defender in tackling range.
    sim.rng.random = lambda: 0.0  # type: ignore[method-assign]
    events = sim._foul(carrier)
    types = [event.type for event in events]

    assert "FOUL" in types
    assert "FREE_KICK" in types
    card = next(event for event in events if event.type == "CARD")
    assert card.qualifiers.get("card") == "yellow"
    assert sim.possession == "RED"


def test_foul_requires_a_nearby_defender() -> None:
    sim = MatchSimulator(seed=3, scenario_id="balanced")
    carrier = sim.players["red_8"]
    carrier.x, carrier.y = 52.0, 34.0
    sim.ball = Point(x=52.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"
    for player in sim.players.values():
        if player.team_id == "BLUE":
            player.x, player.y = 90.0, 60.0

    events = sim._foul(carrier)

    assert not any(event.type == "FOUL" for event in events)


def test_foul_in_the_box_creates_a_penalty_then_a_penalty_shot() -> None:
    sim = MatchSimulator(seed=4, scenario_id="balanced")
    carrier = sim.players["red_9"]
    carrier.x, carrier.y = 94.0, 34.0
    carrier.vx = carrier.vy = 0.0
    sim.ball = Point(x=94.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    sim.players["blue_4"].x, sim.players["blue_4"].y = 95.0, 34.0
    sim.rng.random = lambda: 0.9  # type: ignore[method-assign]
    events = sim._foul(carrier)
    assert any(event.type == "PENALTY" for event in events)
    assert sim.pending_penalty_taker == carrier.player_id
    assert abs(sim.ball.x - 94.0) < 0.01
    assert abs(sim.ball.y - 34.0) < 0.01

    shot = sim._take_penalty()
    assert len(shot) == 1
    assert shot[0].type == "SHOT"
    assert shot[0].qualifiers.get("penalty") is True


def test_forward_pass_can_generate_offside_and_free_kick_events() -> None:
    sim = MatchSimulator(seed=5, scenario_id="balanced")
    carrier = sim.players["red_8"]
    carrier.x, carrier.y = 60.0, 34.0
    carrier.vx = carrier.vy = 0.0
    sim.ball = Point(x=60.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    for player in sim.players.values():
        if player.team_id == "RED" and player.player_id not in {"red_8", "red_9"}:
            player.x, player.y = 15.0, 60.0
            player.vx = player.vy = 0.0
        elif player.team_id == "BLUE" and player.role != "GK":
            player.x = 75.0
            player.vx = player.vy = 0.0
    target = sim.players["red_9"]
    target.x, target.y = 90.0, 34.0
    target.vx = target.vy = 0.0

    sim.rng.random = lambda: 0.0  # type: ignore[method-assign]
    events = sim._pass(carrier, long_ball=False)

    assert any(event.type == "OFFSIDE" for event in events)
    assert any(event.type == "FREE_KICK" and event.qualifiers.get("reason") == "OFFSIDE" for event in events)


def test_pass_out_creates_throw_in_and_restarts_with_opponent() -> None:
    sim = MatchSimulator(seed=6, scenario_id="balanced")
    carrier = sim.players["red_8"]
    target = sim.players["red_7"]
    carrier.x, carrier.y = 42.0, 34.0
    target.x, target.y = 50.0, 10.0
    sim.ball = Point(x=42.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    sim._select_target = lambda _carrier, _long_ball: target  # type: ignore[method-assign]
    sim._pass_success = lambda *_args, **_kwargs: 0.0  # type: ignore[method-assign]
    # An onside target, failed pass and deterministic OUT branch, independent of RNG draw count.
    sim.rng.random = lambda: 0.0  # type: ignore[method-assign]

    events = sim._pass(carrier, long_ball=False)
    throw_in = next(event for event in events if event.type == "THROW_IN")
    assert throw_in.team_id == "BLUE"
    assert sim.pending_restart is not None
    assert sim.pending_restart[0] == "THROW_IN"

    while sim.flight is not None:
        sim._advance(50)

    assert sim.possession == "BLUE"
    assert sim.carrier_id is not None
    assert sim.ball.y in {0.4, 67.6}


def test_saved_wide_shot_can_create_corner_restart() -> None:
    sim = MatchSimulator(seed=8, scenario_id="balanced")
    carrier = sim.players["red_9"]
    carrier.x, carrier.y = 92.0, 34.0
    sim.ball = Point(x=92.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    sim.rng.uniform = lambda _a, _b: 3.5  # type: ignore[method-assign]
    sim.rng.random = lambda: 0.2  # type: ignore[method-assign]

    events = sim._shot(carrier)
    corner = next(event for event in events if event.type == "CORNER")
    assert corner.team_id == "RED"
    assert sim.pending_restart is not None
    assert sim.pending_restart[0] == "CORNER"

    while sim.flight is not None:
        sim._advance(50)

    assert sim.possession == "RED"
    assert sim.ball.x > 104.0
    assert sim.ball.y in {0.4, 67.6}


def test_off_target_shot_creates_goal_kick_restart() -> None:
    sim = MatchSimulator(seed=9, scenario_id="balanced")
    carrier = sim.players["red_9"]
    carrier.x, carrier.y = 82.0, 34.0
    sim.ball = Point(x=82.0, y=34.0)
    sim.carrier_id = carrier.player_id
    sim.possession = "RED"

    sim.rng.uniform = lambda _a, _b: 0.0  # type: ignore[method-assign]
    sim.rng.random = lambda: 0.99  # type: ignore[method-assign]

    events = sim._shot(carrier)
    goal_kick = next(event for event in events if event.type == "GOAL_KICK")
    assert goal_kick.team_id == "BLUE"
    assert sim.pending_restart is not None
    assert sim.pending_restart[0] == "GOAL_KICK"

    while sim.flight is not None:
        sim._advance(50)

    assert sim.possession == "BLUE"
    assert abs(sim.ball.x - 99.5) < 0.01
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
