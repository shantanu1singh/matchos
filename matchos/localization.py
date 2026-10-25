"""French and Spanish fact-locked presentation, including offline fallback.

Only canonical facts enter these templates. Provider-generated supporting copy
still passes the same numeric guard as English and Hindi.
"""
from matchos.pitch import shirt
from matchos.teams import team_name


_LABELS = {
    "fr": dict(zip(
        ("Press bypass", "Final third", "Defensive line", "Between the lines", "Previous 30'", "Progressive", "Box entries", "Shots", "Possession", "Score", "Minute", "Card", "Players LIV–MCI", "Shot"),
        ("Pressing franchi", "Dernier tiers", "Ligne défensive", "Entre les lignes", "30 min précédentes", "Actions progressives", "Entrées dans la surface", "Tirs", "Possession", "Score", "Minute", "Carton", "Joueurs LIV–MCI", "Tir"))),
    "es": dict(zip(
        ("Press bypass", "Final third", "Defensive line", "Between the lines", "Previous 30'", "Progressive", "Box entries", "Shots", "Possession", "Score", "Minute", "Card", "Players LIV–MCI", "Shot"),
        ("Presión superada", "Último tercio", "Línea defensiva", "Entre líneas", "30 min anteriores", "Acciones progresivas", "Entradas al área", "Tiros", "Posesión", "Marcador", "Minuto", "Tarjeta", "Jugadores LIV–MCI", "Tiro"))),
}


def localize_stat_labels(stats, language):
    labels = _LABELS.get(language, {})
    return [{**stat, "label": labels.get(stat["label"], stat["label"])} for stat in stats]


def localized_copy(moment, viewer, *, rich):
    """Localized title and useful fallback body for every supported event frame."""
    f, fr, role = moment.facts, viewer.language == "fr", viewer.persona
    side = lambda key: team_name(str(f[key]))
    pct = lambda key: round(float(f[key]) * 100)
    player = shirt(str(f.get("playerId") or f.get("player") or ""))
    score = f"{f['scoreRed']}–{f['scoreBlue']}" if "scoreRed" in f and "scoreBlue" in f else ""
    prefix = (f"À {score}, " if fr else f"Con {score}, ") if score else ""
    kicker = ({"broadcaster": "En direct", "analyst": "Analyse", "fan": "Supporter", "player": "Joueur"} if fr else
              {"broadcaster": "En directo", "analyst": "Análisis", "fan": "Afición", "player": "Jugador"}).get(role, "Match" if fr else "Partido")
    frame = moment.frame
    if frame == "PRESS_BREAKDOWN":
        attacking, defending = side("attackingTeam"), side("defendingTeam")
        before, now = pct("pressBypassBefore"), pct("pressBypassNow")
        title = (f"{prefix}{attacking} franchit le pressing de {defending}" if fr else
                 f"{prefix}{attacking} supera la presión de {defending}")
        body = (f"Le taux de franchissement du premier rideau est passé de {before} % à {now} %." if fr else
                f"La tasa de superación de la primera línea ha pasado del {before}% al {now}%.")
        if role == "analyst":
            title = "Un pressing moins efficace" if fr else "La presión pierde eficacia"
            line = f["defensiveLineDeltaM"]
            body += (f" Variation de la ligne défensive de {defending} : {line:+.1f} m." if fr else
                     f" La línea defensiva de {defending} varió {line:+.1f} m.")
        elif role == "fan":
            own_defense = team_name(viewer.team_id) == defending
            title = ((f"{defending} sous pression" if fr else f"{defending} bajo presión") if own_defense else
                     (f"{attacking} déjoue le pressing" if fr else f"{attacking} supera la presión"))
            body = (f"{attacking} franchit le premier rideau de {defending} dans {now} % des tentatives récentes, contre {before} % auparavant." if fr else
                    f"{attacking} supera la primera línea de {defending} en el {now}% de los intentos recientes, frente al {before}% anterior.")
        return kicker, title, body
    if frame == "MOMENTUM_SWING":
        team, before, now = side("leadingTeam"), f["finalThirdEntriesBefore"], f["finalThirdEntries"]
        title = f"{team} prend l'initiative" if fr else f"{team} toma la iniciativa"
        body = (f"Les entrées de {team} dans le dernier tiers sont passées de {before} à {now}." if fr else
                f"Las entradas de {team} en el último tercio han pasado de {before} a {now}.")
        if role == "analyst" or not rich:
            body += (f" Possession sur les cinq dernières minutes : {pct('possessionLast5')} %." if fr else
                     f" Posesión en los últimos cinco minutos: {pct('possessionLast5')}%.")
        if role == "broadcaster":
            title = f"{prefix}{team} accentue la pression" if fr else f"{prefix}{team} aumenta la presión"
        return kicker, title, body
    if frame in {"GOAL", "COMEBACK"}:
        team = side("scoringTeam")
        scorer = shirt(str(f.get("scorer") or "")) or team
        red, blue = int(f["scoreRed"]), int(f["scoreBlue"])
        own, other = (red, blue) if team == team_name("RED") else (blue, red)
        if fr:
            title = f"{team} égalise : {score}" if own == other else f"But de {team} : {score}"
            body = f"{scorer} marque à la {f['minute']}e minute. Score : {score}."
        else:
            title = f"{team} empata: {score}" if own == other else f"¡Gol de {team}! {score}"
            body = f"{scorer} marca en el minuto {f['minute']}. Marcador: {score}."
        if role == "analyst":
            title = "Le score évolue" if fr else "Cambio en el marcador"
        return kicker, title, body
    if frame == "RED_CARD":
        team = side("cardTeam")
        red = team == team_name("RED")
        count = int(f.get("playersRed" if red else "playersBlue", 10))
        others = int(f.get("playersBlue" if red else "playersRed", 11))
        title = f"{team} à {count}" if fr else f"{team} se queda con {count}"
        body = (f"{player or team} est expulsé. {team} compte désormais {count} joueurs, contre {others} pour l'adversaire." if fr else
                f"Expulsión de {player or team}. {team} tiene ahora {count} jugadores frente a {others} del rival.")
        if role == "fan" and team_name(viewer.team_id) != team and others > count:
            title = (f"Avantage numérique pour {team_name(viewer.team_id)}" if fr else
                     f"Ventaja numérica para {team_name(viewer.team_id)}")
        return kicker, title, body
    if frame == "PLAYER_IMPACT":
        title = f"{player} plus présent entre les lignes" if fr else f"{player} aparece más entre líneas"
        body = (f"{player} a reçu le ballon entre les lignes {f['betweenLinesLast8']} fois en huit minutes, contre {f['betweenLinesPrev30']} sur les trente précédentes." if fr else
                f"{player} recibió entre líneas {f['betweenLinesLast8']} veces en ocho minutos, frente a {f['betweenLinesPrev30']} en los treinta anteriores.")
        return kicker, title, body
    if frame == "SUBSTITUTE_IMPACT":
        title = "L'apport du remplaçant" if fr else "El aporte del suplente"
        body = (f"Depuis son entrée à la {f['minuteOn']}e minute, {player} a produit {f['progressiveActions']} actions progressives, {f['boxEntries']} entrées dans la surface et {f['shots']} tirs." if fr else
                f"Desde su entrada en el minuto {f['minuteOn']}, {player} suma {f['progressiveActions']} acciones progresivas, {f['boxEntries']} entradas al área y {f['shots']} tiros.")
        return kicker, title, body
    outcomes = ({"SAVED": "arrêté", "OFF_TARGET": "non cadré", "BLOCKED": "contré", "GOAL": "but", "ON_TARGET": "cadré"} if fr else
                {"SAVED": "parado", "OFF_TARGET": "fuera", "BLOCKED": "bloqueado", "GOAL": "gol", "ON_TARGET": "a puerta"})
    outcome = outcomes.get(str(f.get("outcome")), "résultat enregistré" if fr else "resultado registrado")
    team = side("team")
    return (kicker, f"Tir de {team}" if fr else f"Tiro de {team}",
            f"Tir de {player or team} : {outcome}." if fr else f"Tiro de {player or team}: {outcome}.")
