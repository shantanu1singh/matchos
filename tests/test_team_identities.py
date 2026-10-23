"""Club identities stay consistent across simulation, evidence, and model inputs."""
import json

from fastapi.testclient import TestClient

from matchos import server
from matchos.copy import prose_is_faithful, render_overlay
from matchos.decision_client import _editorial_state
from matchos.language_agent import prompt_payload
from matchos.pitch import event_label
from matchos.schemas import ViewerProfile
from matchos.simulator import MatchSimulator
from matchos.state_engine import StateEngine
from matchos.teams import PLAYERS, TEAMS
from test_editorial_selection import setup_candidates


def test_tracking_and_substitute_keep_slot_ids_with_actual_shirt_numbers():
    sim = MatchSimulator(seed=7, scenario_id="balanced")
    engine = StateEngine()
    tick = sim.step(100)
    assert len(tick.frame.players) == 22
    for row in tick.frame.players:
        assert row.number == PLAYERS[row.player_id]["number"]
        assert row.name == PLAYERS[row.player_id]["name"]
    for team in TEAMS:
        numbers = [p.number for p in tick.frame.players if p.team_id == team]
        assert len(set(numbers)) == 11
    assert sim.players["red_2"].number == 30  # Frimpong, not slot 2
    assert sim.players["blue_10"].number == 47  # Foden, not slot 10
    events = sim._substitute("red_7", "red_17")
    assert sim.players["red_17"].number == 29
    assert not sim.players["red_7"].on_pitch
    assert "Federico Chiesa #14 off, Bradley Barcola #29 on" == event_label(events[0])
    tick = sim.step(100)
    tick.events = events + tick.events
    state = engine.ingest(tick)
    assert state.players["red_17"].number == 29
    assert next(p for p in tick.frame.players if p.player_id == "red_17").name == "Bradley Barcola"


def test_verified_copy_and_both_model_contexts_use_club_and_player_identities():
    engine, candidates = setup_candidates()
    moment = next(c for c in candidates if c.frame == "MOMENTUM_SWING")
    assert moment.facts["leadingTeam"] == "Liverpool"
    assert moment.facts["trailingTeam"] == "Manchester City"
    viewer = ViewerProfile(persona="analyst")
    overlay = render_overlay(moment, viewer, rich=True, provider="test")
    assert "Liverpool" in overlay.headline + overlay.body
    for payload in (_editorial_state(engine.state, candidates, viewer, {}), json.loads(prompt_payload(moment, viewer))):
        assert payload["roster"]["teams"]["BLUE"]["name"] == "Manchester City"
        assert payload["roster"]["players"]["red_2"]["number"] == 30
    player = next(c for c in candidates if c.frame == "PLAYER_IMPACT")
    assert prose_is_faithful("Erling Haaland #9", player)
    player = player.model_copy(update={"player_id": "red_2", "facts": {"playerId": "red_2"}})
    assert prose_is_faithful("Jeremie Frimpong #30", player)
    assert not prose_is_faithful("Jeremie Frimpong #99", player)

    press = moment.model_copy(update={"frame": "PRESS_BREAKDOWN", "facts": {
        "defendingTeam": "Manchester City", "attackingTeam": "Liverpool",
        "pressBypassBefore": .2, "pressBypassNow": .8, "defensiveLineDeltaM": -4,
        "opponentFinalThirdEntries": 10, "finalThirdEntriesBefore": 2,
    }})
    fan = render_overlay(press, ViewerProfile(persona="fan", team_id="BLUE"), rich=True, provider="test")
    assert fan.headline == "Manchester City under pressure"
    assert "Liverpool" in fan.body


def test_websocket_provides_complete_identity_manifest(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "local")
    monkeypatch.setenv("REASONING_MODEL", "local")
    with TestClient(server.app) as client, client.websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["roster"]["teams"]["RED"]["name"] == "Liverpool"
        assert hello["roster"]["players"]["blue_9"]["name"] == "Erling Haaland"
        assert hello["roster"]["players"]["red_17"]["number"] == 29
