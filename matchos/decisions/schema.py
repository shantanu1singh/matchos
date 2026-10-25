"""Provider-independent decision questions, evidence payloads and response validation."""

from __future__ import annotations

import math
from typing import get_args

from matchos.schemas import MatchState, MomentDecision, MomentFrame, VerifiedMoment, ViewerProfile
from matchos.teams import identity_context

FRAMES = get_args(MomentFrame)
SURFACE_THRESHOLD = 0.5
QUESTIONS = {
    "tacticalShift": "Has a team's pressing or territorial structure materially changed?",
    "momentumShift": "Has control of the match swung enough to matter?",
    "playerStory": "Is one player's recent involvement a story of its own?",
    "importance": "How important is the strongest change, from 0 to 5?",
    "primaryFrame": "Which single frame best names the change?",
    "shouldSurface": "Is this worth interrupting a viewer?",
}


def _questions() -> list[dict]:
    questions = [
        {"type": "predicate", "name": name, "instructions": QUESTIONS[name]}
        for name in ("tacticalShift", "momentumShift", "playerStory", "shouldSurface")
    ]
    questions.extend(
        [
            {
                "type": "score",
                "name": "importance",
                "instructions": QUESTIONS["importance"],
                "levels": [{"label": str(level)} for level in range(6)],
            },
            {
                "type": "choice",
                "name": "primaryFrame",
                "instructions": QUESTIONS["primaryFrame"],
                "choices": [{"value": frame} for frame in FRAMES],
            },
            {
                "type": "choice",
                "name": "team",
                "instructions": "Which team is the subject of the primary frame? Choose NONE if neither.",
                "choices": [{"value": team} for team in ("RED", "BLUE", "NONE")],
            },
        ]
    )
    return questions


def _decision_state(state: MatchState, reasons: list[str], focus_player_id: str | None) -> dict:
    return {
        "roster": identity_context(),
        "minute": state.minute,
        "score": {"RED": state.score_red, "BLUE": state.score_blue},
        "reasons": reasons,
        "focusPlayerId": focus_player_id,
        "deltas": state.deltas,
        "teams": {"RED": state.red.model_dump(mode="json"), "BLUE": state.blue.model_dump(mode="json")},
        "players": {key: player.model_dump(mode="json") for key, player in state.players.items()},
    }


def _systemone_questions(questions_list: list[dict] | None = None) -> dict:
    questions = {}
    for question in questions_list if questions_list is not None else _questions():
        converted = {"type": question["type"], "instructions": question["instructions"]}
        if question["type"] == "predicate":
            converted["type"] = "noul"
        elif question["type"] == "choice":
            converted["criteria"] = {
                item["value"]: item["value"].replace("_", " ") for item in question["choices"]
            }
        elif question["type"] == "score":
            converted["criteria"] = [item["label"] for item in question["levels"]]
        questions[question["name"]] = converted
    return questions


def _parse_systemone(body: dict, provider: str, latency_ms: float) -> MomentDecision:
    if not isinstance(body, dict):
        raise ValueError("Invalid SystemOne response envelope")
    if body.get("success") is False:
        raise ValueError("SystemOne rejected the decision request")
    result = body.get("result", body)
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("Invalid SystemOne answers")
    answers = result["answers"]
    normalized = []
    for name, question in _systemone_questions().items():
        answer = answers[name]
        if not isinstance(answer, dict):
            raise ValueError("Invalid SystemOne answer")
        if answer["type"] != question["type"]:
            raise ValueError("Invalid SystemOne decision answer type")
        if question["type"] == "noul":
            normalized.append({"name": name, "type": "predicate", "probability": answer["noul"]})
        elif question["type"] == "choice":
            if not isinstance(answer["probabilities"], dict):
                raise ValueError("Invalid SystemOne choice probabilities")
            normalized.append(
                {
                    "name": name,
                    "type": "choice",
                    "choice": answer["choice"],
                    "confidence": answer["confidence"],
                    "probabilities": [
                        {"value": key, "probability": probability}
                        for key, probability in answer["probabilities"].items()
                    ],
                }
            )
        else:
            normalized.append({"name": name, "type": "score", "score": answer["score"]})
    return _parse_gateway({"answers": normalized}, provider, latency_ms)


def _selection_questions(candidates: list[VerifiedMoment]) -> list[dict]:
    from matchos.editorial import REASONS

    return [
        {
            "name": "reason",
            "type": "choice",
            "instructions": "Choose the main editorial reason for your selection or abstention. Use only reasons supported by the supplied viewer, candidates and recent updates.",
            "choices": [{"value": code} for code in REASONS],
        },
        {
            "name": "candidate",
            "type": "choice",
            "instructions": "Choose the single verified candidate most worth showing NOW to this viewer, or NONE. Judge interest, novelty, team/player relevance, match phase, viewingContext and recent updates. All candidates have already passed evidence checks; do not re-detect events. Goals and red cards usually deserve immediate attention. A viewer with no aired story may benefit from useful initial match context; later updates need a new development. Prefer NONE to weak, repetitive or unhelpful updates.",
            "choices": [{"value": moment.id} for moment in candidates] + [{"value": "NONE"}],
        },
        {
            "name": "importance",
            "type": "score",
            "instructions": "Editorial importance of the chosen candidate for this viewer, from 0 to 5; 0 for NONE.",
            "levels": [{"label": str(level)} for level in range(6)],
        },
        {
            "name": "shouldSurface",
            "type": "predicate",
            "instructions": "Should the chosen verified candidate be published now? When viewingContext.hasAiredStory is false, assess whether it provides useful initial match context, rather than interrupting existing commentary. Consider the supplied waiting time, but do not publish a weak story just to fill silence. After a story has aired, require a useful new development and consider recent updates. Answer no for NONE.",
        },
    ]


def _editorial_state(
    state: MatchState, candidates: list[VerifiedMoment], viewer: ViewerProfile, context: dict
) -> dict:
    from matchos.language_agent import PERCENT_FACTS
    from matchos.publication import GAPS

    recent = context.get("recentUpdates", [])
    viewing = context.get("viewingContext") or {
        "hasAiredStory": bool(recent),
        "secondsWatching": None,
        "secondsWaitingForFirstStory": None,
        "secondsSinceLastAir": recent[-1].get("secondsAgo") if recent else None,
    }
    policies = {
        "analyst": "Cover both teams neutrally, with no club allegiance or preferred player. Prioritize meaningful tactical or territorial changes with strong before-and-after evidence; cover major goals and dismissals using their verified score and player-count context.",
        "fan": "Prioritize the selected team's fortunes, including verified pressure from the opponent. A goal or dismissal for either side can change the supported team's score or player advantage; use that verified context. Avoid repetitive emotional commentary.",
        "player": "Prioritize direct evidence about the focused player. Prefer NONE for unrelated tactical stories unless there is a major match event.",
        "casual": "Interrupt for goals, red cards, comebacks or clearly significant chances. Prefer NONE for ordinary tactical fluctuations, gradual possession changes and repeated updates.",
        "broadcaster": "Cover the whole match neutrally, with no club allegiance or preferred player. Ask: what should the audience notice right now? Prioritize goals, dismissals, score changes and clear new turning points that make a useful live commentary cue. Tactical changes deserve airtime when they establish a clear match story, not merely because another metric changed. Prefer NONE over routine possession fluctuations or repeats. Use only the supplied evidence; do not invent drama, chances or causal links.",
    }
    if viewer.persona == "broadcaster":
        policies["broadcaster"] += (
            " No story has aired for this viewer yet. Provide an initial, concise match-context update when the verified candidates establish a meaningful development. Sustained possession supported by a substantial increase in final-third entries can be a worthwhile opening cue; it is more than an isolated possession fluctuation. Consider how long this viewer has been waiting. You do not need to wait for a goal or dismissal. Still choose NONE when the evidence offers no useful story."
            if not viewing["hasAiredStory"]
            else " This viewer has already received match context. Prefer a genuinely new development over restating that context; respect secondsSinceLastAir and recentUpdates."
        )
    return {
        "task": "Select from verified candidates; do not invent facts or choose unavailable candidates.",
        "roster": identity_context(),
        "minute": state.minute,
        "score": {"RED": state.score_red, "BLUE": state.score_blue},
        "playersOnPitch": {"RED": state.red.players_on_pitch, "BLUE": state.blue.players_on_pitch},
        "redCards": {"RED": state.red.red_cards, "BLUE": state.blue.red_cards},
        "viewer": viewer.model_profile(),
        "viewingContext": viewing,
        "editorialPolicy": {
            "guidance": policies[viewer.persona],
            "minimumGapSeconds": GAPS[viewer.persona],
            "minimumPublicationScore": SURFACE_THRESHOLD,
            "repeatWindowSeconds": 90,
            "onAirWindowSeconds": 40,
            "postGoalTacticalHoldSeconds": 15,
            "majorEventInterruptions": ["GOAL", "COMEBACK", "RED_CARD"],
            "timeUnit": "wall-clock seconds",
        },
        **context,
        "verifiedCandidates": [
            {
                "id": moment.id,
                "frame": moment.frame,
                "team": moment.team_id,
                "player": moment.player_id,
                "facts": {
                    key: f"{round(float(value) * 100)}%" if key in PERCENT_FACTS else value
                    for key, value in moment.facts.items()
                },
                "checks": [check.model_dump(mode="json") for check in moment.checks],
                "evidence": [item.model_dump(mode="json") for item in moment.evidence_detail],
            }
            for moment in candidates
        ],
    }


def _selection_answers(body: dict, native_systemone: bool) -> dict:
    if not native_systemone:
        return {answer["name"]: answer for answer in body["answers"]}
    if body.get("success") is False:
        raise ValueError("SystemOne rejected selection")
    raw = body.get("result", body)["answers"]
    if (
        raw["candidate"]["type"] != "choice"
        or raw["importance"]["type"] != "score"
        or raw["shouldSurface"]["type"] != "noul"
    ):
        raise ValueError("Invalid selection answer types")
    choice = raw["candidate"]
    return {
        **({"reason": raw["reason"]} if "reason" in raw else {}),
        "candidate": {
            **choice,
            "probabilities": [
                {"value": key, "probability": value} for key, value in choice["probabilities"].items()
            ],
        },
        "importance": raw["importance"],
        "shouldSurface": {"type": "predicate", "probability": raw["shouldSurface"]["noul"]},
    }


def _parse_selection(
    answers: dict, candidates: list[VerifiedMoment], provider: str, latency_ms: float
) -> MomentDecision:
    for name, kind in (("candidate", "choice"), ("importance", "score"), ("shouldSurface", "predicate")):
        if answers[name]["type"] != kind:
            raise ValueError("Invalid selection answer")
    choice = answers["candidate"]
    available = {moment.id: moment for moment in candidates}
    selected = choice["choice"]
    if selected not in {*available, "NONE"}:
        raise ValueError("Selection is not a verified candidate")
    scores = {item["value"]: _bounded(item["probability"]) for item in choice["probabilities"]}
    if set(scores) - {*available, "NONE"}:
        raise ValueError("Unverified candidate in selection probabilities")
    confidence = _bounded(choice.get("confidence", scores.get(selected, 0)))
    scores.setdefault(selected, confidence)
    importance = _bounded(answers["importance"]["score"], 5)
    surface = _bounded(answers["shouldSurface"]["probability"])
    reason = answers.get("reason")
    reason_code = None
    if reason is not None:
        from matchos.editorial import REASONS

        if reason.get("type") != "choice" or reason.get("choice") not in REASONS:
            raise ValueError("Invalid editorial reason")
        reason_code = reason["choice"]
    moment = available.get(selected)
    frame_scores = {}
    for candidate in candidates:
        frame_scores[candidate.frame] = max(frame_scores.get(candidate.frame, 0), scores.get(candidate.id, 0))
    return MomentDecision(
        editorial_reason=reason_code,
        selected_candidate_id=moment.id if moment else None,
        candidate_scores=scores,
        tactical_shift=sum(score for frame, score in frame_scores.items() if frame == "PRESS_BREAKDOWN"),
        momentum_shift=frame_scores.get("MOMENTUM_SWING", 0),
        player_story=max(frame_scores.get("PLAYER_IMPACT", 0), frame_scores.get("SUBSTITUTE_IMPACT", 0)),
        importance=importance if moment else 0,
        should_surface=surface if moment else 0,
        primary_frame=moment.frame if moment else candidates[0].frame,
        primary_frame_confidence=confidence,
        frame_scores=frame_scores,
        team_id=moment.team_id if moment else None,
        player_id=moment.player_id if moment else None,
        provider=provider,
        latency_ms=round(latency_ms, 3),
    )


def _bounded(value: object, upper: float = 1.0) -> float:
    if isinstance(value, bool):
        raise ValueError("Expected a numeric probability or score")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= upper:
        raise ValueError("Probability or score out of range")
    return number


def _parse_gateway(body: dict, provider: str, latency_ms: float) -> MomentDecision:
    answers = {answer["name"]: answer for answer in body["answers"]}
    for question in _questions():
        if answers[question["name"]]["type"] != question["type"]:
            raise ValueError("Missing, refused, or invalid decision answer")
    primary = answers["primaryFrame"]
    frame = primary["choice"]
    if frame not in FRAMES:
        raise ValueError("Unknown moment frame")
    scores = {item["value"]: _bounded(item["probability"]) for item in primary.get("probabilities", [])}
    if any(key not in FRAMES for key in scores):
        raise ValueError("Unknown moment frame in probabilities")
    confidence = _bounded(primary.get("confidence", scores.get(frame, 0.5)))
    scores.setdefault(frame, confidence)
    team = answers["team"]["choice"]
    if team not in ("RED", "BLUE", "NONE"):
        raise ValueError("Unknown team")
    return MomentDecision(
        tactical_shift=_bounded(answers["tacticalShift"]["probability"]),
        momentum_shift=_bounded(answers["momentumShift"]["probability"]),
        player_story=_bounded(answers["playerStory"]["probability"]),
        should_surface=_bounded(answers["shouldSurface"]["probability"]),
        importance=_bounded(answers["importance"]["score"], 5),
        primary_frame=frame,
        primary_frame_confidence=confidence,
        frame_scores=scores,
        team_id=None if team == "NONE" else team,
        provider=provider,
        latency_ms=round(latency_ms, 3),
    )


def selection_hold_reason(decision: MomentDecision) -> str | None:
    if decision.selected_candidate_id is None:
        return "Decision model chose no update."
    if decision.should_surface < SURFACE_THRESHOLD:
        from decimal import Decimal

        score = f"{(Decimal(str(decision.should_surface)) * 100).normalize():f}%"
        return (
            f"The model rated this story {score} for publication; at least 50% is required. "
            "Waiting for a stronger update."
        )
    return None
