const state = {
  ws: null,
  speech: {available: false, message: "Connecting audio…"},
  audioPlayer: null,
  serverReady: false,
  revision: 0,
  seeking: false,
  scrubbing: false,
  comparisonMoment: null,
  comparisonViewers: [{ persona: "fan", team_id: "RED", language: "es", player_id: "red_9" }, { persona: "analyst", team_id: null, language: "en", player_id: "red_9" }],
  viewerDisplays: {},
  primaryEditorial: null,
  comparisonEditorial: [],
  comparisonOverlays: [],
  scenarios: [],
  roster: { teams: {}, players: {} },
  scenario: "press_collapse",
  seed: 47129,
  speed: 1,
  running: false,
  viewer: { persona: "broadcaster", team_id: null, language: "en", player_id: "red_7" },
  frame: null,
  prev: null,
  moment: null,
  overlay: null,
  whyOpen: false,
  expandedProof: new Set(),
  candidates: [],
  selected: 0,
  pulse: null,
  provider: "",
  raw: [],
  playerRosterKey: "",
  eventQueue: [],
  eventShowing: false,
  eventTimer: null,
  evidence: null,
  demo: { active: false, step: 0, phase: "idle", settled: 0, outcomes: {}, stops: [] },
};

const pitch = document.getElementById("pitch");
const ctx = pitch.getContext("2d");
state.audioPlayer = new CommentaryPlayer({getStory: audioStory, onState: renderAudioControls});

function connect() {
  state.serverReady = false;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => setLive("Live");
  state.ws.onclose = () => {
    state.audioPlayer?.reset();
    state.speech = {available: false, message: "Reconnect to the match server to use audio."};
    renderAudioControls();
    state.serverReady = false;
    if (state.demo.active && ["preparing", "evaluating", "playing"].includes(state.demo.phase)) {
      state.running = false;
      state.demo.phase = "error";
      state.demo.message = "Connection lost. Refresh the page after restarting the backend to retry the demo.";
      renderDemoGuide();
    }
    showDemoNotice("Connection lost. Start the backend, then refresh this page to reconnect.");
    setLive("Disconnected");
  };
  state.ws.onmessage = (event) => onMessage(JSON.parse(event.data));
}

function send(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(payload));
}

function onMessage(message) {
  if (message.type.startsWith("demo_")) { receiveDemo(message); return; }
  if (message.type === "viewer_update") {
    if (state.seeking || (state.demo.active && state.demo.phase === "preparing")) return;
    const viewer = message.index === 0 ? state.viewer : state.comparisonViewers[message.index - 1];
    if (!viewer || JSON.stringify(message.profile) !== JSON.stringify(viewer)) {
      // Compare fields rather than relying on JSON object key order.
      if (!viewer || ["persona", "team_id", "language", "player_id"].some(key => message.profile?.[key] !== viewer[key])) return;
    }
    if (message.index === 0) {
      state.primaryEditorial = message;
      state.moment = message.explanation.status === "update" ? message.result.moment : null;
      state.overlay = message.explanation.status === "update" ? message.result.overlay : null;
      renderInsight(message.phase !== "enrichment");
    } else {
      state.comparisonEditorial[message.index - 1] = message;
      renderComparison(message.index - 1, message.phase !== "enrichment");
    }
    state.audioPlayer.offer(message.index === 0 ? "primary" : `view-${message.index - 1}`, message.explanation.status === "update");
    if (state.demo.active) {
      state.demo.outcomes[message.index] = message;
      renderDemoGuide();
    }
    return;
  }
  if (message.type === "enrichment") {
    if (state.seeking) return;
    if (state.primaryEditorial?.result.moment?.id !== message.result.moment_id || !state.moment) return;
    state.primaryEditorial.result.overlay = message.result.overlay;
    state.overlay = message.result.overlay;
    renderInsight(false);
    return;
  }
  if (message.type === "telemetry") { receiveTelemetry(message); return; }
  if (message.type === "seeking") { setLive("Seeking…"); return; }
  if (message.type === "seeked") {
    resetUiState();
    state.seeking = false;
    state.running = message.running;
    document.getElementById("timeline").disabled = false;
    document.getElementById("pause").disabled = false;
    document.getElementById("pause").textContent = state.running ? "Pause" : "Resume";
    document.getElementById("seek-status").textContent = "Drag to rewind or jump ahead";
    setLive(state.running ? `Live · ${state.speed}×` : "Paused");
    onMessage({ type: "frame", frame: message.frame, state: message.state, events: [] });
    return;
  }
  if (state.seeking && ["frame", "moment", "enrichment", "reframe", "comparison", "comparison_enrichment", "trace"].includes(message.type)) return;
  if (message.type === "comparison") {
    state.comparisonMoment = message.moment;
    state.comparisonOverlays = message.overlays;
    renderComparison();
    return;
  }
  if (message.type === "comparison_enrichment") {
    if (state.comparisonMoment?.id !== message.momentId) return;
    state.comparisonOverlays[message.index] = message.overlay;
    renderComparison();
    return;
  }
  if (message.type === "hello") {
    state.speech = message.speech || {available: false, message: "Restart the backend and refresh to enable commentary."};
    state.serverReady = true;
    state.demo.stops = message.demoStops || [];
    state.roster = message.roster || state.roster;
    updatePrimaryLabel();
    buildComparisonCards();
    state.scenarios = message.scenarios;
    renderScenarios();
    startMatch();
    return;
  }
  if (message.type === "status") {
    state.running = message.running;
    state.provider = message.languageProvider || state.provider;
    document.getElementById("pause").textContent = state.running ? "Pause" : "Resume";
    setLive(state.running ? `Live · ${state.speed}×` : "Paused");
    return;
  }
  if (message.type === "frame") {
    const now = performance.now();
    state.prev = state.frame;
    state.prevAt = state.frameAt || now;
    const stamp = (frame) => (frame && frame.action ? frame.action.started_ms : null);
    const nextAction = stamp(message.frame);
    const prevAction = stamp(state.frame);
    state.frame = message.frame;
    state.audioPlayer.check();
    state.frameAt = now;
    if (!state.prev || state.frame.timestamp_ms < state.prev.timestamp_ms || nextAction !== prevAction) state.trail = [];
    state.pulse = message.state;
    const arrivals = message.events || [];
    queueImportantMatchEvents(arrivals);
    const shown = arrivals.filter((item) => RAW_TYPES.has(item.type));
    if (shown.length) state.raw = state.raw.concat(shown).slice(-12);
    renderRaw();
    renderPulse();
    for (const [key, display] of Object.entries(state.viewerDisplays)) {
      const cardId = key === "primary" ? "insight" : `compare-card-${Number(key.split("-")[1])}`;
      const card = document.getElementById(cardId);
      if (display.result.moment.expires_at_ms <= state.frame.timestamp_ms && card?.dataset.displayAge !== "historical") {
        if (key === "primary") renderInsight(false);
        else renderComparison(Number(key.split("-")[1]));
      }
    }
    updateScore();
    if (!state.scrubbing) {
      document.getElementById("timeline").value = state.frame.timestamp_ms;
      document.getElementById("timeline-time").textContent = clockOf(state.frame.timestamp_ms);
    }
    fillPlayers();
    fillComparisonPlayers();
    return;
  }
  if (message.type === "moment") {
    pushCandidate(message.result);
    renderCandidates();
    return;
  }
  if (message.type === "reframe") {
    state.overlay = message.overlay;
    if (message.moment) state.moment = message.moment;
    renderInsight(true);
    return;
  }
  if (message.type === "trace") {
    pushCandidate(message.result);
    renderCandidates();
  }
}

const RAW_TYPES = new Set(["PASS", "SHOT", "CARRY", "DUEL", "INTERCEPTION", "CARD", "OFFSIDE", "FREE_KICK", "PENALTY", "CORNER", "THROW_IN", "GOAL_KICK", "SUBSTITUTION", "FOUL", "FORMATION_CHANGE"]);

function teamLabel(id, short = false) {
  const team = state.roster.teams[String(id).toUpperCase()];
  return team?.[short ? "short_name" : "name"] || id || "";
}

function shirt(id) {
  const player = state.roster.players[id];
  if (player) return `${player.name} #${player.number}`;
  if (!id || !String(id).includes("_")) return "";
  const [team, number] = String(id).split("_");
  return `${teamLabel(team)} ${number}`;
}

function clockOf(ms) {
  const minute = String(Math.floor(ms / 60000)).padStart(2, "0");
  const second = String(Math.floor((ms / 1000) % 60)).padStart(2, "0");
  return `${minute}:${second}`;
}

function eventLine(event) {
  const who = shirt(event.player_id) || teamLabel(event.team_id);
  const target = event.target_player_id ? ` → ${shirt(event.target_player_id)}` : "";
  const outcome = event.outcome ? `  ${event.outcome}` : "";
  return `<b>${escapeHtml(event.type)}</b> ${escapeHtml(`${clockOf(event.timestamp_ms)}  ${who}${target}${outcome}`)}`;
}

function renderRaw() {
  const log = document.getElementById("raw-log");
  if (!log) return;
  const rows = state.raw.slice(-5).reverse();
  log.innerHTML = rows.length
    ? rows.map((event, index) => `<li class="${index === 0 ? "now" : ""}">${eventLine(event)}</li>`).join("")
    : "<li>Waiting for the first event</li>";
}

function resetUiState() {
  state.audioPlayer?.reset();
  clearEvidence();
  state.revision += 1;
  state.primaryEditorial = null;
  state.viewerDisplays = {};
  state.comparisonEditorial = [];
  state.comparisonMoment = null;
  state.comparisonOverlays = [];
  state.pulse = null;
  state.expandedProof.clear();
  document.getElementById("seek-preview").hidden = true;
  renderComparison();
  state.raw = [];
  state.frame = null;
  state.prev = null;
  state.frameAt = 0;
  state.prevAt = 0;
  state.trail = [];
  state.moment = null;
  state.overlay = null;
  state.whyOpen = false;
  state.candidates = [];
  state.selected = 0;
  state.pulse = null;
  state.playerRosterKey = "";
  state.eventQueue = [];
  state.eventShowing = false;
  if (state.eventTimer) window.clearTimeout(state.eventTimer);
  state.eventTimer = null;
  const eventFlash = document.getElementById("event-flash");
  eventFlash.className = "event-flash";
  document.getElementById("event-flash-title").textContent = "";
  document.getElementById("event-flash-detail").textContent = "";
  document.getElementById("latency").textContent = "—";
  document.getElementById("score-red").textContent = "0";
  document.getElementById("score-blue").textContent = "0";
  for (const team of ["red", "blue"]) {
    document.getElementById(`cards-${team}`).hidden = true;
    document.getElementById(`cards-${team}`).textContent = "0";
  }
  document.getElementById("clock").textContent = "00:00";
  document.getElementById("insight").className = "card empty";
  document.getElementById("insight").innerHTML = `
    <p class="kicker">Waiting for the match</p>
    <h2>Waiting for an insight</h2>
    <p class="body">Verified moments will appear as the match unfolds.</p>`;
  document.getElementById("why").classList.add("hidden");
  document.getElementById("why").innerHTML = "";
  document.getElementById("pulse").innerHTML = "";
  document.getElementById("candidate-detail").innerHTML = `
    <p class="kicker">Current moment</p>
    <h2>Nothing has cleared the producer yet.</h2>`;
  renderCandidates();
  renderRaw();
  renderInsight(false);
}

function startMatch() {
  state.demo.active = false;
  renderDemoGuide();
  resetUiState();
  state.seeking = false;
  state.scrubbing = false;
  const start = (state.scenarios.find(item => item.id === state.scenario)?.startMinute || 0) * 60000;
  document.getElementById("timeline").min = start;
  document.getElementById("timeline").value = start;
  document.getElementById("timeline").disabled = false;
  document.getElementById("pause").disabled = false;
  document.getElementById("timeline-start").textContent = clockOf(start);
  document.getElementById("timeline-time").textContent = clockOf(start);
  send({
    type: "start",
    scenario: state.scenario,
    seed: Number(state.seed),
    speed: Number(state.speed),
    viewer: state.viewer,
    views: comparisonViews(),
  });
}

function setLive(text) {
  document.getElementById("live-pill").textContent = text;
}

function updateScore() {
  if (!state.frame) return;
  document.getElementById("score-red").textContent = state.frame.score_red;
  document.getElementById("score-blue").textContent = state.frame.score_blue;
  for (const team of ["red", "blue"]) {
    const badge = document.getElementById(`cards-${team}`);
    const count = state.pulse?.[team]?.red_cards || 0;
    badge.hidden = count === 0;
    badge.textContent = count;
    badge.title = `${teamLabel(team)} red cards: ${count}`;
    badge.setAttribute("aria-label", badge.title);
  }
  const minute = String(Math.floor(state.frame.timestamp_ms / 60000)).padStart(2, "0");
  const second = String(Math.floor(state.frame.timestamp_ms / 1000) % 60).padStart(2, "0");
  document.getElementById("clock").textContent = `${minute}:${second}`;
}

function storyName(frame) {
  return ({PRESS_BREAKDOWN:"Press breakdown", MOMENTUM_SWING:"Momentum shift", PLAYER_IMPACT:"Player involvement", SUBSTITUTE_IMPACT:"Substitute impact", GOAL:"Goal", COMEBACK:"Comeback", RED_CARD:"Red card", SHOT_CHANCE:"Chance created"})[frame] || frame || "No selection";
}
function renderEditorialCard(card, proof, viewer, update, key, fresh) {
  if (!card) return;
  const now = state.frame?.timestamp_ms || 0;
  if (update?.explanation.status === "update" && update.result.overlay && update.result.moment?.expires_at_ms > now) state.viewerDisplays[key] = update;
  const display = state.viewerDisplays[key];
  const result = display?.result || update?.result;
  const explanation = display?.explanation || update?.explanation;
  const latest = update?.explanation;
  const showing = Boolean(display?.result.overlay);
  const historical = showing && display.result.moment.expires_at_ms <= now;
  const heldNext = showing && latest?.status === "no_interruption";
  card.classList.toggle("empty", !showing);
  card.classList.toggle("quiet-card", !showing);
  const displayId = showing ? result.moment.id : "";
  if (fresh && showing && card.dataset.displayId !== displayId) {
    const panel = card.closest(".persona-panel");
    panel.classList.remove("selection-flash");
    requestAnimationFrame(() => panel.classList.add("selection-flash"));
  }
  card.dataset.displayId = displayId;
  card.dataset.displayAge = historical ? "historical" : "current";
  const at = explanation?.timestamp_ms;
  const header = `<div class="editorial-status"><span class="${showing && !historical ? "status-update" : "status-quiet"}">${showing ? (historical ? "Last aired" : "Update") : "Watching"}</span>${at != null ? `<time title="Match time of this story">${clockOf(at)}</time>` : ""}</div>`;
  const title = showing ? result.overlay.headline : "Watching for your first story";
  const body = showing ? result.overlay.body : explanation?.reason || "The match keeps flowing. A story appears when it matters to this viewer.";
  const heldStatus = heldNext ? `<div class="held-update" title="${escapeHtml(latest.reason)}"><span>${latest.candidateCount ? "Stories held" : "No new story"}</span><p>${escapeHtml(latest.reason)}</p>${latest.timestamp_ms != null ? `<time>${clockOf(latest.timestamp_ms)}</time>` : ""}</div>` : "";
  const disclosureKey = `${key}:explanation`;
  const previous = card.querySelector(".editorial-explanation");
  if (previous) {
    if (previous.open) state.expandedProof.add(disclosureKey);
    else state.expandedProof.delete(disclosureKey);
  }
  const evidence = explanation?.evidence?.map(text => `<li>${escapeHtml(text)}</li>`).join("") || "<li>No selected story needs to be put on screen.</li>";
  const held = latest?.held?.map(item => `<li><b>${escapeHtml(storyName(item.frame))}${item.player ? " · " + escapeHtml(shirt(item.player)) : ""}</b> · ${escapeHtml(item.reason)}</li>`).join("") || "<li>No other verified candidates in this update.</li>";
  const why = explanation ? `<details class="editorial-explanation" ${state.expandedProof.has(disclosureKey) ? "open" : ""}><summary>Why this decision? <span>${explanation.candidateCount} ${explanation.candidateCount === 1 ? "story" : "stories"}</span></summary><div class="editorial-details"><p><b>${showing ? "Chosen" : "Held"}:</b> ${escapeHtml(storyName(explanation.chosen))}</p><p>${escapeHtml(explanation.reason)}</p>${showing ? `<p class="proof-label">Full update</p><p>${escapeHtml(result.overlay.body)}</p>` : ""}${heldNext ? `<p class="proof-label">Latest decision: no interruption</p><p>${escapeHtml(latest.reason)}</p>` : ""}<p class="proof-label">Evidence</p><ul>${evidence}</ul><p class="proof-label">Held back</p><ul>${held}</ul><p class="note">${explanation.sourceEventCount} source events · explanation from ${escapeHtml(explanation.reasonSource)}</p></div></details>` : "";
  card.innerHTML = `${header}<h2>${escapeHtml(title)}</h2><p class="body">${escapeHtml(body)}</p>${showing ? '<button class="evidence-link" data-show-evidence type="button">View evidence on pitch ↗</button>' : ""}${heldStatus}${why}`;
  card.querySelector("[data-show-evidence]")?.addEventListener("click", () => showEvidence(result.moment));
  renderProof(proof, showing ? result.moment : null, showing ? result.overlay : null, key);
}
function renderInsight(fresh) {
  renderEditorialCard(document.getElementById("insight"), document.getElementById("why"), state.viewer, state.primaryEditorial, "primary", fresh);
  renderAudioControls();
}

function renderWhy() {
  renderProof(document.getElementById("why"), state.moment, state.overlay, "primary");
}

function renderProof(host, moment, overlay, viewKey) {
  if (!host) return;
  if (!moment || !overlay || moment.id !== overlay.moment_id) {
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }
  host.classList.remove("hidden");
  const key = `${viewKey}:${moment.id}`;
  const stats = (overlay.stats || []).map(stat => `<div class="stat"><span>${escapeHtml(stat.label)}</span><span>${escapeHtml(stat.value)}</span></div>`).join("");
  const facts = Object.entries(moment.facts || {}).map(([name, value]) => `<div class="stat"><span>${escapeHtml(name)}</span><span>${escapeHtml(value)}</span></div>`).join("");
  const evidence = (moment.evidence_detail || []).map((item, index) => `<li><button class="source-event" data-source-index="${index}" type="button"><b>${clockOf(item.timestamp_ms)}</b> ${escapeHtml(item.label)} · ${escapeHtml(item.outcome || item.type)} ↗</button><small>${escapeHtml(item.id)}</small></li>`).join("");
  const checks = (moment.checks || []).map(check => `<li><b>${check.passed ? "PASS" : "FAIL"}</b> ${escapeHtml(check.name)}: ${escapeHtml(check.detail)}</li>`).join("");
  host.innerHTML = `<details class="proof" data-proof="${escapeHtml(key)}" ${state.expandedProof.has(key) ? "open" : ""}>
    <summary>Facts &amp; evidence <span>${Math.round(moment.confidence * 100)}% selection confidence</span></summary>
    <div class="proof-content"><p class="kicker">Verified at ${clockOf(moment.valid_from_ms)}</p><button class="evidence-link" data-show-evidence type="button">View evidence on pitch ↗</button>${stats}
    <p class="proof-label">Facts behind this update</p>${facts || '<p class="evidence">No additional facts.</p>'}
    <p class="proof-label">Source events</p><ul class="evidence">${evidence || '<li>No event details attached.</li>'}</ul>
    ${checks ? `<p class="proof-label">Verification checks</p><ul class="evidence">${checks}</ul>` : ""}
    <details class="raw-proof" data-proof="${escapeHtml(key + ":raw")}" ${state.expandedProof.has(key + ":raw") ? "open" : ""}><summary>Raw verified data</summary><pre>${escapeHtml(JSON.stringify({ moment, overlay }, null, 2))}</pre></details>
    </div></details>`;
  host.querySelectorAll("details").forEach(details => details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) state.expandedProof.add(details.dataset.proof);
    else state.expandedProof.delete(details.dataset.proof);
  }));
  host.querySelector("[data-show-evidence]")?.addEventListener("click", () => showEvidence(moment));
  host.querySelectorAll("[data-source-index]").forEach(button => button.addEventListener("click", () => showEvidence(moment, Number(button.dataset.sourceIndex))));
}

function evidenceMetric(moment) {
  const f = moment.facts;
  if (moment.frame === "PRESS_BREAKDOWN") return {
    label: `${f.attackingTeam} through ${f.defendingTeam}'s first press`,
    before: Math.round(f.pressBypassBefore * 100), after: Math.round(f.pressBypassNow * 100), unit: "%", max: 100,
  };
  if (moment.frame === "MOMENTUM_SWING") return {
    label: `${f.leadingTeam} final-third entries · five-minute windows`,
    before: f.finalThirdEntriesBefore, after: f.finalThirdEntries, unit: "",
    max: Math.max(f.finalThirdEntriesBefore, f.finalThirdEntries, 1),
  };
  return null;
}

function showEvidence(moment, selected = null) {
  state.evidence = { moment, selected };
  const panel = document.getElementById("pitch-evidence");
  panel.hidden = false;
  const events = moment.evidence_detail || [];
  const metric = evidenceMetric(moment);
  document.getElementById("evidence-title").textContent = `${storyName(moment.frame)} · verified ${clockOf(moment.valid_from_ms)}`;
  document.getElementById("evidence-metric").innerHTML = metric ?
    `<p>${escapeHtml(metric.label)}</p><div class="evidence-values"><span>Before <b>${metric.before}${metric.unit}</b></span><span>Now <b>${metric.after}${metric.unit}</b></span></div><div class="evidence-bars"><i style="width:${Math.max(0, Math.min(100, metric.before / metric.max * 100))}%"></i><i style="width:${Math.max(0, Math.min(100, metric.after / metric.max * 100))}%"></i></div>` :
    `<p>${escapeHtml(moment.frame === "GOAL" || moment.frame === "COMEBACK" ? `Score ${moment.facts.scoreBeforeRed}–${moment.facts.scoreBeforeBlue} → ${moment.facts.scoreRed}–${moment.facts.scoreBlue}` : moment.frame === "RED_CARD" ? `Players LIV ${moment.facts.playersRed} · MCI ${moment.facts.playersBlue}` : "Source events attached to this update")}</p>`;
  document.getElementById("evidence-caption").textContent = events.length ?
    `${events.length} historical source event${events.length === 1 ? "" : "s"} · current player positions hidden · ${state.running ? "match continues behind this map" : "match held"}` :
    "Verified rolling aggregates. No event paths attached to this story; no player movement is inferred.";
  document.getElementById("evidence-events").innerHTML = (events.length ? `<button type="button" data-event="all" aria-pressed="${selected === null}">All paths</button>` : "") + events.map((event, index) =>
    `<button type="button" data-event="${index}" aria-pressed="${index === selected}" title="${escapeHtml(event.label)}">${index + 1} · ${clockOf(event.timestamp_ms)} · ${escapeHtml(event.type)}</button>`).join("");
  document.getElementById("evidence-events").querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => showEvidence(moment, button.dataset.event === "all" ? null : Number(button.dataset.event)));
  });
  document.querySelector('[data-tab="watch"]').click();
}

function clearEvidence() {
  state.evidence = null;
  const panel = document.getElementById("pitch-evidence");
  if (panel) panel.hidden = true;
}

function drawEvidenceMap(xOf, yOf) {
  const { moment, selected } = state.evidence;
  const events = moment.evidence_detail || [];
  const validPoint = point => point && Number.isFinite(point.x) && Number.isFinite(point.y) && point.x >= 0 && point.x <= 105 && point.y >= 0 && point.y <= 68;
  ctx.save();
  events.forEach((event, index) => {
    if (!validPoint(event.location)) return;
    const start = event.location;
    const end = validPoint(event.end_location) ? event.end_location : start;
    const sx = xOf(start.x), sy = yOf(start.y), ex = xOf(end.x), ey = yOf(end.y);
    const focused = selected === null || selected === index;
    ctx.globalAlpha = focused ? 1 : .2;
    ctx.strokeStyle = "#ffe39b"; ctx.fillStyle = "#ffe39b"; ctx.lineWidth = focused ? 3.5 : 2;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
    if (Math.hypot(ex - sx, ey - sy) > 8) {
      const angle = Math.atan2(ey - sy, ex - sx);
      ctx.beginPath(); ctx.moveTo(ex, ey);
      ctx.lineTo(ex - 11 * Math.cos(angle - .4), ey - 11 * Math.sin(angle - .4));
      ctx.lineTo(ex - 11 * Math.cos(angle + .4), ey - 11 * Math.sin(angle + .4)); ctx.closePath(); ctx.fill();
    }
    ctx.beginPath(); ctx.arc(sx, sy, 12, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#102019"; ctx.font = "600 11px Outfit, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(String(index + 1), sx, sy);
  });
  if (!events.some(event => validPoint(event.location))) {
    ctx.fillStyle = "#f3f6f2"; ctx.font = "500 15px Outfit, sans-serif"; ctx.textAlign = "center";
    ctx.fillText("Verified aggregate metrics · no event map available", xOf(52.5), yOf(48));
  }
  ctx.restore();
}

function statCapsules(viewer) {
  const pulse = state.pulse;
  if (!pulse) return [];
  const dismissals = (pulse.red.red_cards || 0) + (pulse.blue.red_cards || 0);
  const manpower = `Players LIV ${pulse.red.players_on_pitch ?? 11} · MCI ${pulse.blue.players_on_pitch ?? 11}`;
  if (isNeutralViewer(viewer)) {
    if (viewer.persona === "broadcaster") return [`Score ${pulse.score_red}–${pulse.score_blue}`, dismissals ? manpower : `Possession LIV ${Math.round(pulse.red.possession_last5 * 100)}% · MCI ${Math.round(pulse.blue.possession_last5 * 100)}%`];
    const bypass = team => team.press_bypass_rate == null ? "—" : `${Math.round(team.press_bypass_rate * 100)}%`;
    return [dismissals ? manpower : `Press beaten LIV ${bypass(pulse.red)} · MCI ${bypass(pulse.blue)}`, `Entries (5′) LIV ${pulse.red.final_third_entries_last5} · MCI ${pulse.blue.final_third_entries_last5}`];
  }
  const teamName = teamLabel(viewer.team_id, true);
  const team = pulse[viewer.team_id.toLowerCase()] || {};
  const share = `${Math.round((team.possession_last5 ?? 0) * 100)}%`;
  if (viewer.persona === "casual") return [`Score ${pulse.score_red}–${pulse.score_blue}`, `${teamLabel(pulse.possession_team, true)} on the ball`];
  if (viewer.persona === "fan") return [`${teamName} possession ${share}`, dismissals ? manpower : `Entries (5′) ${team.final_third_entries_last5 ?? 0}`, `Shots (5′) ${team.shots_last5 ?? 0}`];
  if (viewer.persona === "player") {
    const player = pulse.players?.[viewer.player_id];
    if (!player) return [`${shirt(viewer.player_id)} · no tracking data`];
    return [`${shirt(viewer.player_id)} between lines (8′) ${player.between_lines_last8}`, `Progressive ${player.progressive_actions}`, `Box entries ${player.box_entries}`];
  }
  if (viewer.persona === "broadcaster") return [`Score ${pulse.score_red}–${pulse.score_blue}`, `${teamName} possession ${share}`, `Entries (5′) ${team.final_third_entries_last5 ?? 0}`];
  const bypass = team.press_bypass_rate == null ? "—" : `${Math.round(team.press_bypass_rate * 100)}%`;
  const line = team.defensive_line_m == null ? "—" : `${team.defensive_line_m.toFixed(1)}m`;
  return [`${teamName} press beaten ${bypass}`, `${teamName} line ${line}`, `Possession (5′) ${share}`, `Entries (5′) ${team.final_third_entries_last5 ?? 0}`];
}

function renderPulse() {
  const render = (host, viewer) => {
    if (host) host.innerHTML = statCapsules(viewer).slice(0, 2).map(text => `<span>${escapeHtml(text)}</span>`).join("");
  };
  render(document.getElementById("pulse"), state.viewer);
  state.comparisonViewers.forEach((viewer, index) => render(document.getElementById(`view-pulse-${index}`), viewer));
}

function pushCandidate(result) {
  state.candidates.unshift(result);
  state.candidates = state.candidates.slice(0, 30);
  state.selected = 0;
}

function renderCandidates() {
  const list = document.getElementById("candidates");
  if (!state.candidates.length) {
    list.innerHTML = `<div class="candidate"><strong>No candidates yet</strong><em>Triggers that reach the decision call show up here.</em></div>`;
    return;
  }
  list.innerHTML = state.candidates.map((result, index) => {
    const frame = result.moment?.frame || (result.decision ? "NO UPDATE" : "NO VERIFIED CANDIDATE");
    const action = result.producer?.action || "TRACE";
    const confidence = result.decision ? result.decision.primary_frame_confidence.toFixed(2) : "—";
    return `<button class="candidate ${index === state.selected ? "on" : ""}" data-index="${index}" type="button">
      <span class="tag">${confidence} · ${action}</span>
      <strong>${frame.replaceAll("_", " ")}</strong>
      <em>${escapeHtml(result.producer?.reason || result.verification?.reason || "")}</em>
    </button>`;
  }).join("");
  list.querySelectorAll(".candidate").forEach((button) => {
    button.addEventListener("click", () => {
      state.selected = Number(button.dataset.index);
      renderCandidates();
    });
  });
  renderDetail();
}

function renderDetail() {
  const host = document.getElementById("candidate-detail");
  const result = state.candidates[state.selected];
  if (!result) return;
  const decision = result.decision;
  const rawChecks = result.verification?.checks || result.moment?.checks || [];
  const checks = rawChecks.filter((check) => (result.verification?.verified ? check.passed : true)).map((check) =>
    `<div class="check ${check.passed ? "pass" : "fail"}"><span>${escapeHtml(check.name)}</span><b>${check.passed ? "PASS" : "FAIL"} · ${escapeHtml(check.detail)}</b></div>`
  ).join("");
  const bars = decision
    ? Object.entries(decision.frame_scores).map(([name, value]) => `<div class="stat"><span>${name}</span><span>${Math.round(value * 100)}%</span></div>`).join("")
    : "";
  host.innerHTML = `
    <p class="kicker">${escapeHtml(result.kind)} · ${escapeHtml(decision?.provider || "")}</p>
    <h2>${escapeHtml((result.moment?.frame || (decision?.selected_candidate_id ? decision.primary_frame : "No update")).replaceAll("_", " "))}</h2>
    <p class="body">${escapeHtml(result.producer?.reason || result.verification?.reason || "Trigger did not continue.")}</p>
    ${result.candidates ? `<p class="proof-label">${result.candidates.candidates.length} verified candidates · ${decision?.selected_candidate_id ? "Selected " + escapeHtml(decision.selected_candidate_id) : "No selection"}</p>` : ""}
    ${bars}
    ${checks}`;
}

function renderScenarios() {
  const host = document.getElementById("scenarios");
  host.innerHTML = state.scenarios.map((scenario) => `
    <button class="scenario ${scenario.id === state.scenario ? "on" : ""}" type="button" data-id="${scenario.id}">
      <strong>${escapeHtml(scenario.label)}</strong>
      <span>${escapeHtml(scenario.description)}</span>
    </button>`).join("");
  host.querySelectorAll(".scenario").forEach((button) => {
    button.addEventListener("click", () => {
      state.scenario = button.dataset.id;
      renderScenarios();
    });
  });
}

function fillPlayers() {
  const select = document.getElementById("player");
  if (!state.frame) return;
  const players = [...state.frame.players].sort((a, b) => a.team_id.localeCompare(b.team_id) || a.number - b.number);
  const rosterKey = players.map((player) => player.player_id).join("|");
  if (rosterKey === state.playerRosterKey) return;
  state.playerRosterKey = rosterKey;
  const previous = state.viewer.player_id;
  select.innerHTML = players.map((player) => `<option value="${escapeHtml(player.player_id)}">${escapeHtml(shirt(player.player_id))} · ${escapeHtml(player.role)}</option>`).join("");
  const available = players.some((player) => player.player_id === previous);
  state.viewer.player_id = available ? previous : (players[0]?.player_id || previous);
  select.value = state.viewer.player_id;
  updateViewerHelp();
}

function updateViewerHelp() {
  const help = document.getElementById("viewer-help");
  const select = document.getElementById("player");
  const isPlayer = state.viewer.persona === "player";
  select.disabled = !isPlayer;
  document.getElementById("team").closest("label").hidden = isNeutralViewer(state.viewer);
  document.getElementById("team").disabled = isNeutralViewer(state.viewer);
  document.getElementById("team").value = state.viewer.team_id || "RED";
  if (isPlayer) {
    help.textContent = `Following ${shirt(state.viewer.player_id)}. MatchOS prioritizes verified moments involving this player; major match moments can still break through.`;
  } else if (state.viewer.persona === "fan") {
    help.textContent = `Home fan view frames verified moments from ${teamLabel(state.viewer.team_id)}'s perspective.`;
  } else if (state.viewer.persona === "analyst") {
    help.textContent = "Neutral analysis of both teams: tactical changes, underlying metrics and denser explanations.";
  } else if (state.viewer.persona === "broadcaster") {
    help.textContent = "Neutral coverage of the whole match: timely broadcast headlines and production metadata.";
  } else {
    help.textContent = "Casual view shows fewer moments and explains them with minimal football jargon.";
  }
}

function describeMatchEvent(event) {
  const player = shirt(event.player_id);
  if (event.type === "SHOT" && event.outcome === "GOAL") {
    return { title: "GOAL", detail: player ? `${player} scores` : `${teamLabel(event.team_id)} score`, kind: "goal" };
  }
  if (event.type === "PENALTY") return { title: "PENALTY", detail: `${teamLabel(event.team_id)} awarded a penalty`, kind: "penalty" };
  if (event.type === "SHOT" && event.qualifiers?.penalty) {
    const title = event.outcome === "SAVED" ? "PENALTY SAVED" : "PENALTY MISSED";
    return { title, detail: player || teamLabel(event.team_id), kind: "penalty" };
  }
  if (event.type === "CARD") {
    const card = String(event.qualifiers?.card || event.outcome || "").toLowerCase();
    return { title: card === "red" ? "RED CARD" : "YELLOW CARD", detail: player || teamLabel(event.team_id), kind: card === "red" ? "red" : "yellow" };
  }
  if (event.type === "OFFSIDE") return { title: "OFFSIDE", detail: player ? `${player} caught offside` : "Flag up", kind: "offside" };
  if (event.type === "FOUL") return { title: "FOUL", detail: player ? `Committed by ${player}` : teamLabel(event.team_id), kind: "foul" };
  if (event.type === "FREE_KICK") return { title: "FREE KICK", detail: `${teamLabel(event.team_id)} restart`, kind: "free-kick" };
  if (event.type === "CORNER") return { title: "CORNER", detail: `${teamLabel(event.team_id)} corner kick`, kind: "corner" };
  if (event.type === "THROW_IN") return { title: "THROW-IN", detail: `${teamLabel(event.team_id)} restart`, kind: "throw-in" };
  if (event.type === "GOAL_KICK") return { title: "GOAL KICK", detail: `${teamLabel(event.team_id)} restart`, kind: "goal-kick" };
  if (event.type === "FORMATION_CHANGE") return { title: "FORMATION CHANGE", detail: event.outcome || event.qualifiers?.formation || teamLabel(event.team_id), kind: "formation" };
  if (event.type === "SUBSTITUTION") return { title: "SUBSTITUTION", detail: player ? `${player} comes on` : teamLabel(event.team_id), kind: "substitution" };
  return null;
}

function queueImportantMatchEvents(events) {
  events.forEach((event) => {
    const descriptor = describeMatchEvent(event);
    if (descriptor) state.eventQueue.push(descriptor);
  });
  // Protect accelerated Lab playback from building an unbounded visual backlog.
  state.eventQueue = state.eventQueue.slice(-8);
  showNextMatchEvent();
}

function showNextMatchEvent() {
  if (state.eventShowing || !state.eventQueue.length) return;
  const descriptor = state.eventQueue.shift();
  state.eventShowing = true;
  const node = document.getElementById("event-flash");
  const title = document.getElementById("event-flash-title");
  const detail = document.getElementById("event-flash-detail");
  node.className = `event-flash ${descriptor.kind}`;
  title.textContent = descriptor.title;
  detail.textContent = descriptor.detail;
  void node.offsetWidth;
  node.classList.add("show");
  state.eventTimer = window.setTimeout(() => {
    node.classList.remove("show");
    state.eventShowing = false;
    state.eventTimer = null;
    showNextMatchEvent();
  }, state.speed >= 5 ? 700 : 1300);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function watchVisible() {
  const panel = document.getElementById("panel-watch");
  return Boolean(panel && !panel.classList.contains("hidden"));
}

function resize() {
  if (!watchVisible()) return;
  const rect = pitch.parentElement.getBoundingClientRect();
  if (rect.width < 80 || rect.height < 80) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * ratio);
  const height = Math.round(rect.height * ratio);
  if (pitch.width !== width || pitch.height !== height) {
    pitch.width = width;
    pitch.height = height;
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function viewModel() {
  const curr = state.frame;
  if (!curr) return null;
  const prev = state.prev;
  if (!prev || !state.frameAt || curr.timestamp_ms < prev.timestamp_ms) {
    return { ball: curr.ball, ballZ: curr.ball_z || 0, players: curr.players, action: curr.action || null };
  }

  // Render one network frame behind and interpolate once between authoritative
  // tracking samples. This keeps motion continuous at 1x and at accelerated
  // playback without adding a second follower that makes players float/lag.
  const wallGap = Math.min(180, Math.max(50, state.frameAt - (state.prevAt || state.frameAt) || 100));
  const t = Math.max(0, Math.min(1, (performance.now() - state.frameAt) / wallGap));
  const earlier = new Map(prev.players.map((player) => [player.player_id, player]));
  const players = curr.players.map((player) => {
    const from = earlier.get(player.player_id);
    if (!from) return player;
    return {
      ...player,
      x: lerp(from.x, player.x, t),
      y: lerp(from.y, player.y, t),
    };
  });

  return {
    ball: {
      x: lerp(prev.ball.x, curr.ball.x, t),
      y: lerp(prev.ball.y, curr.ball.y, t),
    },
    ballZ: lerp(prev.ball_z || 0, curr.ball_z || 0, t),
    players,
    action: curr.action || null,
  };
}

function draw() {
  try {
    paint();
  } finally {
    requestAnimationFrame(draw);
  }
}

function paint() {
  if (!watchVisible()) return;
  resize();
  const rect = pitch.parentElement.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  if (width < 80 || height < 80) return;
  ctx.clearRect(0, 0, width, height);
  const pad = 28;
  const pitchW = width - pad * 2;
  const pitchH = height - pad * 2;
  if (pitchW < 40 || pitchH < 40) return;
  for (let i = 0; i < 12; i += 1) {
    ctx.fillStyle = i % 2 === 0 ? "#146845" : "#18784e";
    ctx.fillRect(pad + (pitchW / 12) * i, pad, pitchW / 12 + 0.5, pitchH);
  }
  const x = (metres) => pad + (metres / 105) * pitchW;
  const y = (metres) => pad + (metres / 68) * pitchH;
  ctx.strokeStyle = "rgba(255,255,255,0.82)";
  ctx.lineWidth = 1.6;
  ctx.strokeRect(pad, pad, pitchW, pitchH);
  ctx.beginPath();
  ctx.moveTo(x(52.5), pad);
  ctx.lineTo(x(52.5), pad + pitchH);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(x(52.5), y(34), (9.15 / 105) * pitchW, 0, Math.PI * 2);
  ctx.stroke();
  box(x, y);
  const view = viewModel();
  if (state.evidence) {
    drawEvidenceMap(x, y);
  } else if (view) {
    drawTrail(x, y, view);
    drawAction(x, y, view);
    drawPlayers(x, y, view);
  }
}

function box(x, y) {
  const spots = [
    [0, 13.84, 16.5, 40.32],
    [0, 24.84, 5.5, 18.32],
    [88.5, 13.84, 16.5, 40.32],
    [99.5, 24.84, 5.5, 18.32],
  ];
  const scaleX = (x(105) - x(0)) / 105;
  const scaleY = (y(68) - y(0)) / 68;
  spots.forEach(([mx, my, w, h]) => {
    ctx.strokeRect(x(mx), y(my), w * scaleX, h * scaleY);
  });
}

function rememberTrail(ball, action) {
  if (action) {
    state.trail = [];
    return;
  }
  if (!state.trail) state.trail = [];
  const last = state.trail[state.trail.length - 1];
  if (!last || Math.hypot(ball.x - last.x, ball.y - last.y) > 0.45) {
    state.trail.push({ x: ball.x, y: ball.y });
    if (state.trail.length > 12) state.trail.shift();
  }
}

function drawTrail(x, y, view) {
  rememberTrail(view.ball, view.action);
  const trail = state.trail || [];
  if (trail.length < 2) return;
  ctx.beginPath();
  trail.forEach((point, index) => {
    if (index === 0) ctx.moveTo(x(point.x), y(point.y));
    else ctx.lineTo(x(point.x), y(point.y));
  });
  ctx.strokeStyle = "rgba(247,244,234,0.35)";
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawAction(x, y, view) {
  const action = view.action;
  if (!action || !action.start || !action.end) return;
  const dx = action.end.x - action.start.x;
  const dy = action.end.y - action.start.y;
  const len2 = dx * dx + dy * dy || 1;
  const along = Math.max(0, Math.min(1, ((view.ball.x - action.start.x) * dx + (view.ball.y - action.start.y) * dy) / len2));
  const sx = x(action.start.x);
  const sy = y(action.start.y);
  const ex = x(action.end.x);
  const ey = y(action.end.y);
  const bx = x(action.start.x + dx * along);
  const by = y(action.start.y + dy * along);
  const colour = action.team_id === "RED" ? "rgba(233,76,85," : "rgba(140,200,235,";
  ctx.save();
  ctx.lineCap = "round";
  ctx.setLineDash([6, 7]);
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(ex, ey);
  ctx.strokeStyle = `${colour}0.45)`;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(bx, by);
  ctx.strokeStyle = `${colour}0.95)`;
  ctx.lineWidth = action.kind === "SHOT" ? 2.8 : 2.3;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(ex, ey, 4.5, 0, Math.PI * 2);
  ctx.strokeStyle = `${colour}0.9)`;
  ctx.lineWidth = 1.6;
  ctx.stroke();
  ctx.restore();
}

function drawPlayers(xOf, yOf, view) {
  const highlights = new Set(state.moment?.highlight_player_ids || []);
  view.players.forEach((player) => {
    const x = xOf(player.x);
    const y = yOf(player.y);
    const followed = state.viewer.persona === "player" && state.viewer.player_id === player.player_id;
    if (followed) {
      ctx.beginPath();
      ctx.arc(x, y, 19, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,255,255,0.95)";
      ctx.lineWidth = 2.5;
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.font = "600 9px Outfit, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("FOLLOW", x, y - 23);
    } else if (highlights.has(player.player_id)) {
      ctx.beginPath();
      ctx.arc(x, y, 16, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(230,195,106,0.9)";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(x, y, 11, 0, Math.PI * 2);
    ctx.fillStyle = state.roster.teams[player.team_id]?.color || "#f3f6f2";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(0,0,0,0.35)";
    ctx.stroke();
    ctx.fillStyle = "#08110d";
    ctx.font = "700 10px Outfit, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(player.number), x, y + 0.5);
    const name = state.roster.players[player.player_id]?.short_name || player.name;
    if (name) {
      ctx.font = "500 10px Outfit, sans-serif";
      const width = ctx.measureText(name).width + 10;
      ctx.fillStyle = "rgba(5, 19, 14, 0.82)";
      ctx.fillRect(x - width / 2, y + 14, width, 15);
      ctx.fillStyle = "#f3f6f2";
      ctx.fillText(name, x, y + 21.5);
    }
  });
  const lift = Math.max(0, view.ballZ) * 7;
  const bx = xOf(view.ball.x);
  const by = yOf(view.ball.y) - lift;
  if (lift > 1) {
    ctx.beginPath();
    ctx.ellipse(xOf(view.ball.x), yOf(view.ball.y), 5, 2.2, 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(0,0,0,0.28)";
    ctx.fill();
  }
  ctx.beginPath();
  ctx.arc(bx, by, 5.5, 0, Math.PI * 2);
  ctx.fillStyle = "#f7f4ea";
  ctx.fill();
  ctx.strokeStyle = "#1b1b1b";
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".panel").forEach((panel) => panel.classList.add("hidden"));
    document.getElementById(`panel-${tab.dataset.tab}`).classList.remove("hidden");
    if (tab.dataset.tab === "watch") requestAnimationFrame(resize);
    if (tab.dataset.tab === "telemetry") pollTelemetry();
  });
});

["persona", "team", "language", "player"].forEach((id) => {
  document.getElementById(id).addEventListener("change", (event) => {
    const map = { persona: "persona", team: "team_id", language: "language", player: "player_id" };
    state.revision += 1;
    state.viewer[map[id]] = event.target.value;
    if (state.audioPlayer.view === "primary") state.audioPlayer.reset();
    normalizeViewer(state.viewer);
    state.primaryEditorial = null; delete state.viewerDisplays.primary; state.moment = null; state.overlay = null;
    renderInsight(false);
    send({ type: "viewer", viewer: state.viewer });
    updatePrimaryLabel();
    renderPulse();
    updateViewerHelp();
  });
});

document.getElementById("reset").addEventListener("click", () => {
  markSpeed(state.speed);
  startMatch();
});

document.getElementById("pause").addEventListener("click", () => {
  send({ type: "control", action: state.running ? "pause" : "resume" });
  state.running = !state.running;
  document.getElementById("pause").textContent = state.running ? "Pause" : "Resume";
});

function markSpeed(speed) {
  document.querySelectorAll("#speeds button").forEach((item) => item.classList.toggle("on", Number(item.dataset.speed) === speed));
}

document.getElementById("speeds").innerHTML = [1, 2, 5, 20].map((speed) => `<button type="button" data-speed="${speed}" class="${speed === 1 ? "on" : ""}">${speed}×</button>`).join("");
document.getElementById("speeds").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.speed = Number(button.dataset.speed);
  markSpeed(state.speed);
  send({ type: "speed", speed: state.speed });
  setLive(`Live · ${state.speed}×`);
});

document.getElementById("lab-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.seed = document.getElementById("seed").value;
  state.speed = Number(document.getElementById("lab-speed").value);
  markSpeed(state.speed);
  document.querySelector(".tab[data-tab='watch']").click();
  startMatch();
});

const languageNames = { en: "English", fr: "French", es: "Spanish", hi: "Hindi" };

const personaNames = { casual: "Casual", analyst: "Analyst", fan: "Home fan", player: "Player", broadcaster: "Broadcaster" };

function comparisonViews() {
  return state.comparisonViewers.map(viewer => ({ ...viewer }));
}

function matchesView(overlay, viewer) {
  return overlay && viewer && overlay.persona === viewer.persona && overlay.language === viewer.language && overlay.team_id === viewer.team_id && (viewer.persona !== "player" || overlay.player_id === viewer.player_id);
}

function isNeutralViewer(viewer) {
  return viewer.persona === "analyst" || viewer.persona === "broadcaster";
}

function normalizeViewer(viewer) {
  viewer.team_id = isNeutralViewer(viewer) ? null : (viewer.team_id || "RED");
  return viewer;
}

function viewerLabel(viewer) {
  return `${personaNames[viewer.persona]} · ${isNeutralViewer(viewer) ? "Neutral" : teamLabel(viewer.team_id, true)} · ${languageNames[viewer.language] || viewer.language}`;
}

function viewerLabelMarkup(viewer) {
  const icons = {
    analyst: '<path d="M4 4v16h16M8 16v-4m5 4V8m5 8V4"/>',
    broadcaster: '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M6 11v1a6 6 0 0 0 12 0v-1m-6 7v3m-4 0h8"/>',
    fan: '<path d="M5 21V4m0 1c5-5 9 5 14 0v10c-5 5-9-5-14 0"/>',
    player: '<path d="m8 3-6 4 3 5 3-2v11h8V10l3 2 3-5-6-4a4 4 0 0 1-8 0Z"/>',
    casual: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  };
  const persona = Object.hasOwn(icons, viewer.persona) ? viewer.persona : "casual";
  return `<span class="persona-icon persona-icon--${persona}" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" focusable="false">${icons[persona]}</svg></span><span>${escapeHtml(viewerLabel(viewer))}</span>`;
}

function updatePrimaryLabel() {
  document.getElementById("primary-label").innerHTML = viewerLabelMarkup(state.viewer);
}

function renderComparison(onlyIndex = null, fresh = false) {
  state.comparisonViewers.forEach((viewer, index) => {
    if (onlyIndex !== null && index !== onlyIndex) return;
    renderEditorialCard(document.getElementById(`compare-card-${index}`), document.getElementById(`view-proof-${index}`), viewer, state.comparisonEditorial[index], `view-${index}`, fresh);
  });
  renderPulse();
  renderAudioControls();
}

function audioStory(key) {
  if (!state.speech.available || state.seeking || state.scrubbing || (state.demo.active && state.demo.phase === "preparing")) return null;
  const update = state.viewerDisplays[key];
  if (!update?.speech?.id || !update.result?.overlay) return null;
  const viewer = key === "primary" ? state.viewer : state.comparisonViewers[Number(key.split("-")[1])];
  if (!viewer || !matchesView(update.result.overlay, viewer)) return null;
  return {id: update.speech.id, delivery: update.speech.delivery, label: personaNames[viewer.persona],
          fresh: update.result.moment.expires_at_ms > (state.frame?.timestamp_ms || 0)};
}

function renderAudioControls() {
  const player = state.audioPlayer;
  if (!player) return;
  const views = [{key: "primary", viewer: state.viewer}, ...state.comparisonViewers.map((viewer, index) => ({key: `view-${index}`, viewer}))];
  const select = document.getElementById("audio-view");
  const signature = views.map(({key, viewer}) => `${key}:${viewerLabel(viewer)}`).join("|");
  if (select.dataset.views !== signature) {
    select.innerHTML = views.map(({key, viewer}) => `<option value="${key}">${escapeHtml(viewerLabel(viewer))}</option>`).join("");
    select.dataset.views = signature;
  }
  if (!views.some(view => view.key === player.view)) { player.view = "primary"; player.reset(); return; }
  select.value = player.view; select.disabled = !state.speech.available;
  const auto = document.getElementById("auto-commentary");
  auto.disabled = !state.speech.available; auto.checked = player.auto;
  auto.title = state.speech.message || "Speak new aired stories from one selected perspective";
  const status = document.getElementById("audio-status");
  status.textContent = state.speech.available ? player.message : state.speech.message;
  status.hidden = !status.textContent;
  document.querySelectorAll("[data-listen-view]").forEach(button => {
    const key = button.dataset.listenView;
    const active = key === player.view && player.stage !== "idle";
    const story = audioStory(key);
    const view = views.find(view => view.key === key);
    const label = view ? personaNames[view.viewer.persona] : "viewer";
    button.disabled = !active && !story;
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-label", `${active ? "Stop" : "Listen to"} ${label} ${active ? "commentary" : "update"}`);
    button.title = state.speech.available ? story?.delivery || "Waiting for an aired story" : state.speech.message;
    const text = active ? player.stage === "loading" ? "Cancel" : "Stop" : "Listen";
    if (button.dataset.state !== text) {
      button.dataset.state = text;
      button.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">${active ? '<rect x="6" y="6" width="12" height="12" rx="2"/>' : '<path d="m11 4-6 5H2v6h3l6 5V4Zm4 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>'}</svg><span>${text}</span>`;
    }
  });
}

function positionPopover(popover) {
  popover.addEventListener("beforetoggle", event => {
    if (event.newState !== "open") return;
    const button = document.querySelector(`[popovertarget="${popover.id}"][class*="settings-button"]`);
    const rect = button.getBoundingClientRect();
    const width = Math.min(336, window.innerWidth - 24);
    popover.style.left = `${Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12))}px`;
    popover.style.top = `${Math.max(12, Math.min(rect.bottom + 8, window.innerHeight - 390))}px`;
  });
}

function playerOptions(viewer) {
  const players = state.frame?.players || [];
  if (!players.length) return `<option value="${escapeHtml(viewer.player_id)}">${escapeHtml(shirt(viewer.player_id))}</option>`;
  return players.map(player => `<option value="${escapeHtml(player.player_id)}" ${player.player_id === viewer.player_id ? "selected" : ""}>${escapeHtml(`${shirt(player.player_id)} · ${player.role}`)}</option>`).join("");
}

function fillComparisonPlayers() {
  state.comparisonViewers.forEach((viewer, index) => {
    const select = document.getElementById(`view-player-${index}`);
    if (!select) return;
    const key = state.frame?.players.map(player => player.player_id).join("|") || "";
    if (select.dataset.roster === key) return;
    select.dataset.roster = key;
    if (state.frame && !state.frame.players.some(player => player.player_id === viewer.player_id)) {
      viewer.player_id = state.frame.players[0]?.player_id || viewer.player_id;
      send({ type: "views", views: comparisonViews() });
    }
    select.innerHTML = playerOptions(viewer);
  });
}

function buildComparisonCards() {
  const host = document.getElementById("extra-views");
  host.innerHTML = state.comparisonViewers.map((viewer, index) => `
    <section class="persona-panel">
      <div class="card-top"><span class="view-label" id="view-label-${index}">${viewerLabelMarkup(viewer)}</span><div class="card-actions"><button class="listen-button" type="button" data-listen-view="view-${index}" disabled aria-label="Listen to ${personaNames[viewer.persona]} update">Listen</button><button class="icon-button settings-button" type="button" popovertarget="view-settings-${index}" aria-label="View ${index + 2} settings" title="Viewer settings">⋯</button></div></div>
      <article class="card empty" id="compare-card-${index}" aria-live="polite"></article>
      <div class="why hidden" id="view-proof-${index}"></div>
      <div class="pulse" id="view-pulse-${index}" aria-label="Live match statistics"></div>
      <div id="view-settings-${index}" class="settings-popover" popover>
        <div class="popover-head"><strong>View ${index + 2} settings</strong><button class="icon-button" type="button" popovertarget="view-settings-${index}" popovertargetaction="hide" aria-label="Close view ${index + 2} settings">×</button></div>
        <div class="controls">
          <label>Viewer<select data-field="persona" aria-label="View ${index + 2} persona">${Object.entries(personaNames).map(([value, label]) => `<option value="${value}" ${value === viewer.persona ? "selected" : ""}>${label}</option>`).join("")}</select></label>
          <label ${isNeutralViewer(viewer) ? "hidden" : ""}>Team<select ${isNeutralViewer(viewer) ? "disabled" : ""} data-field="team_id" aria-label="View ${index + 2} team"><option value="BLUE" ${viewer.team_id === "BLUE" ? "selected" : ""}>Manchester City</option><option value="RED" ${viewer.team_id === "RED" ? "selected" : ""}>Liverpool</option></select></label>
          <label>Language<select data-field="language" aria-label="View ${index + 2} language">${Object.entries(languageNames).map(([code, name]) => `<option value="${code}" ${viewer.language === code ? "selected" : ""}>${name}</option>`).join("")}</select></label>
          <label>Focus player<select id="view-player-${index}" data-field="player_id" aria-label="View ${index + 2} focus player" ${viewer.persona === "player" ? "" : "disabled"}>${playerOptions(viewer)}</select></label>
          <p class="viewer-help">Independent selection. Analyst and Broadcaster cover both teams neutrally.</p>
          <button class="remove-view" type="button" data-remove="${index}">Remove this view</button>
        </div>
      </div>
    </section>`).join("");
  host.querySelectorAll(".settings-popover").forEach(positionPopover);
  host.querySelectorAll("section").forEach((panel, index) => {
    panel.querySelectorAll("select").forEach(select => select.addEventListener("change", () => {
      const viewer = state.comparisonViewers[index];
      if (state.audioPlayer.view === `view-${index}`) state.audioPlayer.reset();
      viewer[select.dataset.field] = select.value;
      normalizeViewer(viewer);
      panel.querySelector(".view-label").innerHTML = viewerLabelMarkup(viewer);
      const teamSelect = panel.querySelector('[data-field="team_id"]');
      teamSelect.closest("label").hidden = isNeutralViewer(viewer);
      teamSelect.disabled = isNeutralViewer(viewer);
      teamSelect.value = viewer.team_id || "RED";
      panel.querySelector('[data-field="player_id"]').disabled = viewer.persona !== "player";
      state.comparisonEditorial[index] = null; delete state.viewerDisplays[`view-${index}`];
      state.comparisonOverlays = [];
      renderComparison();
      send({ type: "views", views: comparisonViews() });
    }));
    panel.querySelector(".remove-view").addEventListener("click", () => {
      if (state.audioPlayer.view !== "primary") { state.audioPlayer.view = "primary"; state.audioPlayer.reset(); }
      state.comparisonViewers.splice(index, 1);
      state.comparisonEditorial = [];
      state.viewerDisplays = state.viewerDisplays.primary ? {primary: state.viewerDisplays.primary} : {};
      state.comparisonOverlays = [];
      buildComparisonCards();
      send({ type: "views", views: comparisonViews() });
    });
  });
  const add = document.getElementById("add-view");
  add.disabled = state.comparisonViewers.length >= 2;
  add.title = add.disabled ? "Three views are open" : "Add persona view";
  renderComparison();
}

document.getElementById("add-view").addEventListener("click", () => {
  if (state.comparisonViewers.length >= 2) return;
  state.comparisonEditorial = [];
  state.comparisonViewers.push(normalizeViewer({ ...state.viewer, persona: state.comparisonViewers.length ? "fan" : "analyst" }));
  state.comparisonOverlays = [];
  buildComparisonCards();
  send({ type: "views", views: comparisonViews() });
});
positionPopover(document.getElementById("primary-settings"));
document.getElementById("persona").value = state.viewer.persona;
updatePrimaryLabel();
updateViewerHelp();
buildComparisonCards();

const timeline = document.getElementById("timeline");
timeline.addEventListener("input", () => {
  if (!state.scrubbing) state.audioPlayer.reset();
  state.scrubbing = true;
  const time = clockOf(Number(timeline.value));
  document.getElementById("timeline-time").textContent = time;
  const preview = document.getElementById("seek-preview");
  preview.hidden = false;
  preview.textContent = `Seek to ${time}`;
  const ratio = (Number(timeline.value) - Number(timeline.min)) / (Number(timeline.max) - Number(timeline.min));
  preview.style.left = `clamp(64px, ${ratio * 100}%, calc(100% - 64px))`;
});
timeline.addEventListener("change", () => {
  state.scrubbing = false;
  document.getElementById("seek-preview").hidden = true;
  state.seeking = true;
  timeline.disabled = true;
  document.getElementById("pause").disabled = true;
  document.getElementById("seek-status").textContent = "Rebuilding match and statistics…";
  setLive("Seeking…");
  send({ type: "seek", timestamp_ms: Number(timeline.value) });
});
renderComparison();

function requestDemo(step = 0) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showDemoNotice("Judge demo needs a live connection. Start the backend, then refresh this page to reconnect.");
    return;
  }
  if (!state.serverReady) {
    showDemoNotice("Still connecting to the match server. Try Judge demo again once the connection is ready.");
    return;
  }
  if (!state.demo.stops[step]) {
    showDemoNotice("This backend is running an older version without Judge demo. Stop it with Ctrl+C, restart with .venv/bin/python -m matchos, then refresh this page.");
    return;
  }
  showDemoNotice("");
  state.audioPlayer?.reset();
  if (!state.demo.active) state.candidates = [];
  state.demo = { ...state.demo, active: true, step, requestId: (state.demo.requestId || 0) + 1,
                 phase: "preparing", settled: 0, outcomes: {}, candidates: [] };
  state.scenario = "press_collapse"; state.seed = 47129; state.speed = 1;
  document.getElementById("seed").value = state.seed;
  document.getElementById("lab-speed").value = state.speed;
  markSpeed(1); renderScenarios();
  state.seeking = false;
  clearEvidence();
  document.querySelectorAll(".settings-popover").forEach(popover => {
    if (popover.matches(":popover-open")) popover.hidePopover();
  });
  document.querySelector('[data-tab="watch"]').click();
  renderDemoGuide();
  send({ type: "demo", step, requestId: state.demo.requestId });
}

function showDemoNotice(message) {
  const notice = document.getElementById("demo-notice");
  notice.textContent = message;
  notice.hidden = !message;
}

function receiveDemo(message) {
  if (!state.demo.active || message.step !== state.demo.step || message.requestId !== state.demo.requestId) return;
  if (message.type === "demo_snapshot") {
    const history = state.candidates;
    state.viewer = message.viewers[0]; state.comparisonViewers = message.viewers.slice(1);
    resetUiState();
    state.candidates = history; renderCandidates();
    state.running = false; state.demo.phase = "evaluating";
    document.getElementById("persona").value = state.viewer.persona;
    document.getElementById("language").value = state.viewer.language;
    document.getElementById("team").value = state.viewer.team_id || "RED";
    updatePrimaryLabel(); buildComparisonCards(); updateViewerHelp();
    onMessage({ type: "frame", frame: message.frame, state: message.state, events: message.events });
  } else if (message.type === "demo_progress") {
    state.demo.settled = message.settled;
  } else {
    state.demo.phase = message.phase;
    if (typeof message.running === "boolean") state.running = message.running;
    if (message.candidates) state.demo.candidates = message.candidates.candidates;
    state.demo.message = message.message || "";
    if (message.results) {
      message.results.forEach(result => { state.demo.outcomes[result.index] = result; });
      state.demo.settled = message.results.length;
    }
  }
  renderDemoGuide();
}

function renderDemoGuide() {
  const demo = state.demo;
  document.getElementById("demo-guide").hidden = !demo.active;
  document.getElementById("judge-demo").textContent = demo.active ? "Exit demo" : "Judge demo";
  document.querySelectorAll(".settings-button, #speeds button").forEach(button => { button.disabled = demo.active; });
  document.getElementById("add-view").disabled = demo.active || state.comparisonViewers.length >= 2;
  document.getElementById("timeline").disabled = demo.active;
  document.getElementById("pause").disabled = demo.active;
  document.getElementById("pause").textContent = demo.active ? (demo.phase === "playing" ? "Demo playing" : "Held for demo") : state.running ? "Pause" : "Resume";
  renderAudioControls();
  if (!demo.active) return;
  const checkpoint = demo.stops[demo.step];
  const busy = ["preparing", "evaluating", "playing"].includes(demo.phase);
  document.getElementById("demo-title").textContent = checkpoint.title;
  document.getElementById("demo-description").textContent = checkpoint.description;
  document.getElementById("demo-stops").innerHTML = demo.stops.map((stop, index) =>
    `<button class="demo-stop ${index === demo.step ? "active" : ""}" type="button" data-demo-step="${index}" ${busy ? "disabled" : ""} ${index === demo.step ? 'aria-current="step"' : ""}>${index + 1}. ${escapeHtml(stop.title)} <span>${clockOf(stop.timestamp_ms)}</span></button>`).join("");
  document.getElementById("demo-stops").querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => requestDemo(Number(button.dataset.demoStep)));
  });
  document.getElementById("demo-status").textContent = demo.phase === "preparing" ? "Replaying the match to this checkpoint…" :
    demo.phase === "playing" ? `Playing at 1× · ${demo.settled}/3 viewers finished · the match will pause for comparison.` :
    demo.phase === "evaluating" ? `Match held · ${demo.settled}/3 viewers finished · waiting for model decisions and copy…` :
    demo.phase === "error" ? demo.message : "Match held · 3/3 viewers finished · compare their stories or inspect evidence.";
  document.getElementById("demo-retry").hidden = demo.phase !== "error";
  document.getElementById("demo-evidence").disabled = !demo.candidates?.length;
  const next = document.getElementById("demo-next");
  next.disabled = busy;
  next.textContent = demo.step === demo.stops.length - 1 ? "Finish demo" : "Next checkpoint →";
  const roles = ["broadcaster", "fan", "analyst"];
  const purposes = ["Turning point", "Liverpool's perspective", "Tactics & evidence"];
  document.getElementById("demo-outcomes").innerHTML = roles.map((role, index) => {
    const result = demo.outcomes[index];
    const aired = result?.explanation.status === "update";
    const status = result ? (aired ? `Aired · ${storyName(result.result.moment?.frame)}` : "Held · no interruption") : demo.phase === "error" ? "Did not finish" : "Evaluating…";
    return `<span title="${escapeHtml(result?.result.overlay?.headline || result?.explanation.reason || purposes[index])}"><b>${personaNames[role]}</b><small>${purposes[index]}</small>${escapeHtml(status)}</span>`;
  }).join("");
  setLive(demo.phase === "playing" ? "Judge demo · 1×" : "Judge demo · held");
}

document.getElementById("judge-demo").addEventListener("click", () => state.demo.active ? startMatch() : requestDemo());
document.getElementById("close-evidence").addEventListener("click", clearEvidence);
document.getElementById("demo-next").addEventListener("click", () => {
  if (state.demo.step === state.demo.stops.length - 1) startMatch();
  else requestDemo(state.demo.step + 1);
});
document.getElementById("demo-retry").addEventListener("click", () => requestDemo(state.demo.step));
document.getElementById("demo-evidence").addEventListener("click", () => {
  if (state.demo.candidates?.length) showEvidence(state.demo.candidates[0]);
});

document.addEventListener("click", event => {
  const button = event.target.closest("[data-listen-view]");
  if (!button || button.disabled) return;
  const key = button.dataset.listenView;
  if (state.audioPlayer.view === key && state.audioPlayer.stage !== "idle") state.audioPlayer.stop();
  else void state.audioPlayer.play(key);
});
document.getElementById("auto-commentary").addEventListener("change", event => state.audioPlayer.setAuto(event.target.checked));
document.getElementById("audio-view").addEventListener("change", event => state.audioPlayer.selectView(event.target.value));
window.addEventListener("pagehide", () => state.audioPlayer.reset());

window.addEventListener("resize", resize);
updateViewerHelp();
resize();
requestAnimationFrame(draw);
connect();


const telemetryState = { session: null, revision: 0, runs: [], selected: null, pinnedRun: null };
function pollTelemetry() {
  if (!document.getElementById("panel-telemetry").classList.contains("hidden")) {
    send({ type: "telemetry", session: telemetryState.session, since: telemetryState.revision });
  }
}
setInterval(pollTelemetry, 1000);
function receiveTelemetry(message) {
  if (message.session !== telemetryState.session) {
    telemetryState.runs = []; telemetryState.selected = null; telemetryState.pinnedRun = null;
  }
  if (message.retainedIds) telemetryState.runs = telemetryState.runs.filter(run => message.retainedIds.includes(run.id));
  telemetryState.session = message.session;
  telemetryState.revision = message.revision;
  for (const run of message.runs) {
    const index = telemetryState.runs.findIndex(item => item.id === run.id);
    if (index >= 0) telemetryState.runs[index] = run;
    else telemetryState.runs.push(run);
    if (telemetryState.pinnedRun?.id === run.id) telemetryState.pinnedRun = run;
  }
  const quietRuns = telemetryState.runs.filter(run => run.quiet).sort((a,b) => b.revision-a.revision);
  telemetryState.runs = telemetryState.runs.filter(run => !run.quiet).slice(-40).concat(quietRuns.slice(0,1));
  const runs = [...telemetryState.runs];
  // Retain one explicitly selected snapshot even after server history rotates.
  if (telemetryState.pinnedRun && !runs.some(run => run.id === telemetryState.pinnedRun.id)) runs.push(telemetryState.pinnedRun);
  runs.sort((a,b) => b.revision-a.revision);
  if (!telemetryState.selected || !runs.some(run => run.id === telemetryState.selected)) telemetryState.selected = runs.find(run => !run.quiet)?.id || runs[0]?.id;
  const list = document.getElementById("telemetry-runs");
  const scrollTop = list.scrollTop;
  const existing = new Map([...list.children].map(button => [button.dataset.run, button]));
  runs.forEach((run, index) => {
    let button = existing.get(run.id);
    if (!button) {
      button = document.createElement("button"); button.dataset.run = run.id;
      button.append(document.createElement("strong"), document.createElement("span"));
      button.addEventListener("click", () => {
        telemetryState.selected = run.id;
        telemetryState.pinnedRun = telemetryState.runs.find(item => item.id === run.id) || telemetryState.pinnedRun;
        receiveTelemetry({ session: telemetryState.session, revision: telemetryState.revision, runs: [] });
      });
    }
    button.className = `trace-run ${run.id === telemetryState.selected ? "selected" : ""}`;
    button.setAttribute("aria-pressed", String(run.id === telemetryState.selected));
    const label = `${clockOf(run.metadata.timestamp_ms || 0)} · ${run.quiet ? "Quiet tick" : run.label}`;
    const detail = `${run.metadata.viewer?.persona || ""} · ${run.status} · ${run.duration_ms ?? "…"} ms`;
    if (button.children[0].textContent !== label) button.children[0].textContent = label;
    if (button.children[1].textContent !== detail) button.children[1].textContent = detail;
    if (list.children[index] !== button) list.insertBefore(button, list.children[index] || null);
    existing.delete(run.id);
  });
  for (const button of existing.values()) button.remove();
  list.scrollTop = scrollTop;
  renderTelemetryRun(runs.find(run => run.id === telemetryState.selected));
  document.getElementById("export-trace").disabled = !runs.length;
}
function readableTrace(value) {
  if (Array.isArray(value)) return value.map(readableTrace);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key,item]) => {
    if (key === "content" && typeof item === "string" && /^[{[]/.test(item)) {
      try { return [key, readableTrace(JSON.parse(item))]; } catch (_) { /* Keep non-JSON text. */ }
    }
    return [key, readableTrace(item)];
  }));
  return value;
}
function executionSummary(run) {
  const steps = run.steps || [];
  if (run.label === "Spoken commentary") {
    const speech = steps.find(step => step.name.startsWith("speech:"));
    const cached = steps.some(step => step.name === "speech cache");
    const shared = steps.some(step => step.name === "speech shared synthesis");
    return [["Speech", speech ? speech.name.substring(speech.name.indexOf(":") + 1).trim() : cached ? "Cached audio" : shared ? "Shared synthesis" : "Waiting"],
            ["Delivery", speech?.input?.delivery || (cached ? "Previously synthesized" : "—")],
            ["Audio", run.status === "running" ? "Preparing" : run.status === "complete" ? "Ready" : run.status],
            ["Total", `${Math.round(run.duration_ms || 0)} ms`]];
  }
  const decisions = steps.filter(step => step.name.startsWith("editorial selection:") || step.name.startsWith("decision:"));
  const language = steps.find(step => step.name.startsWith("reasoning:"));
  const production = [...steps].reverse().find(step => step.name === "viewer production" || step.name === "producer");
  const output = production?.output || {};
  const parsedDecision = [...steps].reverse().find(step => step.name === "editorial selection")?.output;
  const candidateSelection = output.selection || parsedDecision || decisions[decisions.length - 1]?.output;
  const selection = candidateSelection && Object.hasOwn(candidateSelection, "selected_candidate_id") ? candidateSelection : null;
  const producer = output.producer || (output.action ? output : null);
  const decision = decisions[0];
  const describe = step => step ? `${step.name.substring(step.name.indexOf(":") + 1).trim()} · ${Math.round(step.duration_ms || 0)} ms${step.status === "fallback" ? " · fallback" : ""}` : "Not called";
  const selected = selection?.selected_candidate_id ? selection.primary_frame?.replaceAll("_", " ") || selection.selected_candidate_id : selection ? "No update selected" :
    run.quiet ? "No evaluation" : run.status === "running" ? "Awaiting selection" : decisions.length ? "No selection available" : "Candidate pool only";
  const outcomes = { AIR: "Aired", SUPPRESS: "Held", REJECT: "Rejected" };
  const outcome = producer ? outcomes[producer.action] || producer.action : run.quiet ? "No evaluation needed" :
    run.status === "running" ? "In progress" : run.status === "cancelled" ? "Cancelled" :
    run.status === "error" ? "Failed" : decisions.length ? "Evaluation complete" : "Evidence prepared";
  return [["Decision", describe(decision)], ["Language", describe(language)],
          ["Story", selected], ["Publication", outcome], ["Total", `${Math.round(run.duration_ms || 0)} ms${run.status === "running" ? " · running" : ""}`]];
}
function renderTelemetryRun(run) {
  const host = document.getElementById("telemetry-detail");
  if (!run) {
    if (host.dataset.run) host.replaceChildren();
    host.dataset.run = ""; host.dataset.revision = "";
    host.textContent = "Waiting for execution traces…"; return;
  }
  const sameRun = host.dataset.run === run.id;
  if (sameRun && host.dataset.revision === String(run.revision)) return;
  if (!sameRun) {
    host.replaceChildren(document.createElement("h2"), document.createElement("p"), document.createElement("dl"));
    host.children[1].className = "note";
    host.children[2].className = "execution-summary";
  }
  host.dataset.run = run.id; host.dataset.revision = String(run.revision);
  host.children[0].textContent = run.label;
  host.children[1].textContent = `${run.id} · ${run.status}${run.error ? " · " + run.error : ""}${run.quiet ? " · Trigger did not fire; no model call" : ""}${run.trace_id ? " · Trace ID " + run.trace_id : ""}`;
  const summary = host.children[2];
  summary.replaceChildren(...executionSummary(run).map(([label, value]) => {
    const item = document.createElement("div"), title = document.createElement("dt"), detail = document.createElement("dd");
    title.textContent = label; detail.textContent = value; item.append(title, detail); return item;
  }));
  const max = Math.max(run.duration_ms || 0, ...run.steps.map(step => step.offset_ms + step.duration_ms), 1);
  const existing = new Map([...host.querySelectorAll("details")].map(item => [item.dataset.step, item]));
  run.steps.forEach((step, index) => {
    const key = `${run.id}:${step.call_id || index}`;
    let details = existing.get(key);
    if (!details) {
      details = document.createElement("details"); details.className = "trace-step"; details.dataset.step = key;
      const summary = document.createElement("summary");
      const track = document.createElement("div"); track.className = "trace-track"; track.append(document.createElement("span"));
      const content = document.createElement("div"); content.className = "trace-payload";
      for (const label of ["Input", "Output"]) {
        const section = document.createElement("div"), title = document.createElement("p"), pre = document.createElement("pre");
        title.textContent = label; pre.tabIndex = 0; pre.setAttribute("aria-label", `${step.name} ${label.toLowerCase()}`);
        section.append(title, pre); content.append(section);
      }
      details.append(summary, track, content); host.append(details);
    }
    details.querySelector("summary").textContent = `${step.name} · ${step.status} · ${step.duration_ms} ms${step.call_id ? " · call " + step.call_id : ""}`;
    const bar = details.querySelector(".trace-track span");
    bar.style.marginLeft = `${Math.max(0,step.offset_ms)/max*100}%`; bar.style.width = `${Math.max(0.6,step.duration_ms/max*100)}%`;
    const payloads = [step.input, step.responses?.length > 1 ? step.responses : step.output];
    details.querySelectorAll("pre").forEach((pre, column) => {
      const value = payloads[column];
      const text = value === null ? "—" : JSON.stringify(readableTrace(value),null,2);
      if (pre.textContent !== text) {
        const top = pre.scrollTop, left = pre.scrollLeft;
        pre.textContent = text;
        pre.scrollTop = top; pre.scrollLeft = left;
      }
    });
    existing.delete(key);
  });
  for (const details of existing.values()) details.remove();
}

document.getElementById("export-trace").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(telemetryState, null, 2)], {type:"application/json"});
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url; link.download = "matchos-traces.json"; link.click(); URL.revokeObjectURL(url);
});
