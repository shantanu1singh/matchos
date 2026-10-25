"""Rolling match state from events and tracking samples. No model calls."""

from __future__ import annotations

from collections import deque

from matchos.pitch import between_the_lines, entered_final_third, pass_against_press, pass_bypassed, shirt
from matchos.schemas import MatchEvent, MatchState, PlayerRollup, TeamId, TeamRollup, Tick
from matchos.teams import shirt_number

FIVE = 5 * 60_000
EIGHT = 8 * 60_000
THIRTY = 30 * 60_000


def _rate(events: list[MatchEvent], defending: TeamId) -> tuple[float | None, int]:
    attempts = [
        event
        for event in events
        if event.type == "PASS"
        and event.qualifiers.get("defending_team") == defending
        and pass_against_press(event)
    ]
    if not attempts:
        return None, 0
    bypasses = sum(1 for event in attempts if pass_bypassed(event))
    return bypasses / len(attempts), len(attempts)


def _entries(events: list[MatchEvent], team: TeamId) -> int:
    count = 0
    for event in events:
        if event.team_id != team:
            continue
        if (
            event.type in {"PASS", "CARRY"}
            and event.outcome == "COMPLETE"
            and (event.qualifiers.get("entered_final_third") is True or entered_final_third(event))
        ):
            count += 1
    return count


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


class StateEngine:
    def __init__(self) -> None:
        self.events: deque[MatchEvent] = deque()
        self.dismissed: dict[str, set[str]] = {"RED": set(), "BLUE": set()}
        self.samples: deque[tuple[int, float, float, float, str]] = deque()
        self.state = MatchState(
            match_id="",
            timestamp_ms=0,
            period=1,
            minute=0,
            second=0,
            score_red=0,
            score_blue=0,
            possession_team="RED",
        )

    def ingest(self, tick: Tick) -> MatchState:
        for sample in tick.samples:
            self.samples.append(
                (
                    sample.timestamp_ms,
                    sample.red_line_m,
                    sample.blue_line_m,
                    sample.ball_x,
                    sample.possession_team,
                )
            )
        for event in tick.events:
            self.events.append(event)
            if event.type == "CARD" and event.qualifiers.get("card") == "red" and event.player_id:
                self.dismissed[event.team_id].add(event.player_id)
        now = tick.frame.timestamp_ms
        self._trim(now)
        red = self._team("RED", now)
        blue = self._team("BLUE", now)
        for team_id, roll in (("RED", red), ("BLUE", blue)):
            roll.players_on_pitch = sum(
                player.team_id == team_id and player.on_pitch for player in tick.frame.players
            )
            roll.red_cards = len(self.dismissed[team_id])
        players = self._players(now)
        deltas = self._deltas(red, blue, players)
        self.state = MatchState(
            match_id=tick.match_id,
            timestamp_ms=now,
            period=tick.frame.period,
            minute=now // 60_000,
            second=(now // 1000) % 60,
            score_red=tick.frame.score_red,
            score_blue=tick.frame.score_blue,
            possession_team=tick.frame.possession_team,
            red=red,
            blue=blue,
            players=players,
            deltas=deltas,
        )
        return self.state

    def _trim(self, now: int) -> None:
        horizon = now - 40 * 60_000
        while self.events and self.events[0].timestamp_ms < horizon:
            self.events.popleft()
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()

    def _between(self, start: int, end: int) -> list[MatchEvent]:
        return [event for event in self.events if start <= event.timestamp_ms < end]

    def _team(self, team: TeamId, now: int) -> TeamRollup:
        recent = self._between(now - FIVE, now)
        previous = self._between(now - 2 * FIVE, now - FIVE)
        rate, sample = _rate(recent, team)
        prev_rate, _ = _rate(previous, team)
        line_now = self._line(team, now - 60_000, now)
        line_prev = self._line(team, now - FIVE - 60_000, now - FIVE)
        poss = self._possession(team, now - FIVE, now)
        return TeamRollup(
            possession_last5=poss,
            final_third_entries_last5=_entries(recent, team),
            final_third_entries_prev5=_entries(previous, team),
            press_bypass_rate=rate,
            press_bypass_prev=prev_rate,
            press_bypass_sample=sample,
            defensive_line_m=line_now,
            defensive_line_prev_m=line_prev,
            shots_last5=sum(1 for event in recent if event.type == "SHOT" and event.team_id == team),
            turnovers_last5=sum(
                1
                for event in recent
                if event.type == "PASS" and event.outcome == "INCOMPLETE" and event.team_id == team
            ),
        )

    def _line(self, team: TeamId, start: int, end: int) -> float | None:
        index = 1 if team == "RED" else 2
        values = [sample[index] for sample in self.samples if start <= sample[0] < end]
        mean = _mean(values)
        return round(mean, 2) if mean is not None else None

    def _possession(self, team: TeamId, start: int, end: int) -> float:
        rows = [sample for sample in self.samples if start <= sample[0] < end]
        if not rows:
            return 0.5
        held = sum(1 for sample in rows if sample[4] == team)
        return round(held / len(rows), 3)

    def _players(self, now: int) -> dict[str, PlayerRollup]:
        last8 = self._between(now - EIGHT, now)
        prev30 = self._between(now - EIGHT - THIRTY, now - EIGHT)
        found: dict[str, PlayerRollup] = {}
        for event in self.events:
            for player_id in (event.player_id, event.target_player_id):
                if player_id and player_id not in found:
                    team, _, number = _split(player_id)
                    found[player_id] = PlayerRollup(team_id=team, number=number)
            if event.type == "SUBSTITUTION":
                on_id = str(event.qualifiers.get("player_on") or event.player_id or "")
                off_id = str(event.qualifiers.get("player_off") or "")
                if on_id:
                    team, _, number = _split(on_id)
                    row = found.setdefault(on_id, PlayerRollup(team_id=team, number=number))
                    row.subbed_on_ms = event.timestamp_ms
                    row.on_pitch = True
                if off_id and off_id in found:
                    found[off_id].on_pitch = False
            if event.type == "CARD" and event.qualifiers.get("card") == "red" and event.player_id in found:
                found[event.player_id].on_pitch = False
        for player_id, row in found.items():
            row.final_third_receptions_last8 = _receptions(last8, player_id, final_third=True)
            row.final_third_receptions_prev30 = _receptions(prev30, player_id, final_third=True)
            row.between_lines_last8 = _receptions(last8, player_id, between=True)
            row.between_lines_prev30 = _receptions(prev30, player_id, between=True)
            since = row.subbed_on_ms
            if since is not None:
                window = self._between(since, now)
                row.progressive_actions = _progressive(window, player_id)
                row.box_entries = sum(
                    1
                    for event in window
                    if event.player_id == player_id
                    and event.outcome == "COMPLETE"
                    and event.qualifiers.get("box_entry") is True
                )
                row.shots = sum(
                    1
                    for event in window
                    if event.type == "SHOT"
                    and (event.player_id == player_id or event.qualifiers.get("assisted_by") == player_id)
                )
        return found

    def _deltas(
        self, red: TeamRollup, blue: TeamRollup, players: dict[str, PlayerRollup]
    ) -> dict[str, float]:
        deltas: dict[str, float] = {}
        for name, roll in (("red", red), ("blue", blue)):
            if roll.press_bypass_rate is not None and roll.press_bypass_prev is not None:
                deltas[f"{name}_press_bypass_delta"] = round(
                    roll.press_bypass_rate - roll.press_bypass_prev, 3
                )
            if roll.defensive_line_m is not None and roll.defensive_line_prev_m is not None:
                deltas[f"{name}_line_delta_m"] = round(roll.defensive_line_m - roll.defensive_line_prev_m, 2)
            deltas[f"{name}_entries_delta"] = float(
                roll.final_third_entries_last5 - roll.final_third_entries_prev5
            )
            deltas[f"{name}_possession_last5"] = roll.possession_last5
        focus = players.get("red_7")
        if focus and focus.between_lines_prev30:
            prev_rate = focus.between_lines_prev30 / 30.0
            now_rate = focus.between_lines_last8 / 8.0
            if prev_rate > 0:
                deltas["red_7_involvement_delta"] = round((now_rate - prev_rate) / prev_rate, 3)
        elif focus:
            deltas["red_7_involvement_delta"] = float(focus.between_lines_last8)
        sub = players.get("red_17")
        if sub and sub.subbed_on_ms is not None:
            deltas["red_17_progressive"] = float(sub.progressive_actions)
            deltas["red_17_box_entries"] = float(sub.box_entries)
        return deltas

    def evidence(self, predicate, start: int, end: int, limit: int = 6) -> list[MatchEvent]:
        rows = [event for event in self._between(start, end) if predicate(event)]
        return rows[-limit:]


def _split(player_id: str) -> tuple[TeamId, str, int]:
    team, number = player_id.split("_", 1)
    team_id: TeamId = "RED" if team.lower() == "red" else "BLUE"
    try:
        parsed = int(number)
    except ValueError:
        parsed = 0
    return team_id, team, shirt_number(player_id, parsed)


def _receptions(
    events: list[MatchEvent], player_id: str, final_third: bool = False, between: bool = False
) -> int:
    count = 0
    for event in events:
        if event.type != "PASS" or event.outcome != "COMPLETE" or event.target_player_id != player_id:
            continue
        if between and between_the_lines(event):
            count += 1
        elif final_third and event.end_location is not None:
            team = event.team_id
            from matchos.pitch import in_final_third as third

            if third(team, event.end_location.x):
                count += 1
    return count


def _progressive(events: list[MatchEvent], player_id: str) -> int:
    count = 0
    for event in events:
        if event.player_id != player_id or event.outcome not in {"COMPLETE", "GOAL"}:
            continue
        if event.type == "CARRY" and event.location and event.end_location:
            forward = event.end_location.x - event.location.x
            if event.team_id == "BLUE":
                forward = -forward
            if forward >= 8 or event.qualifiers.get("box_entry") is True:
                count += 1
        elif event.type == "SHOT":
            count += 1
        elif event.type == "PASS" and pass_bypassed(event):
            count += 1
    return count


def describe_player(player_id: str | None) -> str:
    return shirt(player_id)
