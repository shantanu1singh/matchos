"""Seeded football decisions and scripted scenario events."""

from __future__ import annotations

import math

from matchos.pitch import (
    attack_x,
    clamp,
    distance,
    first_line_x,
    in_box,
    in_final_third,
    other,
)
from matchos.scenarios import progression_bias
from matchos.schemas import (
    MatchEvent,
    Point,
    TeamId,
)
from matchos.simulation.movement import MovementSystem
from matchos.simulation.restarts import RestartSystem
from matchos.simulation.world import Flight, MatchWorld, SimPlayer
from matchos.teams import shirt_number


class EventGenerator:
    def __init__(self, world: MatchWorld, movement: MovementSystem, restarts: RestartSystem) -> None:
        self.world = world
        self.movement = movement
        self.restarts = restarts

    def generate(self) -> list[MatchEvent]:
        if self.world.carrier_id is None or not self.world.players[self.world.carrier_id].on_pitch:
            self.world.carrier_id = self.world.nearest_actual(self.world.possession, self.world.ball)
        if self.world.carrier_id is None:
            return []
        self.world.focus.clear()
        carrier = self.world.players[self.world.carrier_id]
        events: list[MatchEvent] = []
        if self.world.pressers(carrier) >= 2 and self.world.clock_ms - self.world.last_pressure_ms > 2500:
            events.append(
                self.world.emit(
                    "PRESSURE",
                    other(carrier.team_id),
                    self.world.nearest_actual(other(carrier.team_id), Point(x=carrier.x, y=carrier.y)),
                    None,
                    "APPLIED",
                )
            )
            self.world.last_pressure_ms = self.world.clock_ms
        kind = self._choose_action(carrier)
        if kind == "SHOT":
            events.extend(self._shot(carrier))
        elif kind == "FOUL":
            events.extend(self._foul(carrier))
        elif kind == "CARRY":
            events.extend(self._carry(carrier))
        elif kind == "DUEL":
            events.extend(self._duel(carrier))
        elif kind == "LONG_BALL":
            events.extend(self._pass(carrier, long_ball=True))
        else:
            events.extend(self._pass(carrier, long_ball=False))
        tempo = self.world.style(self.world.possession)["tempo"]
        gap = int(self.world.rng.randint(700, 1600) * (1.25 - tempo * 0.45))
        self.world.next_action_ms = self.world.clock_ms + gap
        return events

    def _choose_action(self, carrier: SimPlayer) -> str:
        ax = attack_x(carrier.team_id, carrier.x)
        style = self.world.style(carrier.team_id)
        weights = {
            "PASS": 0.60,
            "CARRY": 0.16 + carrier.dribbling * 0.08,
            "LONG_BALL": 0.06 + style["directness"] * 0.08,
            "DUEL": 0.05,
            "FOUL": (0.025 + min(0.035, self.world.pressers(carrier) * 0.012))
            if self.world.scenario_id in {"balanced", "random"}
            else 0.0,
            "SHOT": 0.02,
        }
        if ax > 0.72:
            weights["SHOT"] = 0.16 + carrier.shooting * 0.12
            weights["PASS"] = 0.48
        elif ax < 0.30:
            weights["LONG_BALL"] += 0.1
            weights["SHOT"] = 0.0
        if self.world.scenario_id == "substitute_impact" and carrier.player_id == "red_17":
            weights["CARRY"] += 0.22
            weights["SHOT"] += 0.05
        return self._weighted(weights)

    def _weighted(self, weights: dict[str, float]) -> str:
        total = sum(max(0.0, v) for v in weights.values())
        roll = self.world.rng.random() * total
        upto = 0.0
        for key, value in weights.items():
            upto += max(0.0, value)
            if roll <= upto:
                return key
        return "PASS"

    def _pass(self, carrier: SimPlayer, long_ball: bool) -> list[MatchEvent]:
        target = self._select_target(carrier, long_ball)
        if target is None:
            return self._carry(carrier)
        start = Point(x=self.world.ball.x, y=self.world.ball.y)
        end = self._receive_point(target, carrier.team_id)
        length = distance(start, end)
        pressure = self.world.pressure_modifier(carrier)
        qual = self.world.geometry(carrier.team_id)
        success_p = self._pass_success(carrier, target, length, pressure, qual, long_ball, end)

        # Approximate the offside line from the visible defensive block. This
        # intentionally fires only on clearly advanced, forward targets.
        defending_line = float(qual["defending_line_x"])
        beyond_line = (carrier.team_id == "RED" and target.x > defending_line + 1.2) or (
            carrier.team_id == "BLUE" and target.x < defending_line - 1.2
        )
        forward_pass = attack_x(carrier.team_id, end.x) > attack_x(carrier.team_id, start.x) + 0.05
        offside = (
            self.world.scenario_id in {"balanced", "random"}
            and beyond_line
            and forward_pass
            and attack_x(carrier.team_id, target.x) > 0.52
            and self.world.rng.random() < 0.32
        )
        if offside:
            defending = other(carrier.team_id)
            whistle = Point(x=target.x, y=target.y)
            receiver = self.world.nearest_actual(defending, whistle)
            pace = 12.0 if not long_ball else 14.0
            flight = self.movement.realize(
                receiver, start, whistle, "PASS", carrier.team_id, False, pace, max_s=2.4
            )
            whistle = flight.end
            self.world.next_action_ms = self.world.clock_ms + flight.duration_ms + 900
            pass_event = self.world.emit(
                "PASS",
                carrier.team_id,
                carrier.player_id,
                target.player_id,
                "OFFSIDE",
                start,
                whistle,
                {
                    **qual,
                    "length_m": round(length, 1),
                    "long_ball": long_ball,
                    "pressure": round(pressure, 2),
                },
            )
            offside_event = self.world.emit(
                "OFFSIDE",
                carrier.team_id,
                target.player_id,
                carrier.player_id,
                "CALLED",
                whistle,
                whistle,
                {"related": pass_event.id},
            )
            restart = self.world.emit(
                "FREE_KICK",
                defending,
                receiver,
                None,
                "AWARDED",
                whistle,
                whistle,
                {"reason": "OFFSIDE", "related": offside_event.id},
            )
            return [pass_event, offside_event, restart]

        success = self.world.rng.random() < success_p
        outcome = "COMPLETE" if success else ("OUT" if self.world.rng.random() < 0.25 else "INCOMPLETE")
        runner_id = target.player_id
        desired = end
        challenge = self._named_interceptor(start, end, carrier.team_id) if outcome == "INCOMPLETE" else None
        if challenge is not None:
            desired, runner_id = challenge
        elif outcome == "INCOMPLETE":
            # A failed pass cannot still be animated as the intended teammate
            # cleanly receiving it. Give the loose ball to the closest visible
            # opponent and let the motion planner choose a reachable meeting point.
            runner_id = self.world.nearest_actual(other(carrier.team_id), desired)
        pace = 12.0 if not long_ball else 14.0
        restart_event: MatchEvent | None = None
        if outcome == "OUT":
            desired = Point(x=clamp(end.x, 0.4, 104.6), y=0.4 if end.y < 34 else 67.6)
            restart_team = other(carrier.team_id)
            runner_id = self.world.nearest_actual(restart_team, desired)
            flight = self.movement.realize(
                runner_id, start, desired, "PASS", carrier.team_id, False, pace, pull=False, max_s=3.2
            )
            if self.world.scenario_id in {"balanced", "random"}:
                self.restarts.schedule("THROW_IN", restart_team, flight.end, runner_id)
        else:
            flight = self.movement.realize(runner_id, start, desired, "PASS", carrier.team_id, success, pace)
        shown_end = flight.end
        event = self.world.emit(
            "PASS",
            carrier.team_id,
            carrier.player_id,
            target.player_id if success or outcome == "INCOMPLETE" else None,
            outcome,
            start,
            shown_end,
            {
                **qual,
                "length_m": round(length, 1),
                "long_ball": long_ball,
                "pressure": round(pressure, 2),
                "entered_final_third": (not in_final_third(carrier.team_id, start.x))
                and in_final_third(carrier.team_id, shown_end.x)
                and success,
            },
        )
        events = [event]
        if outcome == "OUT":
            restart_event = self.world.emit(
                "THROW_IN",
                other(carrier.team_id),
                runner_id,
                carrier.player_id,
                "AWARDED",
                shown_end,
                shown_end,
                {"related": event.id},
            )
            events.append(restart_event)
        caught = outcome == "INCOMPLETE" and self.world.rng.random() < 0.7 and challenge is not None
        if caught:
            events.append(
                self.world.emit(
                    "INTERCEPTION",
                    other(carrier.team_id),
                    runner_id,
                    None,
                    "WON",
                    shown_end,
                    shown_end,
                    {"related": event.id},
                )
            )
        return events

    def _named_interceptor(self, start: Point, end: Point, attacking: TeamId) -> tuple[Point, str] | None:
        """The defender whose shape stands on this pass. None if the lane is clear."""
        dx, dy = end.x - start.x, end.y - start.y
        length2 = dx * dx + dy * dy
        if length2 < 36:
            return None
        best: tuple[float, str, Point] | None = None
        for player in self.world.on_pitch(other(attacking)):
            if player.role == "GK":
                continue
            # Interceptions belong to defenders who are actually near the lane.
            px, py = player.x, player.y
            t = ((px - start.x) * dx + (py - start.y) * dy) / length2
            t = clamp(t, 0.28, 0.82)
            point = Point(x=start.x + dx * t, y=start.y + dy * t)
            dist = math.hypot(px - point.x, py - point.y)
            if best is None or dist < best[0]:
                best = (dist, player.player_id, point)
        if best is None or best[0] > 8.0:
            return None
        return best[2], best[1]

    def _select_target(self, carrier: SimPlayer, long_ball: bool) -> SimPlayer | None:
        mates = [
            p
            for p in self.world.on_pitch(carrier.team_id)
            if p.player_id != carrier.player_id and p.role != "GK"
        ]
        if long_ball:
            mates = [p for p in mates if p.role in {"ST", "RW", "LW", "LCM", "RCM"}] or mates
        if not mates:
            return None
        bias = progression_bias(self.world.scenario_id, self.world.minute(), carrier.team_id)
        defending = other(carrier.team_id)
        line = first_line_x(self.world.tracking(), defending)
        origin = Point(x=carrier.x, y=carrier.y)
        scored: list[tuple[float, SimPlayer]] = []
        for mate in mates:
            spot = Point(x=mate.x, y=mate.y)
            forward = attack_x(carrier.team_id, spot.x) - attack_x(carrier.team_id, origin.x)
            length = math.hypot(spot.x - origin.x, spot.y - origin.y)
            if length < 4 or length > (48 if long_ball else 36):
                continue
            score = 0.25 + max(0.0, forward) * 1.4
            score -= abs(length - (28 if long_ball else 16)) * 0.03
            score -= self.world.nearby_opponents(spot, carrier.team_id, 6.0) * 0.18
            # Keep tactical role as a weak prior, while current physical position
            # determines whether the pass is actually feasible.
            slot = self.world.slot(mate)
            score -= min(0.18, math.hypot(slot.x - spot.x, slot.y - spot.y) * 0.008)
            breaks = (spot.x > line + 3 and carrier.team_id == "RED") or (
                spot.x < line - 3 and carrier.team_id == "BLUE"
            )
            if breaks:
                score += bias
            if (
                self.world.scenario_id == "press_collapse"
                and mate.player_id == "red_7"
                and self.world.minute() >= 57
            ):
                score += 0.45
            if (
                self.world.scenario_id == "substitute_impact"
                and mate.player_id == "red_17"
                and self.world.minute() >= 65
            ):
                score += 0.55
            scored.append((score, mate))
        if not scored:
            # No pass is physically plausible from the current world state.
            # Let _pass fall back to a carry rather than dragging a remote
            # teammate across the pitch to satisfy an invented event.
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:3]
        weights = [max(0.05, item[0]) for item in top]
        return top[self._pick(weights)][1]

    def _pick(self, weights: list[float]) -> int:
        total = sum(weights)
        roll = self.world.rng.random() * total
        upto = 0.0
        for i, weight in enumerate(weights):
            upto += weight
            if roll <= upto:
                return i
        return len(weights) - 1

    def _receive_point(self, target: SimPlayer, team: TeamId) -> Point:
        """Lead the receiver from their current position, not an abstract formation slot."""
        lead_s = 0.55
        forward = 2.8 if team == "RED" else -2.8
        return Point(
            x=clamp(target.x + target.vx * lead_s + forward + self.world.rng.uniform(-0.8, 0.8), 2.0, 103.0),
            y=clamp(target.y + target.vy * lead_s + self.world.rng.uniform(-1.0, 1.0), 3.0, 65.0),
        )

    def _pass_success(
        self,
        carrier: SimPlayer,
        target: SimPlayer,
        length: float,
        pressure: float,
        qual: dict[str, object],
        long_ball: bool,
        end: Point,
    ) -> float:
        distance_mod = 1.05 - min(0.55, length / 70.0)
        angle_mod = 0.85 + 0.15 * (1.0 - min(1.0, abs(carrier.y - target.y) / 40.0))
        availability = 1.0 - min(
            0.45, self.world.nearby_opponents(Point(x=target.x, y=target.y), carrier.team_id, 5.5) * 0.15
        )
        pressure_mod = 1.0 - 0.45 * pressure
        p = carrier.passing * distance_mod * angle_mod * availability * pressure_mod
        if long_ball:
            p *= 0.82
        line = float(qual["defending_first_line_x"])
        would_break = (carrier.team_id == "RED" and end.x > line + 4) or (
            carrier.team_id == "BLUE" and end.x < line - 4
        )
        minute = self.world.minute()
        if self.world.scenario_id == "press_collapse" and carrier.team_id == "RED" and would_break:
            # Through-balls stay difficult until Blue's block has had time to drop.
            if minute < 57:
                p = min(p, 0.32)
            else:
                t = min(1.0, (minute - 57.0) / 1.2)
                p = max(p, 0.74 + 0.16 * t)
        if self.world.scenario_id == "red_card" and carrier.team_id == "RED" and minute >= 36 and would_break:
            p = max(p, 0.72)
        return clamp(p, 0.08, 0.93)

    def _carry(self, carrier: SimPlayer) -> list[MatchEvent]:
        direction = 1.0 if carrier.team_id == "RED" else -1.0
        burst = 6.0 + carrier.dribbling * 10.0
        if carrier.player_id == "red_17":
            burst += 4.0
        end = Point(
            x=clamp(carrier.x + direction * burst, 1.5, 103.5),
            y=clamp(carrier.y + self.world.rng.uniform(-3.0, 3.0), 2.0, 66.0),
        )
        start = Point(x=carrier.x, y=carrier.y)
        pressure = self.world.pressure_modifier(carrier)
        success = self.world.rng.random() < clamp(0.45 + carrier.dribbling * 0.4 - pressure * 0.35, 0.15, 0.9)
        outcome = "COMPLETE" if success else "TACKLED"
        event = self.world.emit(
            "CARRY",
            carrier.team_id,
            carrier.player_id,
            None,
            outcome,
            start,
            end if success else Point(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2),
            {
                **self.world.geometry(carrier.team_id),
                "entered_final_third": success
                and (not in_final_third(carrier.team_id, start.x))
                and in_final_third(carrier.team_id, end.x),
                "box_entry": success
                and in_box(carrier.team_id, end.x)
                and not in_box(carrier.team_id, start.x),
            },
        )
        events = [event]
        end_point = end if success else Point(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2)
        if success:
            length = distance(Point(x=carrier.x, y=carrier.y), end_point)
            duration = int(max(800, min(2800, length / 4.4 * 1000)))
            self.world.carry_target = end_point
            self.world.carry_until_ms = self.world.clock_ms + duration
            self.world.carrier_id = carrier.player_id
        else:
            tackler = self.world.nearest_actual(other(carrier.team_id), end_point)
            events.append(
                self.world.emit(
                    "DUEL", other(carrier.team_id), tackler, carrier.player_id, "WON", end_point, end_point
                )
            )
            self.world.carry_target = None
            self.world.carry_until_ms = 0
            self.movement.realize(
                tackler,
                Point(x=self.world.ball.x, y=self.world.ball.y),
                end_point,
                "PASS",
                other(carrier.team_id),
                False,
                9.0,
            )
        return events

    def _foul(self, carrier: SimPlayer) -> list[MatchEvent]:
        """Create a stoppage committed by the nearest defender."""
        spot = Point(x=carrier.x, y=carrier.y)
        defending = other(carrier.team_id)
        fouler_id = self.world.nearest_actual(defending, spot)
        if fouler_id is None:
            return self._carry(carrier)
        fouler = self.world.players[fouler_id]
        if math.hypot(fouler.x - carrier.x, fouler.y - carrier.y) > 4.5:
            return self._carry(carrier)

        events = [
            self.world.emit(
                "FOUL",
                defending,
                fouler_id,
                carrier.player_id,
                "FOUL",
                spot,
                spot,
                {"fouled_player": carrier.player_id},
            )
        ]
        if self.world.rng.random() < 0.30:
            events.append(
                self.world.emit(
                    "CARD",
                    defending,
                    fouler_id,
                    None,
                    "YELLOW",
                    spot,
                    spot,
                    {"card": "yellow", "related": events[0].id},
                )
            )

        self.world.flight = None
        self.world.carry_target = None
        self.world.carry_until_ms = 0
        self.world.ball_vx = self.world.ball_vy = 0.0
        self.world.ball_z = 0.0
        self.world.possession = carrier.team_id
        self.world.carrier_id = carrier.player_id

        if in_box(carrier.team_id, carrier.x) and 24.84 <= carrier.y <= 43.16:
            penalty_spot = Point(x=94.0 if carrier.team_id == "RED" else 11.0, y=34.0)
            self.world.ball = penalty_spot
            self.world.focus.clear()
            self.world.focus[carrier.player_id] = (penalty_spot.x, penalty_spot.y)
            self.world.pending_penalty_taker = carrier.player_id
            self.world.next_action_ms = self.world.clock_ms + 1000
            events.append(
                self.world.emit(
                    "PENALTY",
                    carrier.team_id,
                    carrier.player_id,
                    fouler_id,
                    "AWARDED",
                    spot,
                    penalty_spot,
                    {"related": events[0].id},
                )
            )
            return events

        self.world.ball = spot
        taker = self.world.nearest_actual(carrier.team_id, spot)
        self.world.carrier_id = taker or carrier.player_id
        self.world.focus.clear()
        if self.world.carrier_id:
            self.world.focus[self.world.carrier_id] = (spot.x, spot.y)
        self.world.next_action_ms = self.world.clock_ms + 800
        events.append(
            self.world.emit(
                "FREE_KICK",
                carrier.team_id,
                self.world.carrier_id,
                fouler_id,
                "AWARDED",
                spot,
                spot,
                {"reason": "FOUL", "related": events[0].id},
            )
        )
        return events

    def take_penalty(self) -> list[MatchEvent]:
        taker_id = self.world.pending_penalty_taker
        self.world.pending_penalty_taker = None
        if (
            taker_id is None
            or taker_id not in self.world.players
            or not self.world.players[taker_id].on_pitch
        ):
            return []
        taker = self.world.players[taker_id]
        start = Point(x=self.world.ball.x, y=self.world.ball.y)
        goal_x = 105.0 if taker.team_id == "RED" else 0.0
        aim_y = clamp(34.0 + self.world.rng.uniform(-3.2, 3.2), 30.8, 37.2)
        end = Point(x=goal_x, y=aim_y)
        goal = self.world.rng.random() < 0.76
        saved = not goal and self.world.rng.random() < 0.72
        outcome = "GOAL" if goal else ("SAVED" if saved else "OFF_TARGET")
        event = self.world.emit(
            "SHOT",
            taker.team_id,
            taker.player_id,
            None,
            outcome,
            start,
            end,
            {"penalty": True, "xg": 0.76, "distance_m": 11.0},
        )
        shot_end = end if goal or saved else Point(x=goal_x, y=clamp(aim_y + 6.0, 1.0, 67.0))
        self.world.flight = Flight(
            start, shot_end, self.world.clock_ms, 650, "SHOT", taker.team_id, None, False
        )
        self.world.focus.clear()
        self.world.possession = other(taker.team_id)
        self.world.possession_id += 1
        if goal:
            self.world.carrier_id = None
            if taker.team_id == "RED":
                self.world.score_red += 1
            else:
                self.world.score_blue += 1
        else:
            keeper = "blue_1" if taker.team_id == "RED" else "red_1"
            self.world.carrier_id = (
                keeper
                if keeper in self.world.players and self.world.players[keeper].on_pitch
                else self.world.nearest_actual(self.world.possession, shot_end)
            )
            if self.world.carrier_id:
                keeper_x = 103.2 if taker.team_id == "RED" else 1.8
                self.world.focus[self.world.carrier_id] = (keeper_x, clamp(shot_end.y, 27.5, 40.5))
        self.world.next_action_ms = self.world.clock_ms + 1500
        return [event]

    def _duel(self, carrier: SimPlayer) -> list[MatchEvent]:
        spot = Point(x=carrier.x, y=carrier.y)
        rival = self.world.nearest_actual(other(carrier.team_id), spot)
        start = Point(x=self.world.ball.x, y=self.world.ball.y)
        won = self.world.rng.random() < 0.48 + carrier.dribbling * 0.2
        winner = carrier.player_id if won else rival
        loser = rival if won else carrier.player_id
        event = self.world.emit(
            "DUEL",
            carrier.team_id if won else other(carrier.team_id),
            winner,
            loser,
            "WON",
            start,
            start,
        )
        if not won and rival:
            self.world.possession = other(carrier.team_id)
            self.world.possession_id += 1
            self.world.carrier_id = rival
            self.world.focus[rival] = (start.x, start.y)
        return [event]

    def _shot(self, carrier: SimPlayer) -> list[MatchEvent]:
        goal_x = 105.0 if carrier.team_id == "RED" else 0.0
        aim_y = clamp(34 + self.world.rng.uniform(-6, 6), 30.5, 37.5)
        start = Point(x=self.world.ball.x, y=self.world.ball.y)
        end = Point(x=goal_x, y=aim_y)
        distance_m = math.hypot(goal_x - carrier.x, 34 - carrier.y)
        pressure = self.world.pressure_modifier(carrier)
        p_on_target = clamp(carrier.shooting * (1.15 - distance_m / 50.0) * (1 - 0.4 * pressure), 0.08, 0.8)
        on_target = self.world.rng.random() < p_on_target
        p_goal = p_on_target * (0.18 if distance_m < 14 else 0.06)
        is_goal = on_target and self.world.rng.random() < p_goal
        outcome = "GOAL" if is_goal else ("SAVED" if on_target else "OFF_TARGET")
        duration = int(max(480, min(1600, distance_m / 16 * 1000)))
        shot_end = end if on_target else Point(x=goal_x, y=clamp(aim_y + 8, 1, 67))
        event = self.world.emit(
            "SHOT",
            carrier.team_id,
            carrier.player_id,
            None,
            outcome,
            start,
            shot_end,
            {"distance_m": round(distance_m, 1), "xg": round(p_goal, 2)},
        )
        events = [event]
        self.world.flight = Flight(
            start, shot_end, self.world.clock_ms, duration, "SHOT", carrier.team_id, None, False
        )
        self.world.carrier_id = None

        restart_events = self.world.scenario_id in {"balanced", "random"}
        corner = restart_events and outcome == "SAVED" and abs(aim_y - 34.0) >= 2.4
        goal_kick = restart_events and outcome == "OFF_TARGET"
        keeper = "blue_1" if carrier.team_id == "RED" else "red_1"

        if corner:
            corner_y = 67.6 if aim_y >= 34.0 else 0.4
            corner_spot = Point(x=104.6 if carrier.team_id == "RED" else 0.4, y=corner_y)
            taker = self.world.nearest_actual(carrier.team_id, corner_spot)
            self.restarts.schedule("CORNER", carrier.team_id, corner_spot, taker)
            if keeper in self.world.players and self.world.players[keeper].on_pitch:
                keeper_x = 103.2 if carrier.team_id == "RED" else 1.8
                self.world.focus[keeper] = (keeper_x, clamp(end.y, 27.5, 40.5))
            events.append(
                self.world.emit(
                    "CORNER",
                    carrier.team_id,
                    taker,
                    keeper,
                    "AWARDED",
                    corner_spot,
                    corner_spot,
                    {"reason": "DEFLECTION", "related": event.id},
                )
            )
        elif goal_kick:
            defending = other(carrier.team_id)
            goal_kick_spot = Point(x=99.5 if carrier.team_id == "RED" else 5.5, y=34.0)
            taker = (
                keeper
                if keeper in self.world.players and self.world.players[keeper].on_pitch
                else self.world.nearest_actual(defending, goal_kick_spot)
            )
            self.restarts.schedule("GOAL_KICK", defending, goal_kick_spot, taker)
            events.append(
                self.world.emit(
                    "GOAL_KICK",
                    defending,
                    taker,
                    carrier.player_id,
                    "AWARDED",
                    goal_kick_spot,
                    goal_kick_spot,
                    {"related": event.id},
                )
            )
        elif not is_goal:
            if keeper in self.world.players and self.world.players[keeper].on_pitch:
                keeper_x = 103.2 if carrier.team_id == "RED" else 1.8
                keeper_y = clamp(shot_end.y, 27.5, 40.5)
                self.world.focus[keeper] = (keeper_x, keeper_y)

        if is_goal:
            if carrier.team_id == "RED":
                self.world.score_red += 1
            else:
                self.world.score_blue += 1
            # Restart from the centre after a short beat. The flight visually ends in the goal.
            self.world.possession = other(carrier.team_id)
            self.world.possession_id += 1
            self.world.next_action_ms = self.world.clock_ms + duration + 700
        elif not (corner or goal_kick):
            self.world.possession = other(carrier.team_id)
            self.world.possession_id += 1
            self.world.carrier_id = (
                keeper
                if keeper in self.world.players and self.world.players[keeper].on_pitch
                else self.world.nearest_actual(self.world.possession, end)
            )
            self.world.next_action_ms = self.world.clock_ms + duration + 400
        else:
            self.world.next_action_ms = self.world.clock_ms + duration + 800
        return events

    def scripted(self) -> list[MatchEvent]:
        events: list[MatchEvent] = []
        minute_ms = self.world.clock_ms
        if self.world.scenario_id == "press_collapse":
            if "lab_goal65" not in self.world.done_scripts and minute_ms >= 65 * 60_000:
                scripted = self._script_goal("red_9", "lab_goal65")
                events.extend(scripted)
                if any(event.type == "SHOT" and event.outcome == "GOAL" for event in scripted):
                    self.world.done_scripts.add("lab_goal65")
            if "lab_card68" not in self.world.done_scripts and minute_ms >= 68 * 60_000:
                events.extend(self._red_card("blue_8"))
                self.world.done_scripts.add("lab_card68")
        if (
            self.world.scenario_id == "substitute_impact"
            and "sub" not in self.world.done_scripts
            and minute_ms >= 65 * 60_000
        ):
            events.extend(self._substitute("red_11", "red_17"))
            self.world.done_scripts.add("sub")
        if (
            self.world.scenario_id == "red_card"
            and "card" not in self.world.done_scripts
            and minute_ms >= 36 * 60_000
        ):
            events.extend(self._red_card("blue_8"))
            self.world.done_scripts.add("card")
        if (
            self.world.scenario_id == "late_comeback"
            and "shape" not in self.world.done_scripts
            and minute_ms >= 77 * 60_000
        ):
            events.append(
                self.world.emit(
                    "FORMATION_CHANGE",
                    "RED",
                    None,
                    None,
                    "4-2-4",
                    None,
                    None,
                    {"formation": "4-2-4"},
                )
            )
            self.world.done_scripts.add("shape")
        if self.world.scenario_id == "late_comeback":
            if "goal82" not in self.world.done_scripts and minute_ms >= 82 * 60_000:
                scripted = self._script_goal("red_9", "goal82")
                events.extend(scripted)
                if any(event.type == "SHOT" and event.outcome == "GOAL" for event in scripted):
                    self.world.done_scripts.add("goal82")
            if (
                "goal82" in self.world.done_scripts
                and "goal88" not in self.world.done_scripts
                and minute_ms >= 88 * 60_000
            ):
                scripted = self._script_goal("red_7", "goal88")
                events.extend(scripted)
                if any(event.type == "SHOT" and event.outcome == "GOAL" for event in scripted):
                    self.world.done_scripts.add("goal88")
        return events

    def _substitute(self, off_id: str, on_id: str) -> list[MatchEvent]:
        off = self.world.players[off_id]
        on = SimPlayer(
            player_id=on_id,
            team_id=off.team_id,
            number=shirt_number(on_id, 17),
            role=off.role,
            x=off.x,
            y=off.y,
            passing=0.74,
            shooting=0.7,
            speed=0.93,
            dribbling=0.9,
            pressing=0.8,
        )
        off.on_pitch = False
        self.world.players[on_id] = on
        if self.world.carrier_id == off_id:
            self.world.carrier_id = on_id
        event = self.world.emit(
            "SUBSTITUTION",
            off.team_id,
            on_id,
            None,
            "COMPLETE",
            Point(x=off.x, y=off.y),
            Point(x=off.x, y=off.y),
            {"player_off": off_id, "player_on": on_id},
        )
        return [event]

    def _red_card(self, player_id: str) -> list[MatchEvent]:
        player = self.world.players[player_id]
        player.on_pitch = False
        if self.world.carrier_id == player_id:
            self.restarts.turnover(Point(x=player.x, y=player.y), player.team_id)
        spot = Point(x=player.x, y=player.y)
        foul = self.world.emit("FOUL", player.team_id, player_id, None, "FOUL", spot, spot, {"card": "red"})
        card = self.world.emit("CARD", player.team_id, player_id, None, "RED", spot, spot, {"card": "red"})
        return [foul, card]

    def _script_goal(self, scorer_id: str, script_key: str) -> list[MatchEvent]:
        """Stage a deterministic goal through visible football actions.

        The old implementation could credit a player with a shot from wherever
        the ball happened to be. Here the scenario first wins possession if
        necessary, then plays a visible setup pass to the named scorer, and only
        shoots once that scorer is actually on the ball.
        """
        if self.world.flight is not None:
            return []

        scorer = self.world.players[scorer_id]
        scorer.on_pitch = True
        events: list[MatchEvent] = []

        if self.world.possession != scorer.team_id:
            previous_carrier = self.world.carrier_id
            spot = Point(x=self.world.ball.x, y=self.world.ball.y)
            previous_team = self.world.possession
            self.restarts.turnover(spot, previous_team)
            winner = self.world.carrier_id
            events.append(
                self.world.emit(
                    "INTERCEPTION",
                    scorer.team_id,
                    winner,
                    previous_carrier,
                    "WON",
                    spot,
                    spot,
                    {"scripted": True, "setup_for": scorer_id},
                )
            )
            self.world.next_action_ms = self.world.clock_ms + 900
            return events

        carrier = self.world.players.get(self.world.carrier_id) if self.world.carrier_id else None
        if carrier is None or not carrier.on_pitch or not self.movement.touching(carrier):
            return []

        if carrier.player_id != scorer_id:
            setup_key = f"{script_key}_setup"
            if setup_key in self.world.done_scripts:
                return []
            start = Point(x=self.world.ball.x, y=self.world.ball.y)
            desired = self._receive_point(scorer, scorer.team_id)
            flight = self.movement.realize(
                scorer_id, start, desired, "PASS", scorer.team_id, True, 14.0, max_s=3.2
            )
            self.world.done_scripts.add(setup_key)
            self.world.next_action_ms = self.world.clock_ms + flight.duration_ms + 900
            return [
                self.world.emit(
                    "PASS",
                    scorer.team_id,
                    carrier.player_id,
                    scorer.player_id,
                    "COMPLETE",
                    start,
                    flight.end,
                    {"scripted": True, "setup_for": scorer_id},
                )
            ]

        start = Point(x=self.world.ball.x, y=self.world.ball.y)
        end = Point(x=105.0 if scorer.team_id == "RED" else 0.0, y=34.0)
        distance_m = math.hypot(end.x - start.x, end.y - start.y)
        event = self.world.emit(
            "SHOT",
            scorer.team_id,
            scorer.player_id,
            None,
            "GOAL",
            start,
            end,
            {"distance_m": round(distance_m, 1), "xg": 0.42, "scripted": True},
        )
        duration = int(max(420, min(1200, distance_m / 18.0 * 1000)))
        self.world.flight = Flight(
            start, end, self.world.clock_ms, duration, "SHOT", scorer.team_id, None, False
        )
        self.world.carrier_id = None
        if scorer.team_id == "RED":
            self.world.score_red += 1
        else:
            self.world.score_blue += 1
        self.world.possession = other(scorer.team_id)
        self.world.possession_id += 1
        self.world.next_action_ms = self.world.clock_ms + duration + 900
        return [event]
