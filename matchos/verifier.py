"""Prove that a proposed frame is supported by the rolling state and events."""

from __future__ import annotations

from matchos.teams import team_name
from matchos.pitch import event_label, pass_bypassed, shirt
from matchos.schemas import (
    Check,
    EvidenceItem,
    MatchEvent,
    MatchState,
    MomentDecision,
    MomentFrame,
    VerificationResult,
    VerifiedMoment,
    VerifiedCandidates,
)
from matchos.state_engine import StateEngine

MAJOR_EVENT_WINDOW_MS = 60_000
MAJOR_STORY_TTL_MS = 120_000

FRAME_ORDER: list[MomentFrame] = [
    "GOAL",
    "COMEBACK",
    "RED_CARD",
    "PRESS_BREAKDOWN",
    "SUBSTITUTE_IMPACT",
    "PLAYER_IMPACT",
    "MOMENTUM_SWING",
    "SHOT_CHANCE",
]


class Verifier:
    def __init__(self, state_engine: StateEngine) -> None:
        self.state_engine = state_engine
        self._seq = 0

    def candidates(self, state: MatchState, events: list[MatchEvent], focus_player_id: str, additional_focus_ids: list[str] | None = None) -> VerifiedCandidates:
        """Check every supported story before asking a model for editorial preference."""
        result = VerifiedCandidates()
        for frame in FRAME_ORDER:
            # Existing evidence functions share a decision-shaped input for player focus.
            # These neutral placeholders are not model probabilities or editorial scores.
            seed = MomentDecision(tactical_shift=0, momentum_shift=0, player_story=0,
                                  importance=0, primary_frame=frame, primary_frame_confidence=0,
                                  should_surface=0, frame_scores={}, player_id=focus_player_id,
                                  provider="evidence-candidate", latency_ms=0)
            moment, checks = self._check_frame(frame, state, seed, events)
            result.checks.extend(checks)
            if moment is not None:
                # Failed checks for alternative teams stay in the set diagnostics.
                moment.checks = moment.checks or [check for check in checks if check.passed]
                result.candidates.append(moment)
        for player_id in dict.fromkeys(additional_focus_ids or []):
            if player_id == focus_player_id:
                continue
            seed.player_id = player_id
            moment, checks = self._player(state, seed)
            result.checks.extend(checks)
            if moment is not None:
                moment.checks = [check for check in checks if check.passed]
                result.candidates.append(moment)
        return result

    def verify(self, state: MatchState, decision: MomentDecision, events: list[MatchEvent]) -> VerificationResult:
        """Legacy frame-decision helper; the live workflow builds candidates first."""
        ranked = sorted(
            FRAME_ORDER,
            key=lambda frame: (frame == decision.primary_frame, decision.frame_scores.get(frame, 0.0)),
            reverse=True,
        )
        checks: list[Check] = []
        for frame in ranked:
            if decision.frame_scores.get(frame, 0.0) < 0.45 and frame != decision.primary_frame:
                continue
            result = self._check_frame(frame, state, decision, events)
            checks.extend(result[1])
            if result[0] is not None:
                return VerificationResult(verified=True, moment=result[0], checks=checks)
        if not checks:
            checks.append(Check(name="frame", passed=False, detail="No frame cleared the evidence rules."))
        return VerificationResult(verified=False, checks=checks, reason="Evidence did not support a moment.")

    def _check_frame(
        self,
        frame: MomentFrame,
        state: MatchState,
        decision: MomentDecision,
        events: list[MatchEvent],
    ) -> tuple[VerifiedMoment | None, list[Check]]:
        if frame == "PRESS_BREAKDOWN":
            return self._press(state, decision)
        if frame == "GOAL":
            return self._goal(state, decision, events, comeback=False)
        if frame == "COMEBACK":
            return self._goal(state, decision, events, comeback=True)
        if frame == "RED_CARD":
            return self._card(state, decision, events)
        if frame == "PLAYER_IMPACT":
            return self._player(state, decision)
        if frame == "SUBSTITUTE_IMPACT":
            return self._sub(state, decision)
        if frame == "MOMENTUM_SWING":
            return self._momentum(state, decision)
        if frame == "SHOT_CHANCE":
            return self._shot(state, decision, events)
        return None, []

    def _press(self, state: MatchState, decision: MomentDecision) -> tuple[VerifiedMoment | None, list[Check]]:
        checks: list[Check] = []
        candidates = []
        for team, roll in (("BLUE", state.blue), ("RED", state.red)):
            if roll.press_bypass_rate is None or roll.press_bypass_prev is None:
                checks.append(Check(name=f"{team.lower()}_bypass_history", passed=False, detail="Need a before-and-after window."))
                continue
            delta = roll.press_bypass_rate - roll.press_bypass_prev
            enough = roll.press_bypass_sample >= 5 and delta >= 0.25
            attacking = "RED" if team == "BLUE" else "BLUE"
            attack_roll = state.red if attacking == "RED" else state.blue
            line_drop = (roll.defensive_line_prev_m - roll.defensive_line_m
                         if roll.defensive_line_prev_m is not None and roll.defensive_line_m is not None else None)
            entries_gain = attack_roll.final_third_entries_last5 - attack_roll.final_third_entries_prev5
            # A higher bypass ratio alone can be a handful of counterattacks.
            # Corroborate deterioration with a deeper block or greater penetration.
            corroborated = (line_drop is not None and line_drop >= 5.0) or entries_gain >= 5
            checks.append(
                Check(
                    name=f"{team.lower()}_press_bypass",
                    passed=enough,
                    detail=f"Passes through {team_name(team)}'s press increased {delta * 100:.0f} percentage points on {roll.press_bypass_sample} attempts",
                )
            )
            line_detail = f"line retreated {line_drop:.1f}m" if line_drop is not None else "line history unavailable"
            checks.append(Check(name=f"{team.lower()}_press_corroboration", passed=corroborated,
                                detail=f"{team_name(team)}: {line_detail}; {team_name(attacking)}'s final-third entries changed by {entries_gain:+d}. Need at least 5m retreat or 5 extra entries."))
            if enough and corroborated:
                candidates.append((delta, team, roll))
        if not candidates:
            return None, checks
        _, team, roll = max(candidates, key=lambda item: item[0])
        attacking = "RED" if team == "BLUE" else "BLUE"
        attack_roll = state.red if attacking == "RED" else state.blue
        now = state.timestamp_ms
        evidence = self.state_engine.evidence(
            lambda event: event.qualifiers.get("defending_team") == team and pass_bypassed(event),
            now - 5 * 60_000,
            now,
            limit=5,
        )
        has_events = len(evidence) >= 2
        checks.append(Check(name="evidence_events", passed=has_events, detail=f"{len(evidence)} bypassing passes"))
        if not has_events:
            return None, checks
        line_now = roll.defensive_line_m or 0.0
        line_prev = roll.defensive_line_prev_m if roll.defensive_line_prev_m is not None else line_now
        facts: dict[str, float | int | str] = {
            "defendingTeam": team_name(team),
            "attackingTeam": team_name(attacking),
            "pressBypassBefore": round(roll.press_bypass_prev or 0.0, 3),
            "pressBypassNow": round(roll.press_bypass_rate or 0.0, 3),
            "pressBypassSample": roll.press_bypass_sample,
            "defensiveLineBeforeM": round(line_prev, 2),
            "defensiveLineNowM": round(line_now, 2),
            "defensiveLineDeltaM": round(line_now - line_prev, 2),
            "opponentFinalThirdEntries": attack_roll.final_third_entries_last5,
            "finalThirdEntriesBefore": attack_roll.final_third_entries_prev5,
        }
        checks.append(Check(name="facts_bound", passed=True, detail="Facts copied from rolling state."))
        moment = self._moment(
            state,
            decision,
            "TACTICAL_SHIFT",
            "PRESS_BREAKDOWN",
            team,  # type: ignore[arg-type]
            None,
            facts,
            evidence,
            highlight=[event.player_id for event in evidence if event.player_id][:4],
        )
        # Alternative-team diagnostics belong to the pool, not this story's proof.
        moment.checks = [check for check in checks if check.passed and
                         (check.name.startswith(f"{team.lower()}_") or check.name in {"evidence_events", "facts_bound"})]
        return moment, checks

    def _goal(
        self,
        state: MatchState,
        decision: MomentDecision,
        events: list[MatchEvent],
        comeback: bool,
    ) -> tuple[VerifiedMoment | None, list[Check]]:
        goals = [event for event in events if event.type == "SHOT" and event.outcome == "GOAL"
                 and 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS]
        recent = [event for event in self.state_engine.events if event.type == "SHOT" and event.outcome == "GOAL"
                  and 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS]
        goals = goals or recent
        passed = bool(goals)
        checks = [Check(name="goal_event", passed=passed, detail="Goal event verified in the recent feed." if passed else "No goal event.")]
        if not passed:
            return None, checks
        goal = goals[-1]
        margin = (state.score_red - state.score_blue) * (1 if goal.team_id == "RED" else -1)
        # A goal taking a level match into the lead is not a comeback.
        is_comeback = margin == 0
        if comeback and not is_comeback:
            return None, checks + [Check(name="comeback_score", passed=False, detail="Scoreline is not a comeback.")]
        if comeback:
            checks.append(Check(name="comeback_score", passed=True, detail=f"{state.score_red}-{state.score_blue}"))
        facts = {
            "scoringTeam": team_name(goal.team_id),
            "scorer": goal.player_id or "",
            "scoreRed": state.score_red,
            "scoreBlue": state.score_blue,
            "minute": goal.minute,
            "scoreBeforeRed": state.score_red - int(goal.team_id == "RED"),
            "scoreBeforeBlue": state.score_blue - int(goal.team_id == "BLUE"),
        }
        frame: MomentFrame = "COMEBACK" if comeback else "GOAL"
        return self._moment(state, decision, "GOAL" if not comeback else "COMEBACK", frame, goal.team_id, goal.player_id, facts, [goal], [goal.player_id] if goal.player_id else []), checks

    def _card(self, state: MatchState, decision: MomentDecision, events: list[MatchEvent]) -> tuple[VerifiedMoment | None, list[Check]]:
        cards = [event for event in events if event.type == "CARD" and event.qualifiers.get("card") == "red"
                 and 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS]
        cards = cards or [event for event in self.state_engine.events if event.type == "CARD" and event.qualifiers.get("card") == "red"
                          and 0 <= state.timestamp_ms - event.timestamp_ms <= MAJOR_EVENT_WINDOW_MS]
        checks = [Check(name="red_card_event", passed=bool(cards), detail="Red card event present." if cards else "No red card.")]
        if not cards:
            return None, checks
        card = cards[-1]
        facts = {"cardTeam": team_name(card.team_id), "player": card.player_id or "", "minute": card.minute}
        return self._moment(state, decision, "RED_CARD", "RED_CARD", card.team_id, card.player_id, facts, [card], [card.player_id] if card.player_id else []), checks

    def _player(self, state: MatchState, decision: MomentDecision) -> tuple[VerifiedMoment | None, list[Check]]:
        focus_id = decision.player_id or ""
        player = state.players.get(focus_id)
        if player is None:
            return None, [Check(name="player_known", passed=False, detail="No rollup for the selected player.")]
        rate_up = player.between_lines_last8 >= 4 and player.between_lines_last8 >= max(2, player.between_lines_prev30 // 2)
        checks = [
            Check(
                name="between_lines",
                passed=rate_up,
                detail=f"{player.between_lines_last8} in 8' vs {player.between_lines_prev30} in the previous 30'",
            )
        ]
        if not rate_up:
            return None, checks
        now = state.timestamp_ms
        evidence = self.state_engine.evidence(
            lambda event: event.type == "PASS" and event.outcome == "COMPLETE" and event.target_player_id == focus_id,
            now - 8 * 60_000,
            now,
            limit=6,
        )
        checks.append(Check(name="evidence_events", passed=len(evidence) >= 2, detail=f"{len(evidence)} receptions"))
        if len(evidence) < 2:
            return None, checks
        label = shirt(focus_id) or focus_id
        facts = {
            "player": label,
            "playerId": focus_id,
            "betweenLinesLast8": player.between_lines_last8,
            "betweenLinesPrev30": player.between_lines_prev30,
            "finalThirdReceptionsLast8": player.final_third_receptions_last8,
            "finalThirdReceptionsPrev30": player.final_third_receptions_prev30,
        }
        return self._moment(
            state,
            decision,
            "PLAYER_STORY",
            "PLAYER_IMPACT",
            player.team_id,
            focus_id,
            facts,
            evidence,
            [focus_id],
        ), checks

    def _sub(self, state: MatchState, decision: MomentDecision) -> tuple[VerifiedMoment | None, list[Check]]:
        player = state.players.get("red_17")
        on = player is not None and player.subbed_on_ms is not None
        impact = on and player is not None and (player.progressive_actions >= 3 or player.box_entries + player.shots >= 3)
        checks = [Check(name="substitute_on", passed=on, detail="Substitute is on the pitch." if on else "No substitute rollup.")]
        if player is not None:
            checks.append(
                Check(
                    name="substitute_actions",
                    passed=bool(impact),
                    detail=f"{player.progressive_actions} progressive, {player.box_entries} box entries, {player.shots} shots",
                )
            )
        if not impact or player is None:
            return None, checks
        evidence = self.state_engine.evidence(
            lambda event: event.player_id == "red_17" and event.type in {"CARRY", "SHOT", "PASS"},
            player.subbed_on_ms or 0,
            state.timestamp_ms,
            limit=5,
        )
        checks.append(Check(name="evidence_events", passed=len(evidence) >= 2, detail=f"{len(evidence)} actions"))
        if len(evidence) < 2:
            return None, checks
        facts = {
            "player": shirt("red_17"),
            "playerId": "red_17",
            "progressiveActions": player.progressive_actions,
            "boxEntries": player.box_entries,
            "shots": player.shots,
            "minuteOn": (player.subbed_on_ms or 0) // 60_000,
        }
        return self._moment(state, decision, "PLAYER_STORY", "SUBSTITUTE_IMPACT", "RED", "red_17", facts, evidence, ["red_17"]), checks

    def _momentum(self, state: MatchState, decision: MomentDecision) -> tuple[VerifiedMoment | None, list[Check]]:
        red_share = state.red.possession_last5
        leading = "RED" if red_share >= 0.5 else "BLUE"
        share = red_share if leading == "RED" else state.blue.possession_last5
        entries = state.red.final_third_entries_last5 if leading == "RED" else state.blue.final_third_entries_last5
        prev = state.red.final_third_entries_prev5 if leading == "RED" else state.blue.final_third_entries_prev5
        passed = share >= 0.62 and (entries - prev) >= 2
        checks = [Check(name="territory", passed=passed, detail=f"{leading} possession {share:.0%}, entries {prev}→{entries}")]
        if not passed:
            return None, checks
        facts = {
            "leadingTeam": team_name(leading),
            "trailingTeam": team_name("BLUE" if leading == "RED" else "RED"),
            "possessionLast5": round(share, 3),
            "finalThirdEntries": entries,
            "finalThirdEntriesBefore": prev,
        }
        return self._moment(state, decision, "MOMENTUM_SHIFT", "MOMENTUM_SWING", leading, None, facts, [], []), checks

    def _shot(self, state: MatchState, decision: MomentDecision, events: list[MatchEvent]) -> tuple[VerifiedMoment | None, list[Check]]:
        shots = [event for event in events if event.type == "SHOT" and event.outcome in {"SAVED", "OFF_TARGET"}]
        passed = bool(shots)
        checks = [Check(name="shot_event", passed=passed, detail="Shot event present." if passed else "No shot.")]
        if not passed:
            return None, checks
        shot = shots[-1]
        facts = {
            "team": team_name(shot.team_id),
            "player": shot.player_id or "",
            "outcome": shot.outcome or "",
            "minute": shot.minute,
        }
        return self._moment(state, decision, "SHOT", "SHOT_CHANCE", shot.team_id, shot.player_id, facts, [shot], [shot.player_id] if shot.player_id else []), checks

    def _moment(
        self,
        state: MatchState,
        decision: MomentDecision,
        moment_type: str,
        frame: MomentFrame,
        team_id,
        player_id: str | None,
        facts: dict[str, float | int | str],
        evidence: list[MatchEvent],
        highlight: list[str | None],
    ) -> VerifiedMoment:
        self._seq += 1
        ttl_ms = MAJOR_STORY_TTL_MS if frame in {"GOAL", "COMEBACK", "RED_CARD"} else 40_000
        # Match context is sourced from the same frozen deterministic state.
        facts = {"scoreRed": state.score_red, "scoreBlue": state.score_blue,
                 "playersRed": state.red.players_on_pitch, "playersBlue": state.blue.players_on_pitch,
                 "redCardsRed": state.red.red_cards, "redCardsBlue": state.blue.red_cards, **facts}
        items = [
            EvidenceItem(
                id=event.id,
                timestamp_ms=event.timestamp_ms,
                minute=event.minute,
                second=event.second,
                type=event.type,
                team_id=event.team_id,
                player_id=event.player_id,
                target_player_id=event.target_player_id,
                outcome=event.outcome,
                location=event.location,
                end_location=event.end_location,
                label=event_label(event),
            )
            for event in evidence
        ]
        return VerifiedMoment(
            id=f"moment-{self._seq}",
            type=moment_type,
            team_id=team_id,
            player_id=player_id,
            frame=frame,
            importance=round(min(1.0, decision.importance / 5.0), 3),
            confidence=decision.primary_frame_confidence if frame == decision.primary_frame else decision.frame_scores.get(frame, decision.primary_frame_confidence),
            facts=facts,
            evidence=[item.id for item in items],
            evidence_detail=items,
            valid_from_ms=state.timestamp_ms,
            expires_at_ms=(min(event.timestamp_ms for event in evidence) if frame in {"GOAL", "COMEBACK", "RED_CARD"} and evidence else state.timestamp_ms) + ttl_ms,
            highlight_player_ids=[player for player in highlight if player],
            checks=[],
        )
