"""Fact-locked copy. Templates never invent a number that is not in the moment."""

from __future__ import annotations

import re

from matchos.localization import localize_stat_labels, localized_copy
from matchos.pitch import shirt
from matchos.schemas import Overlay, VerifiedMoment, ViewerProfile
from matchos.teams import shirt_number, team_name

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _pct(value: object) -> str:
    return str(round(float(value) * 100))


def _num(value: object, digits: int = 1) -> str:
    number = float(value)
    if digits == 0:
        return str(int(round(number)))
    text = f"{number:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _side(value: object, language: str) -> str:
    raw = str(value)
    name = team_name(raw)
    if language == "hi":
        return {"Liverpool": "लिवरपूल", "Manchester City": "मैनचेस्टर सिटी"}.get(name, name)
    return name


def _player_label(facts: dict, language: str) -> str:
    player_id = str(facts.get("playerId") or facts.get("player") or "")
    if "_" in player_id:
        return shirt(player_id)
    return str(facts.get("player") or "")


def render_overlay(moment: VerifiedMoment, viewer: ViewerProfile, *, rich: bool, provider: str) -> Overlay:
    facts = moment.facts
    language = viewer.language
    persona = viewer.persona
    kicker, headline, body = _copy(moment, viewer, rich)
    if rich and language in {"en", "hi"} and persona in {"fan", "analyst"} and moment.frame == "SHOT_CHANCE":
        team = _side(facts["team"], language)
        if persona == "fan":
            headline = f"{team} का मौका" if language == "hi" else f"Chance for {team}"
        else:
            headline = "शॉट का नतीजा" if language == "hi" else "Shot outcome"
    stats = localize_stat_labels(_stats(moment, "en" if language in {"fr", "es"} else language), language)
    graphic = None
    if persona == "broadcaster":
        graphic = {
            "frame": moment.frame,
            "confidence": round(moment.confidence, 2),
            "headline": headline,
            "supporting": stats,
            "ttl_sec": max(0, (moment.expires_at_ms - moment.valid_from_ms) // 1000),
            "priority": "HIGH" if moment.importance >= 0.8 else "MEDIUM",
            "on_air": True,
        }
    source = "agent" if rich else "template"
    return Overlay(
        moment_id=moment.id,
        persona=persona,
        language=language,
        team_id=viewer.team_id,
        player_id=viewer.player_id if persona == "player" else moment.player_id,
        kicker=kicker,
        headline=headline,
        body=body,
        stats=stats,
        graphic=graphic,
        source=source,
        provider=provider if rich else "template",
    )


def prose_is_faithful(text: str, moment: VerifiedMoment) -> bool:
    allowed = _allowed_numbers(moment)
    for token in _NUMBER.findall(text):
        value = float(token.replace(",", "."))
        if not any(abs(value - candidate) <= 0.15 for candidate in allowed):
            return False
    return True


def _allowed_numbers(moment: VerifiedMoment) -> set[float]:
    allowed: set[float] = {0.0, 40.0, 5.0, 8.0, 30.0}
    allowed.add(float(moment.minute) if hasattr(moment, "minute") else 0.0)
    for value in moment.facts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            allowed.add(round(number, 2))
            allowed.add(float(round(number)))
            allowed.add(abs(number))
            allowed.add(float(round(abs(number))))
            allowed.add(float(round(abs(number), 1)))
            if 0.0 <= abs(number) <= 1.5:
                pct = abs(number) * 100
                allowed.add(float(round(pct)))
                allowed.add(round(pct, 1))
        elif isinstance(value, str) and "_" in value:
            try:
                allowed.add(float(shirt_number(value, int(value.split("_", 1)[1]))))
            except ValueError:
                continue
    if moment.player_id and "_" in moment.player_id:
        try:
            allowed.add(float(shirt_number(moment.player_id, int(moment.player_id.split("_", 1)[1]))))
        except ValueError:
            pass
    for item in moment.evidence_detail:
        allowed.add(float(item.minute))
        allowed.add(float(item.second))
    return allowed


def _stats(moment: VerifiedMoment, language: str) -> list[dict[str, str]]:
    facts = moment.facts
    if moment.frame == "PRESS_BREAKDOWN":
        drop = float(facts.get("defensiveLineDeltaM", 0))
        return [
            {
                "label": "Press bypass" if language == "en" else "प्रेस बाईपास",
                "value": f"{_pct(facts['pressBypassBefore'])}% → {_pct(facts['pressBypassNow'])}%",
            },
            {
                "label": "Final third" if language == "en" else "फाइनल थर्ड",
                "value": f"{facts['finalThirdEntriesBefore']} → {facts['opponentFinalThirdEntries']}",
            },
            {"label": "Defensive line" if language == "en" else "डिफेंसिव लाइन", "value": f"{drop:+.1f} m"},
        ]
    if moment.frame in {"PLAYER_IMPACT"}:
        return [
            {
                "label": "Between the lines" if language == "en" else "लाइनों के बीच",
                "value": f"{facts['betweenLinesLast8']} / 8'",
            },
            {
                "label": "Previous 30'" if language == "en" else "पिछले 30'",
                "value": str(facts["betweenLinesPrev30"]),
            },
        ]
    if moment.frame == "SUBSTITUTE_IMPACT":
        return [
            {
                "label": "Progressive" if language == "en" else "प्रोग्रेसिव",
                "value": str(facts["progressiveActions"]),
            },
            {"label": "Box entries" if language == "en" else "बॉक्स एंट्री", "value": str(facts["boxEntries"])},
            {"label": "Shots" if language == "en" else "शॉट", "value": str(facts["shots"])},
        ]
    if moment.frame == "MOMENTUM_SWING":
        return [
            {
                "label": "Possession" if language == "en" else "कब्ज़ा",
                "value": f"{_pct(facts['possessionLast5'])}%",
            },
            {
                "label": "Final third" if language == "en" else "फाइनल थर्ड",
                "value": f"{facts['finalThirdEntriesBefore']} → {facts['finalThirdEntries']}",
            },
        ]
    if moment.frame in {"GOAL", "COMEBACK"}:
        return [
            {
                "label": "Score" if language == "en" else "स्कोर",
                "value": f"{facts['scoreRed']}–{facts['scoreBlue']}",
            },
            {"label": "Minute" if language == "en" else "मिनट", "value": f"{facts['minute']}'"},
        ]
    if moment.frame == "RED_CARD":
        stats = [
            {"label": "Card" if language == "en" else "कार्ड", "value": shirt(str(facts.get("player") or ""))}
        ]
        if "playersRed" in facts:
            stats.append(
                {
                    "label": "Players LIV–MCI" if language == "en" else "खिलाड़ी LIV–MCI",
                    "value": f"{facts['playersRed']}–{facts['playersBlue']}",
                }
            )
        return stats
    if moment.frame == "SHOT_CHANCE":
        return [{"label": "Shot" if language == "en" else "शॉट", "value": str(facts.get("outcome") or "")}]
    return []


def _copy(moment: VerifiedMoment, viewer: ViewerProfile, rich: bool) -> tuple[str, str, str]:
    if viewer.language in {"fr", "es"}:
        return localized_copy(moment, viewer, rich=rich)
    language = viewer.language
    persona = viewer.persona
    frame = moment.frame
    if persona == "broadcaster":
        return _broadcast(moment, viewer)
    if frame == "PRESS_BREAKDOWN":
        return _press(moment, viewer, rich)
    if frame == "PLAYER_IMPACT":
        return _player(moment, language, persona, rich)
    if frame == "SUBSTITUTE_IMPACT":
        return _sub(moment, language, persona, rich)
    if frame == "MOMENTUM_SWING":
        return _momentum(moment, language, persona, rich)
    if frame in {"GOAL", "COMEBACK"}:
        return _goal(moment, viewer, rich)
    if frame == "RED_CARD":
        return _card(moment, viewer, rich)
    return _shot(moment, language, persona, rich)


def _broadcast(moment: VerifiedMoment, viewer: ViewerProfile) -> tuple[str, str, str]:
    """A match story above one supporting fact; exact metrics stay in evidence."""
    facts, language = moment.facts, viewer.language
    hindi = language == "hi"
    score = (
        f"{facts['scoreRed']}–{facts['scoreBlue']}" if "scoreRed" in facts and "scoreBlue" in facts else ""
    )
    context = (f"{score} पर, " if hindi else f"At {score}, ") if score else ""
    kicker = "ऑन एयर" if hindi else "On air"
    if moment.frame == "PRESS_BREAKDOWN":
        attacking, defending = (_side(facts[key], language) for key in ("attackingTeam", "defendingTeam"))
        before, now = _pct(facts["pressBypassBefore"]), _pct(facts["pressBypassNow"])
        if hindi:
            return (
                kicker,
                f"{context}{attacking} {defending} की प्रेस के पार निकल रहे हैं",
                f"हाल की कोशिशों में पहली प्रेस लाइन को भेदने की दर {before}% से बढ़कर {now}% हुई है।",
            )
        return (
            kicker,
            f"{context}{attacking} are getting through {defending}'s press",
            f"{attacking} have played through the first line on {now}% of recent attempts, up from {before}%.",
        )
    if moment.frame == "MOMENTUM_SWING":
        team, share = _side(facts["leadingTeam"], language), _pct(facts["possessionLast5"])
        if hindi:
            return (
                kicker,
                f"{context}{team} का दबाव बढ़ रहा है",
                f"पिछले पाँच मिनट में गेंद पर उनका {share}% कब्ज़ा रहा है।",
            )
        return (
            kicker,
            f"{context}{team} are building pressure",
            f"They have held {share}% of the ball over the last five minutes.",
        )
    if moment.frame in {"GOAL", "COMEBACK"}:
        team = _side(facts["scoringTeam"], language)
        scorer = shirt(str(facts.get("scorer") or "")) or team
        red, blue = int(facts["scoreRed"]), int(facts["scoreBlue"])
        own, other = (red, blue) if team_name(str(facts["scoringTeam"])) == team_name("RED") else (blue, red)
        if hindi:
            headline = f"{team} ने {score} पर बराबरी की" if own == other else f"{team} का गोल, स्कोर {score}"
            return kicker, headline, f"{scorer} ने {facts['minute']}वें मिनट में गोल किया।"
        headline = (
            f"{team} draw level at {score}"
            if own == other
            else f"{team} take a {score} lead"
            if own > other and own - other == 1
            else f"{team} extend their lead to {score}"
            if own > other
            else f"{team} pull a goal back: {score}"
        )
        return kicker, headline, f"{scorer} scores at {facts['minute']}′."
    if moment.frame == "RED_CARD":
        team = _side(facts["cardTeam"], language)
        side = "Red" if team_name(str(facts["cardTeam"])) == team_name("RED") else "Blue"
        remaining = int(facts.get(f"players{side}", 10))
        player = shirt(str(facts.get("player") or "")) or team
        if hindi:
            return (
                kicker,
                f"{team} अब {remaining} खिलाड़ियों से खेलेंगे",
                f"{player} को लाल कार्ड मिला। {team} के पास अब {remaining} खिलाड़ी हैं।",
            )
        return (
            kicker,
            f"{team} down to {remaining}",
            f"{player} is sent off, leaving {team} with {remaining} players.",
        )
    if moment.frame == "PLAYER_IMPACT":
        player = _player_label(facts, language)
        if hindi:
            return (
                kicker,
                f"{context}{player} खेल में ज़्यादा शामिल हो रहे हैं",
                f"पिछले आठ मिनट में उन्हें लाइनों के बीच {facts['betweenLinesLast8']} बार गेंद मिली है।",
            )
        return (
            kicker,
            f"{context}{player} is getting more involved",
            f"He has received the ball between the lines {facts['betweenLinesLast8']} times in the last eight minutes.",
        )
    if moment.frame == "SUBSTITUTE_IMPACT":
        player = _player_label(facts, language)
        if hindi:
            return (
                kicker,
                f"{context}{player} के आने के बाद आगे बढ़ती टीम",
                f"मैदान पर आने के बाद उन्होंने {facts['boxEntries']} बार गेंद बॉक्स में पहुँचाई है।",
            )
        return (
            kicker,
            f"{context}{player} is involved after coming on",
            f"He has contributed {facts['boxEntries']} box entries since coming on.",
        )
    _, _, body = _shot(moment, language, "broadcaster", True)
    team = _side(facts["team"], language)
    headline = f"{context}{team} का शॉट" if hindi else f"{context}{team} have a shot"
    return kicker, headline, body


def _press(moment: VerifiedMoment, viewer: ViewerProfile, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    language = viewer.language
    defending = _side(facts["defendingTeam"], language)
    attacking = _side(facts["attackingTeam"], language)
    before = _pct(facts["pressBypassBefore"])
    now = _pct(facts["pressBypassNow"])
    drop = float(facts["defensiveLineDeltaM"])
    drop_abs = _num(abs(drop))
    entries = facts["opponentFinalThirdEntries"]
    entries_before = facts["finalThirdEntriesBefore"]
    allegiance = _side(viewer.team_id, language)
    if not rich:
        if language == "hi":
            body = f"{defending} की प्रेस को अब ज़्यादा बार बाईपास किया जा रहा है। विपक्ष हाल की कोशिशों में से {now}% पर पहली लाइन के पार निकल गया।"
            return "टैक्टिकल", f"{defending} की प्रेस टूट रही है", body
        body = f"{defending}'s press is being bypassed more frequently. The opposition has played through the first line on {now}% of attempts recently."
        return "Tactical", f"{defending}'s press is being bypassed", body
    persona = viewer.persona
    if language == "hi":
        if persona == "analyst":
            line = f" उनकी डिफेंसिव लाइन {drop_abs} मीटर पीछे गई है।" if drop < -1 else ""
            body = f"{defending} की फर्स्ट-लाइन बाईपास दर पिछले पाँच मिनट में {before}% से {now}% हो गई है।{line}"
            return "विश्लेषक", "प्रेस का असर गिरा", body
        if persona == "fan":
            if team_name(viewer.team_id) == team_name(str(facts["defendingTeam"])):
                body = f"{defending} दबाव में हैं। {attacking} पहली प्रेस लाइन को भेदकर उन्हें अपने गोल की ओर धकेल रहे हैं।"
            else:
                body = f"{attacking} अब {defending} की पहली प्रेस लाइन के पार से खेल पा रहे हैं।"
            return "सपोर्टर", f"{allegiance} का पल", body
        if persona == "player":
            body = f"प्रेस के पीछे जगह खुल रही है। {attacking} की एंट्री {entries_before} से {entries} हो गई हैं।"
            return "खिलाड़ी", "लाइनों के बीच जगह", body
        body = (
            f"खेल बदल रहा है। {attacking} को ज़्यादा जगह मिल रही है और {defending} गहराई में धकेले जा रहे हैं।"
            if drop < -1
            else f"खेल बदल रहा है। {attacking} को {defending} की प्रेस के पार निकलना आसान लग रहा है।"
        )
        return "मैच", "खेल बदल रहा है", body
    if persona == "analyst":
        line = f", while their defensive line has dropped {drop_abs}m" if drop < -1 else ""
        body = f"{defending}'s first-line press bypass rate has risen from {before}% to {now}% over the last five minutes{line}."
        return "Analyst", "Press effectiveness down", body
    if persona == "fan":
        if team_name(viewer.team_id) == team_name(str(facts["defendingTeam"])):
            body = f"{defending} are under pressure. {attacking} are increasingly getting through the first press and forcing them toward their own goal."
            headline = f"{defending} under pressure"
        else:
            body = f"{attacking} are playing through {defending}'s first press and finding more of the pitch."
            headline = f"{attacking} in control of the press"
        return "Supporter", headline, body
    if persona == "player":
        body = f"Space is opening behind the first line. {attacking} final-third entries have gone from {entries_before} to {entries}."
        return "Player", "Space between the lines", body
    if drop < -1:
        body = (
            f"The game is changing. {attacking} are finding much more space and pushing {defending} deeper."
        )
    else:
        body = f"The game is changing. {attacking} are finding it much easier to play through {defending}."
    return "Match", "The game is changing", body


def _player(moment: VerifiedMoment, language: str, persona: str, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    label = _player_label(facts, language)
    recent = facts["betweenLinesLast8"]
    prev = facts["betweenLinesPrev30"]
    if language == "hi":
        body = (
            f"{label} को पिछले 8 मिनट में लाइनों के बीच {recent} बार गेंद मिली है, पिछले 30 मिनट में {prev} बार के मुकाबले।"
        )
        return ("खिलाड़ी", f"{label} का असर बढ़ रहा है", body)
    body = f"{label} has received between the lines {recent} times in the last eight minutes, compared with {prev} in the previous 30."
    headline = (
        f"{label} influencing the game more" if rich or persona == "player" else f"{label} between the lines"
    )
    kicker = "Player" if persona != "broadcaster" else "On air"
    return kicker, headline, body


def _sub(moment: VerifiedMoment, language: str, persona: str, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    label = _player_label(facts, language)
    actions = facts["progressiveActions"]
    boxes = facts["boxEntries"]
    shots = facts["shots"]
    minute = facts["minuteOn"]
    if language == "hi":
        body = f"{label} {minute}' पर आए और तब से {actions} प्रोग्रेसिव एक्शन, {boxes} बॉक्स एंट्री और {shots} शॉट दे चुके हैं।"
        return "बदलाव", "सब्स्टीट्यूट मैच बदल रहा है", body
    body = f"{label} came on at {minute}' and has since produced {actions} progressive actions, {boxes} box entries and {shots} shots."
    return "Substitute", "The substitute is changing the match", body


def _momentum(moment: VerifiedMoment, language: str, persona: str, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    leading = _side(facts["leadingTeam"], language)
    share = _pct(facts["possessionLast5"])
    if language == "hi":
        if persona == "analyst" or not rich:
            body = f"{leading} के पास पिछले पाँच मिनट का {share}% कब्ज़ा है और फाइनल थर्ड एंट्री {facts['finalThirdEntriesBefore']} से {facts['finalThirdEntries']} हो गई हैं।"
        else:
            body = f"{leading} मैच को अपनी तरफ खींच रहे हैं और बार-बार विपक्ष के आखिरी तीसरे में पहुँच रहे हैं।"
        return "मोमेंटम", f"{leading} का पल", body
    if persona == "analyst" or not rich:
        body = f"{leading} have had {share}% of the ball over the last five minutes, with final-third entries from {facts['finalThirdEntriesBefore']} to {facts['finalThirdEntries']}."
    else:
        body = f"{leading} are dragging the game their way and reaching the final third much more often."
    return "Momentum", f"{leading} have the momentum", body


def _goal(moment: VerifiedMoment, viewer: ViewerProfile, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    language, persona = viewer.language, viewer.persona
    team = _side(facts["scoringTeam"], language)
    scorer = shirt(str(facts.get("scorer") or "")) or team
    score = f"{facts['scoreRed']}–{facts['scoreBlue']}"
    if language == "hi":
        headline = "वापसी पूरी हुई" if moment.frame == "COMEBACK" else f"{team} का गोल"
        return "गोल", headline, f"{scorer} ने गोल किया। स्कोर अब {score} है।"
    red, blue = int(facts["scoreRed"]), int(facts["scoreBlue"])
    leader = _side("RED" if red > blue else "BLUE", language)
    score_line = f"Liverpool {score} Manchester City"
    if persona == "analyst":
        body = f"{scorer} scores at {facts['minute']}′. {score_line}."
        if "playersRed" in facts:
            body += f" Players on pitch: {facts['playersRed']}–{facts['playersBlue']}."
        return "Analyst", "Score update", body
    if persona == "fan":
        support = _side(viewer.team_id, language)
        own = red if viewer.team_id == "RED" else blue
        opponent = blue if viewer.team_id == "RED" else red
        position = "lead" if own > opponent else "trail" if own < opponent else "are level"
        return "Supporter", f"A goal for {team}", f"{scorer} scores! {support} {position} at {score}."
    headline = "Comeback goal" if moment.frame == "COMEBACK" else f"Goal for {team}"
    return (
        "Goal",
        headline,
        f"{scorer} scores. {leader + ' lead' if red != blue else 'The teams are level'} at {score}.",
    )


def _card(moment: VerifiedMoment, viewer: ViewerProfile, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    language, persona = viewer.language, viewer.persona
    team = _side(facts["cardTeam"], language)
    card_is_red_team = team_name(str(facts["cardTeam"])) == team_name("RED")
    remaining = int(facts.get("playersRed" if card_is_red_team else "playersBlue", 10))
    other_count = int(facts.get("playersBlue" if card_is_red_team else "playersRed", 11))
    other_team = _side("BLUE" if card_is_red_team else "RED", language)
    player = shirt(str(facts.get("player") or "")) or team
    if language == "hi":
        return (
            "कार्ड",
            f"{team} अब {remaining} से",
            f"{player} को लाल कार्ड मिला। {team} अब {remaining} खिलाड़ियों से खेलेंगे।",
        )
    if persona == "analyst":
        return (
            "Analyst",
            f"Dismissal: {team}",
            f"{player} is sent off at {facts['minute']}′. {team} now have {remaining} players against {other_team}'s {other_count}.",
        )
    if persona == "fan":
        support = _side(viewer.team_id, language)
        is_own = team_name(viewer.team_id) == team_name(str(facts["cardTeam"]))
        headline = (
            f"{team} down to {remaining}"
            if is_own or other_count <= remaining
            else f"{support} have a player advantage"
        )
        return (
            "Supporter",
            headline,
            f"{team} lose {player} and drop to {remaining}; {other_team} have {other_count}.",
        )
    return "Discipline", "Red card", f"{player} is sent off. {team} will play with {remaining}."


def _shot(moment: VerifiedMoment, language: str, persona: str, rich: bool) -> tuple[str, str, str]:
    facts = moment.facts
    team = _side(facts["team"], language)
    player = shirt(str(facts.get("player") or "")) or team
    outcome = str(facts.get("outcome") or "").replace("_", " ").lower()
    if language == "hi":
        return "मौका", "शॉट", f"{player} का शॉट {outcome} रहा।"
    return "Chance", "Shot", f"{player}'s shot is {outcome}."
