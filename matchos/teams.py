"""Display identities for the synthetic match, independent of simulation slots.

2026/27 shirt numbers checked on 23 October 2026 against:
https://www.premierleague.com/en/news/4725682
This is a demo selection, not an actual matchday starting XI.
"""

TEAMS = {
    "RED": {"name": "Liverpool", "short_name": "Liverpool", "code": "LIV", "color": "#e94c55"},
    "BLUE": {"name": "Manchester City", "short_name": "Man City", "code": "MCI", "color": "#8cc8eb"},
}

# The suffix is a stable simulation slot, never a shirt number.
_SQUADS = {
    "red": [
        (1, "Alisson Becker", "Alisson", 1),
        (2, "Jeremie Frimpong", "Frimpong", 30),
        (3, "Milos Kerkez", "Kerkez", 6),
        (4, "Virgil van Dijk", "Van Dijk", 4),
        (5, "Jeremy Jacquet", "Jacquet", 5),
        (6, "Ryan Gravenberch", "Gravenberch", 38),
        (7, "Federico Chiesa", "Chiesa", 14),
        (8, "Dominik Szoboszlai", "Szoboszlai", 8),
        (9, "Alexander Isak", "Isak", 9),
        (10, "Alexis Mac Allister", "Mac Allister", 10),
        (11, "Cody Gakpo", "Gakpo", 18),
        (17, "Bradley Barcola", "Barcola", 29),
    ],
    "blue": [
        (1, "Gianluigi Donnarumma", "Donnarumma", 1),
        (2, "Matheus Nunes", "Nunes", 27),
        (3, "Rayan Ait-Nouri", "Ait-Nouri", 21),
        (4, "Ruben Dias", "Dias", 3),
        (5, "Josko Gvardiol", "Gvardiol", 24),
        (6, "Elliot Anderson", "Anderson", 5),
        (7, "Rayan Cherki", "Cherki", 10),
        (8, "Mateo Kovacic", "Kovacic", 8),
        (9, "Erling Haaland", "Haaland", 9),
        (10, "Phil Foden", "Foden", 47),
        (11, "Jeremy Doku", "Doku", 11),
    ],
}
PLAYERS = {
    f"{team}_{slot}": {"team_id": team.upper(), "name": name, "short_name": short, "number": number}
    for team, squad in _SQUADS.items()
    for slot, name, short, number in squad
}


def team_name(team: str) -> str:
    return TEAMS.get(team.upper(), {}).get("name", team)


def shirt_number(player_id: str, fallback: int = 0) -> int:
    return PLAYERS.get(player_id, {}).get("number", fallback)


def player_label(player_id: str | None) -> str:
    player = PLAYERS.get(player_id)
    if player:
        return f"{player['name']} #{player['number']}"
    if not player_id or "_" not in player_id:
        return ""
    team, number = player_id.split("_", 1)
    return f"{team_name(team)} {number}"


def identity_context() -> dict:
    return {
        "teams": TEAMS,
        "players": PLAYERS,
        "note": "Synthetic match. Player IDs are opaque simulation slots; use the roster names and shirt numbers.",
    }
