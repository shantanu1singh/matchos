"""Code gate in front of the decision call. Boring ticks stop here."""

from __future__ import annotations

from matchos.schemas import Tick, TriggerResult

MIN_GAP_MS = 12_000


class TriggerEngine:
    def __init__(self) -> None:
        self.last_fire_ms = -MIN_GAP_MS

    def evaluate(self, tick: Tick, deltas: dict[str, float], sample_sizes: dict[str, int]) -> TriggerResult:
        reasons: list[str] = []
        for team in ("red", "blue"):
            bypass = deltas.get(f"{team}_press_bypass_delta")
            sample = sample_sizes.get(team, 0)
            rate = sample_sizes.get(f"{team}_rate", 0.0)
            # A press collapse needs a real jump, a high current rate, and a block that has actually dropped.
            line_drop = deltas.get(f"{team}_line_delta_m")
            if (
                bypass is not None
                and bypass >= 0.25
                and sample >= 5
                and rate >= 0.68
                and line_drop is not None
                and line_drop <= -5
            ):
                reasons.append(f"{team}_press_bypass")
            entries = deltas.get(f"{team}_entries_delta", 0.0)
            if entries >= 5 and sample_sizes.get(f"{team}_possession", 0.0) >= 0.62:
                reasons.append(f"{team}_entries")
        if deltas.get("focus_player_involvement_delta", 0.0) >= 0.8 and sample_sizes.get("focus_between", 0) >= 4:
            reasons.append("player_involvement")
        if deltas.get("red_17_progressive", 0.0) >= 4:
            reasons.append("substitute_impact")
        major = [event.type for event in tick.events if event.type in {"CARD", "SUBSTITUTION"} or (event.type == "SHOT" and event.outcome == "GOAL")]
        if any(event.qualifiers.get("card") == "red" for event in tick.events):
            reasons.append("red_card")
        if "SUBSTITUTION" in major:
            reasons.append("substitution")
        if any(event.type == "SHOT" and event.outcome == "GOAL" for event in tick.events):
            reasons.append("goal")
        if not reasons:
            return TriggerResult(fired=False)
        now = tick.frame.timestamp_ms
        forced = any(reason in {"goal", "red_card", "substitution"} for reason in reasons)
        if not forced and now - self.last_fire_ms < MIN_GAP_MS:
            return TriggerResult(fired=False, reasons=reasons, cooled_down=True)
        self.last_fire_ms = now
        return TriggerResult(fired=True, reasons=list(dict.fromkeys(reasons)))
