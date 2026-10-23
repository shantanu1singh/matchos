"""Broadcast copy must read as a match cue, with separate supporting evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from matchos.copy import prose_is_faithful, render_overlay
from matchos.language_agent import faithful_text, final_overlay, prompt_payload
from matchos.schemas import PipelineResult, VerifiedMoment, ViewerProfile
from matchos.session import MatchSession
from matchos.telemetry import Telemetry


@pytest.fixture
def press():
    return VerifiedMoment(id="broadcast-press", type="TACTICAL_SHIFT", frame="PRESS_BREAKDOWN", team_id="BLUE",
        importance=.9, confidence=.9, facts={
            "defendingTeam": "Manchester City", "attackingTeam": "Liverpool",
            "pressBypassBefore": .36, "pressBypassNow": .88, "pressBypassSample": 8,
            "defensiveLineBeforeM": 32.49, "defensiveLineNowM": 29.4, "defensiveLineDeltaM": -3.09,
            "opponentFinalThirdEntries": 12, "finalThirdEntriesBefore": 2, "scoreRed": 1, "scoreBlue": 1,
        }, evidence=[], evidence_detail=[], valid_from_ms=0, expires_at_ms=40000)


@pytest.mark.parametrize("language", ["en", "hi"])
@pytest.mark.parametrize("rich", [True, False])
def test_broadcast_fallback_has_story_then_evidence(press, language, rich):
    overlay = render_overlay(press, ViewerProfile(persona="broadcaster", language=language), rich=rich, provider="test")
    assert "1–1" in overlay.headline and "36%" not in overlay.headline and "88%" not in overlay.headline
    assert "36%" in overlay.body and "88%" in overlay.body
    assert overlay.headline != overlay.body
    assert overlay.graphic["headline"] == overlay.headline
    assert prose_is_faithful(overlay.headline + " " + overlay.body, press)
    if language == "en":
        assert "Liverpool are getting through Manchester City's press" in overlay.headline


def test_only_broadcast_prompt_rounds_distances_and_supplies_the_headline(press):
    broadcast = json.loads(prompt_payload(press, ViewerProfile(persona="broadcaster")))
    analyst = json.loads(prompt_payload(press, ViewerProfile(persona="analyst")))
    assert broadcast["moment"]["facts"]["defensiveLineBeforeM"] == 32
    assert broadcast["moment"]["facts"]["defensiveLineNowM"] == 29
    assert broadcast["moment"]["facts"]["defensiveLineDeltaM"] == -3
    assert "1–1" in broadcast["presentation"]["headline"]
    assert "Supporting copy only" in broadcast["presentation"]["output"]
    assert "presentation" not in analyst
    assert analyst["moment"]["facts"]["defensiveLineBeforeM"] == press.facts["defensiveLineBeforeM"] == 32.49
    assert broadcast["moment"]["facts"]["pressBypassNow"] == "88%"
    overlay = render_overlay(press, ViewerProfile(persona="broadcaster"), rich=True, provider="test")
    assert overlay.stats[2]["value"] == "-3.1 m"  # Detailed evidence keeps its precision.


@pytest.mark.parametrize("bad_copy", ["duplicate", "Manchester City are defending 3.09 metres deeper.",
    "Liverpool completed 999 passes.", "Liverpool have the ball. City are defending deeper. The match is level.",
    "Liverpool " * 46])
def test_broadcaster_rejects_repeated_titles_dense_copy_and_invented_numbers(press, bad_copy):
    viewer = ViewerProfile(persona="broadcaster")
    fallback = render_overlay(press, viewer, rich=True, provider="template")
    text = fallback.headline if bad_copy == "duplicate" else bad_copy
    assert faithful_text(text, press, viewer) == fallback.body


def test_rounding_a_negative_line_delta_can_be_explained_as_a_positive_distance(press):
    text = "Manchester City are defending around 3 metres deeper."
    assert faithful_text(text, press, ViewerProfile(persona="broadcaster")) == text


def test_final_publication_and_reframe_never_replace_headline_with_supporting_copy(press):
    support = "Liverpool have played through the first line on 88% of recent attempts."
    class Agent:
        async def run(self, prompt):
            payload = json.loads(prompt)
            assert payload["presentation"]["headline"] != support
            return SimpleNamespace(text=support)

    viewer = ViewerProfile(persona="broadcaster")
    expected = render_overlay(press, viewer, rich=True, provider="test")
    async def run():
        overlay = await final_overlay(Agent(), press, viewer, "test")
        # Exercise the legacy reframe entry point as well as normal live publication.
        session = SimpleNamespace(active=PipelineResult(kind="moment", moment=press), viewer=viewer,
            services=SimpleNamespace(agent=Agent(), language_provider="test"), telemetry=Telemetry())
        session.reframe_template = lambda: MatchSession.reframe_template(session)
        reframed = await MatchSession.reframe(session)
        for card in (overlay, reframed):
            assert card.body == support
            assert card.headline == card.graphic["headline"] == expected.headline
            assert card.headline != card.body
    asyncio.run(run())


@pytest.mark.parametrize("score,team,expected", [((2, 1), "Liverpool", "take a 2–1 lead"),
    ((1, 1), "Liverpool", "draw level at 1–1"), ((3, 1), "Liverpool", "extend their lead to 3–1"),
    ((2, 1), "Manchester City", "pull a goal back: 2–1")])
def test_goal_cue_uses_the_actual_score_situation(press, score, team, expected):
    moment = press.model_copy(update={"frame": "GOAL", "facts": {
        "scoringTeam": team, "scoreRed": score[0], "scoreBlue": score[1], "minute": 65,
        "scorer": "red_9" if team == "Liverpool" else "blue_9"}})
    overlay = render_overlay(moment, ViewerProfile(persona="broadcaster"), rich=True, provider="test")
    assert expected in overlay.headline
    assert "65′" in overlay.body and "scores" in overlay.body
    assert prose_is_faithful(overlay.headline + " " + overlay.body, moment)
