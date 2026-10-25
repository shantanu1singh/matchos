"""Language selection must reach fallback copy, numeric checks and speech."""

import json
from xml.etree import ElementTree

import pytest
from test_speech import aired, configured

from matchos.copy import prose_is_faithful, render_overlay
from matchos.demo import demo_viewers
from matchos.language_agent import INSTRUCTIONS, faithful_text, prompt_payload
from matchos.schemas import VerifiedMoment, ViewerProfile
from matchos.speech import prepare_speech

FACTS = {
    "PRESS_BREAKDOWN": {
        "attackingTeam": "Liverpool",
        "defendingTeam": "Manchester City",
        "pressBypassBefore": 0.36,
        "pressBypassNow": 0.86,
        "defensiveLineDeltaM": -2.6,
        "opponentFinalThirdEntries": 17,
        "finalThirdEntriesBefore": 5,
    },
    "MOMENTUM_SWING": {
        "leadingTeam": "Liverpool",
        "possessionLast5": 0.64,
        "finalThirdEntriesBefore": 5,
        "finalThirdEntries": 17,
    },
    "GOAL": {"scoringTeam": "Liverpool", "scorer": "red_9", "minute": 65},
    "COMEBACK": {"scoringTeam": "Liverpool", "scorer": "red_9", "minute": 65},
    "RED_CARD": {"cardTeam": "Manchester City", "player": "blue_8", "minute": 68},
    "PLAYER_IMPACT": {"playerId": "red_8", "betweenLinesLast8": 5, "betweenLinesPrev30": 2},
    "SUBSTITUTE_IMPACT": {
        "player": "red_14",
        "minuteOn": 60,
        "progressiveActions": 5,
        "boxEntries": 2,
        "shots": 1,
    },
    "SHOT_CHANCE": {"team": "Liverpool", "player": "red_9", "outcome": "SAVED"},
}


def moment(frame):
    return VerifiedMoment(
        id="locale-test",
        type="TACTICAL_SHIFT",
        frame=frame,
        team_id="RED",
        importance=0.9,
        confidence=0.9,
        facts={"scoreRed": 2, "scoreBlue": 1, "playersRed": 11, "playersBlue": 10, **FACTS[frame]},
        evidence=[],
        evidence_detail=[],
        valid_from_ms=0,
        expires_at_ms=40000,
    )


@pytest.mark.parametrize("language", ["fr", "es"])
@pytest.mark.parametrize("persona", ["fan", "analyst", "broadcaster"])
@pytest.mark.parametrize("frame", list(FACTS))
def test_new_languages_cover_every_story_and_keep_numbers_grounded(language, persona, frame):
    story = moment(frame)
    viewer = ViewerProfile(language=language, persona=persona)
    overlay = render_overlay(story, viewer, rich=True, provider="test")
    assert overlay.language == language and overlay.headline and overlay.body
    assert prose_is_faithful(overlay.headline + " " + overlay.body, story)
    assert not any("\u0900" <= char <= "\u097f" for stat in overlay.stats for char in stat["label"])
    payload = json.loads(prompt_payload(story, viewer))
    assert payload["viewer"]["language"] == language and payload["fallbackText"] == overlay.body
    # A provider outage and an invented metric still give localized copy.
    assert faithful_text("9999%", story, viewer) == overlay.body


def test_decimal_commas_are_validated_without_letting_fractional_percentages_leak():
    story = moment("PRESS_BREAKDOWN")
    viewer = ViewerProfile(language="fr", persona="analyst")
    valid = "La ligne défensive a reculé de 2,6 mètres."
    assert faithful_text(valid, story, viewer) == valid
    assert not prose_is_faithful("La ligne a reculé de 999,9 mètres.", story)
    assert faithful_text("La possession est de 0,86.", story, viewer) != "La possession est de 0,86."
    viewer = ViewerProfile(language="es", persona="broadcaster")
    assert (
        faithful_text("La línea ha retrocedido 2,6 metros.", story, viewer)
        == render_overlay(story, viewer, rich=True, provider="test").body
    )


@pytest.mark.parametrize("language,locale", [("fr", "fr-FR"), ("es", "es-MX")])
@pytest.mark.parametrize("persona", ["casual", "player", "fan", "broadcaster", "analyst"])
def test_speech_uses_the_requested_language_and_preserves_approved_copy(language, locale, persona):
    result, viewer, _ = aired(
        persona, language=language, body="Liverpool mène." if language == "fr" else "Liverpool gana."
    )
    if persona == "player":
        result.overlay.player_id = viewer.player_id
    spec = prepare_speech(result, viewer, configured())
    assert spec.voice.startswith(locale + "-")
    assert spec.language == language
    root = ElementTree.fromstring(spec.ssml)
    assert root.attrib["{http://www.w3.org/XML/1998/namespace}lang"] == locale
    assert result.overlay.body in "".join(root.itertext())


def test_language_instruction_and_judge_default_are_explicit():
    assert "fr = French" in INSTRUCTIONS and "es = Spanish" in INSTRUCTIONS
    assert [(v.persona, v.language) for v in demo_viewers()] == [
        ("broadcaster", "en"),
        ("fan", "es"),
        ("analyst", "en"),
    ]
