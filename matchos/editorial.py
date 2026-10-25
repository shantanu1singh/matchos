"""Human-readable decision explanations derived from recorded selections and evidence."""

from matchos.schemas import PipelineResult, ViewerProfile

REASONS = {
    "TACTICAL_DETAIL": "The model prioritized tactical detail for this viewer.",
    "TEAM_RELEVANCE": "The model prioritized the viewer's team.",
    "PLAYER_RELEVANCE": "The model prioritized the focused player.",
    "MAJOR_EVENT": "The model prioritized a major match event.",
    "NOVEL_CHANGE": "The model prioritized a new change in the match.",
    "ALREADY_SHOWN": "The model judged this update repetitive.",
    "LOW_SIGNAL": "The model judged this below the interruption threshold.",
    "NO_RELEVANT_UPDATE": "The model found no update worth interrupting this viewer.",
}


def explain(result: PipelineResult, viewer: ViewerProfile) -> dict:
    candidates = result.candidates.candidates if result.candidates else []
    chosen_id = result.decision.selected_candidate_id if result.decision else None
    chosen = next((candidate for candidate in candidates if candidate.id == chosen_id), result.moment)
    aired = bool(result.producer and result.producer.action == "AIR" and result.moment)
    reason_code = result.decision.editorial_reason if result.decision else None
    # A model attribution must not imply unsupported player/team/recency evidence.
    valid_reason = reason_code in REASONS
    if reason_code == "TEAM_RELEVANCE":
        valid_reason = bool(viewer.persona == "fan" and chosen and chosen.team_id == viewer.team_id)
    if reason_code == "PLAYER_RELEVANCE":
        valid_reason = bool(chosen and chosen.player_id == viewer.player_id)
    if reason_code == "TACTICAL_DETAIL":
        valid_reason = bool(chosen and chosen.frame in {"PRESS_BREAKDOWN", "MOMENTUM_SWING"})
    if reason_code == "MAJOR_EVENT":
        valid_reason = bool(chosen and chosen.frame in {"GOAL", "RED_CARD", "COMEBACK"})
    # Recency claims are explained by producer policy instead of unverified model text.
    if reason_code == "ALREADY_SHOWN":
        valid_reason = False
    from matchos.decision_client import selection_hold_reason

    model_held = bool(result.decision and selection_hold_reason(result.decision))
    fallback = bool(result.decision and result.decision.provider.startswith("local"))
    selector = "The local fallback" if fallback else "The model"
    reason = (
        REASONS[reason_code]
        if valid_reason
        else (
            f"{selector} selected this story from {len(candidates)} verified options for the {viewer.persona} view."
            if chosen
            else f"{selector} chose not to interrupt this viewer."
            if result.decision
            else "No story passed the evidence checks."
        )
    )
    if not aired and result.producer and result.producer.reason != "Decision model chose no update.":
        reason = result.producer.reason
    held = [
        {
            "id": candidate.id,
            "frame": candidate.frame,
            "team": candidate.team_id,
            "player": candidate.player_id,
            "reason": result.producer.reason
            if candidate.id == chosen_id and not aired and result.producer
            else "Not selected in this update.",
        }
        for candidate in candidates
        if candidate.id != chosen_id or not aired
    ]
    return {
        "status": "update" if aired else "no_interruption",
        "chosen": chosen.frame if chosen else None,
        "selectedId": chosen_id,
        "reason": reason,
        "reasonSource": ("fallback" if fallback else "model")
        if aired or model_held
        else "policy"
        if result.decision
        else "evidence",
        "evidence": [check.detail for check in chosen.checks if check.passed][:2] if chosen else [],
        "sourceEventCount": len(chosen.evidence) if chosen else 0,
        "held": held,
        "candidateCount": len(candidates),
        "timestamp_ms": result.state.timestamp_ms if result.state else None,
    }
