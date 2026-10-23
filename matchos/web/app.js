const state = {
  ws: null,
  revision: 0,
  seeking: false,
  scrubbing: false,
  comparisonMoment: null,
  comparisonViewers: [],
  comparisonOverlays: [],
  scenarios: [],
  scenario: "press_collapse",
  seed: 47129,
  speed: 1,
  running: false,
  viewer: { persona: "casual", team_id: "BLUE", language: "en", player_id: "red_7" },
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
};

const pitch = document.getElementById("pitch");
const ctx = pitch.getContext("2d");

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => setLive("Live");
  state.ws.onclose = () => setLive("Reconnecting");
  state.ws.onmessage = (event) => onMessage(JSON.parse(event.data));
}

function send(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(payload));
}

function onMessage(message) {
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
    state.frameAt = now;
    if (!state.prev || state.frame.timestamp_ms < state.prev.timestamp_ms || nextAction !== prevAction) state.trail = [];
    state.pulse = message.state;
    const arrivals = message.events || [];
    queueImportantMatchEvents(arrivals);
    const shown = arrivals.filter((item) => RAW_TYPES.has(item.type));
    if (shown.length) state.raw = state.raw.concat(shown).slice(-12);
    renderRaw();
    renderPulse();
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
    if (message.result.producer && message.result.producer.action === "AIR") {
      state.moment = message.result.moment;
      state.overlay = message.result.overlay;
      state.whyOpen = false;
      const timings = message.result.timings_ms || {};
      const fast = Math.round(timings.fast || timings.template || 0);
      document.getElementById("latency").textContent = fast ? `fast ${fast} ms` : "fast";
      renderInsight(true);
    }
    renderCandidates();
    return;
  }
  if (message.type === "enrichment") {
    const overlay = message.result.overlay;
    const moment = message.result.moment;
    const momentId = moment && moment.id;
    const revision = state.revision;
    window.setTimeout(() => {
      if (state.revision !== revision || !state.moment || state.moment.id !== momentId) return;
      state.overlay = overlay;
      renderInsight(false);
    }, 700);
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

function shirt(id) {
  if (!id || !String(id).includes("_")) return "";
  const [team, number] = String(id).split("_");
  return `${team.toUpperCase()} ${number}`;
}

function xy(point) {
  if (!point || point.x == null || point.y == null) return "";
  return `${Number(point.x).toFixed(1)},${Number(point.y).toFixed(1)}`;
}

function clockOf(ms) {
  const minute = String(Math.floor(ms / 60000)).padStart(2, "0");
  const second = String(Math.floor((ms / 1000) % 60)).padStart(2, "0");
  return `${minute}:${second}`;
}

function eventLine(event) {
  const who = shirt(event.player_id) || event.team_id || "";
  const target = event.target_player_id ? ` → ${shirt(event.target_player_id)}` : "";
  const outcome = event.outcome ? `  ${event.outcome}` : "";
  const from = xy(event.location);
  const to = xy(event.end_location);
  const path = from && to ? `<span class="path">${escapeHtml(`${from} → ${to}`)}</span>` : "";
  return `<b>${escapeHtml(event.type)}</b> ${escapeHtml(`${clockOf(event.timestamp_ms)}  ${who}${target}${outcome}`)}${path}`;
}

function playerPoint(id) {
  const players = state.frame && state.frame.players;
  if (!players || !id) return null;
  const player = players.find((item) => item.player_id === id);
  return player ? { x: player.x, y: player.y } : null;
}

function metres(a, b) {
  if (!a || !b || a.x == null || b.x == null) return null;
  return Math.hypot(Number(a.x) - Number(b.x), Number(a.y) - Number(b.y));
}

function renderRaw() {
  const log = document.getElementById("raw-log");
  const anim = document.getElementById("raw-anim");
  if (!log || !anim) return;
  const rows = state.raw.slice(-5).reverse();
  log.innerHTML = rows.length
    ? rows.map((event, index) => `<li class="${index === 0 ? "now" : ""}">${eventLine(event)}</li>`).join("")
    : "<li>Waiting for the first event</li>";
  const action = state.frame && state.frame.action;
  if (!action) {
    anim.textContent = "Animation  no ball flight";
    return;
  }
  const runner = playerPoint(action.receiver_id);
  const gap = metres(runner, action.end);
  const meets = action.receiver_id
    ? ` · meets ${shirt(action.receiver_id)}${runner ? ` now ${xy(runner)}` : ""}${gap == null ? "" : ` · ${gap.toFixed(1)}m from end`}`
    : "";
  const ball = state.frame.ball ? ` · ball now ${xy(state.frame.ball)}` : "";
  anim.textContent = `Animation  ${action.kind}  ${xy(action.start)} → ${xy(action.end)}${meets}${ball}`;
}

function resetUiState() {
  state.revision += 1;
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
}

function startMatch() {
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
  const minute = String(Math.floor(state.frame.timestamp_ms / 60000)).padStart(2, "0");
  const second = String(Math.floor(state.frame.timestamp_ms / 1000) % 60).padStart(2, "0");
  document.getElementById("clock").textContent = `${minute}:${second}`;
}

function renderInsight(fresh) {
  const card = document.getElementById("insight");
  const overlay = state.overlay;
  const moment = state.moment;
  if (!overlay || !moment) return;
  card.classList.remove("empty");
  if (fresh) card.classList.add("swap");
  const graphic = overlay.graphic
    ? `<div class="graphic">
        <div>CONF ${overlay.graphic.confidence}</div>
        <div>TTL ${overlay.graphic.ttl_sec}s · ${overlay.graphic.priority}</div>
        <button class="air" type="button">Put on air</button>
      </div>`
    : "";
  card.innerHTML = `
    <p class="kicker">${escapeHtml(overlay.kicker)} · ${escapeHtml(overlay.source)}</p>
    <h2>${escapeHtml(overlay.headline)}</h2>
    <p class="body">${escapeHtml(overlay.body)}</p>
    ${graphic}`;
  const air = card.querySelector(".air");
  if (air) air.addEventListener("click", () => { air.textContent = "On air"; });
  renderWhy();
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
  const evidence = (moment.evidence_detail || []).map(item => `<li><b>${clockOf(item.timestamp_ms)}</b> ${escapeHtml(item.label)} · ${escapeHtml(item.outcome || item.type)}<small>${escapeHtml(item.id)}</small></li>`).join("");
  const checks = (moment.checks || []).map(check => `<li><b>${check.passed ? "PASS" : "FAIL"}</b> ${escapeHtml(check.name)}: ${escapeHtml(check.detail)}</li>`).join("");
  host.innerHTML = `<details class="proof" data-proof="${escapeHtml(key)}" ${state.expandedProof.has(key) ? "open" : ""}>
    <summary>Facts &amp; evidence <span>${Math.round(moment.confidence * 100)}% selection confidence</span></summary>
    <div class="proof-content"><p class="kicker">Verified at ${clockOf(moment.valid_from_ms)}</p>${stats}
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
}

function statCapsules(viewer) {
  const pulse = state.pulse;
  if (!pulse) return [];
  const teamName = viewer.team_id === "BLUE" ? "Blue" : "Red";
  const team = pulse[viewer.team_id.toLowerCase()] || {};
  const share = `${Math.round((team.possession_last5 ?? 0) * 100)}%`;
  if (viewer.persona === "casual") return [`Score ${pulse.score_red}–${pulse.score_blue}`, `${pulse.possession_team === "BLUE" ? "Blue" : "Red"} on the ball`];
  if (viewer.persona === "fan") return [`${teamName} possession ${share}`, `Entries (5′) ${team.final_third_entries_last5 ?? 0}`, `Shots (5′) ${team.shots_last5 ?? 0}`];
  if (viewer.persona === "player") {
    const player = pulse.players?.[viewer.player_id];
    if (!player) return [`${shirt(viewer.player_id)} · no tracking data`];
    return [`${shirt(viewer.player_id)} between lines (8′) ${player.between_lines_last8}`, `Progressive ${player.progressive_actions}`, `Box entries ${player.box_entries}`];
  }
  if (viewer.persona === "broadcaster") return [`Score ${pulse.score_red}–${pulse.score_blue}`, `${teamName} possession ${share}`, `Entries (5′) ${team.final_third_entries_last5 ?? 0}`];
  const bypass = team.press_bypass_rate == null ? "—" : `${Math.round(team.press_bypass_rate * 100)}%`;
  const line = team.defensive_line_m == null ? "—" : `${team.defensive_line_m.toFixed(1)}m`;
  return [`${teamName} bypass ${bypass}`, `${teamName} line ${line}`, `Possession (5′) ${share}`, `Entries (5′) ${team.final_third_entries_last5 ?? 0}`];
}

function renderPulse() {
  const render = (host, viewer) => {
    if (host) host.innerHTML = statCapsules(viewer).map(text => `<span>${escapeHtml(text)}</span>`).join("");
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
  select.innerHTML = players.map((player) => `<option value="${player.player_id}">${player.team_id} ${player.number} · ${player.role}</option>`).join("");
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
  if (isPlayer) {
    help.textContent = `Following ${shirt(state.viewer.player_id)}. MatchOS prioritizes verified moments involving this player; major match moments can still break through.`;
  } else if (state.viewer.persona === "fan") {
    help.textContent = `Home fan view frames verified moments from ${state.viewer.team_id}'s perspective.`;
  } else if (state.viewer.persona === "analyst") {
    help.textContent = "Analyst view favors tactical changes, underlying metrics and denser explanations.";
  } else if (state.viewer.persona === "broadcaster") {
    help.textContent = "Broadcaster view returns short, graphic-ready verified copy and production metadata.";
  } else {
    help.textContent = "Casual view shows fewer moments and explains them with minimal football jargon.";
  }
}

function describeMatchEvent(event) {
  const player = shirt(event.player_id);
  if (event.type === "SHOT" && event.outcome === "GOAL") {
    return { title: "GOAL", detail: player ? `${player} scores` : `${event.team_id} score`, kind: "goal" };
  }
  if (event.type === "PENALTY") return { title: "PENALTY", detail: `${event.team_id} awarded a penalty`, kind: "penalty" };
  if (event.type === "SHOT" && event.qualifiers?.penalty) {
    const title = event.outcome === "SAVED" ? "PENALTY SAVED" : "PENALTY MISSED";
    return { title, detail: player || event.team_id, kind: "penalty" };
  }
  if (event.type === "CARD") {
    const card = String(event.qualifiers?.card || event.outcome || "").toLowerCase();
    return { title: card === "red" ? "RED CARD" : "YELLOW CARD", detail: player || event.team_id, kind: card === "red" ? "red" : "yellow" };
  }
  if (event.type === "OFFSIDE") return { title: "OFFSIDE", detail: player ? `${player} caught offside` : "Flag up", kind: "offside" };
  if (event.type === "FOUL") return { title: "FOUL", detail: player ? `Committed by ${player}` : event.team_id, kind: "foul" };
  if (event.type === "FREE_KICK") return { title: "FREE KICK", detail: `${event.team_id} restart`, kind: "free-kick" };
  if (event.type === "CORNER") return { title: "CORNER", detail: `${event.team_id} corner kick`, kind: "corner" };
  if (event.type === "THROW_IN") return { title: "THROW-IN", detail: `${event.team_id} restart`, kind: "throw-in" };
  if (event.type === "GOAL_KICK") return { title: "GOAL KICK", detail: `${event.team_id} restart`, kind: "goal-kick" };
  if (event.type === "FORMATION_CHANGE") return { title: "FORMATION CHANGE", detail: event.outcome || event.qualifiers?.formation || event.team_id, kind: "formation" };
  if (event.type === "SUBSTITUTION") return { title: "SUBSTITUTION", detail: player ? `${player} comes on` : event.team_id, kind: "substitution" };
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
  if (view) {
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
  const colour = action.team_id === "RED" ? "rgba(255,93,82," : "rgba(91,147,255,";
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
    ctx.fillStyle = player.team_id === "RED" ? "#ff5d52" : "#5b93ff";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(0,0,0,0.35)";
    ctx.stroke();
    ctx.fillStyle = "#08110d";
    ctx.font = "700 10px Outfit, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(player.number), x, y + 0.5);
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

const personaNames = { casual: "Casual", analyst: "Analyst", fan: "Home fan", player: "Player", broadcaster: "Broadcaster" };

function comparisonViews() {
  return state.comparisonViewers.map(viewer => ({ ...viewer }));
}

function matchesView(overlay, viewer) {
  return overlay && viewer && overlay.persona === viewer.persona && overlay.language === viewer.language && overlay.team_id === viewer.team_id && (viewer.persona !== "player" || overlay.player_id === viewer.player_id);
}

function updatePrimaryLabel() {
  document.getElementById("primary-label").textContent = `${personaNames[state.viewer.persona]} · ${state.viewer.team_id === "BLUE" ? "Blue" : "Red"} · ${state.viewer.language === "hi" ? "Hindi" : "English"}`;
}

function renderComparison() {
  state.comparisonViewers.forEach((viewer, index) => {
    const card = document.getElementById(`compare-card-${index}`);
    if (!card) return;
    const overlay = state.comparisonOverlays[index];
    const valid = matchesView(overlay, viewer);
    card.classList.toggle("empty", !valid);
    card.innerHTML = valid
      ? `<p class="kicker">${escapeHtml(overlay.kicker)} · ${escapeHtml(overlay.source)}</p><h2>${escapeHtml(overlay.headline)}</h2><p class="body">${escapeHtml(overlay.body)}</p>`
      : '<p class="kicker">Same verified facts</p><h2>Waiting for a moment</h2><p class="body">This perspective updates with the live insight.</p>';
    renderProof(document.getElementById(`view-proof-${index}`), valid ? state.comparisonMoment : null, overlay, `view-${index}`);
  });
  renderPulse();
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
  return players.map(player => `<option value="${escapeHtml(player.player_id)}" ${player.player_id === viewer.player_id ? "selected" : ""}>${escapeHtml(`${player.team_id} ${player.number} · ${player.role}`)}</option>`).join("");
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
      <div class="card-top"><span class="view-label" id="view-label-${index}">${escapeHtml(personaNames[viewer.persona])} · ${viewer.team_id === "BLUE" ? "Blue" : "Red"} · ${viewer.language === "hi" ? "Hindi" : "English"}</span><button class="icon-button settings-button" type="button" popovertarget="view-settings-${index}" aria-label="View ${index + 2} settings" title="Viewer settings">⋯</button></div>
      <article class="card empty" id="compare-card-${index}" aria-live="polite"></article>
      <div class="why hidden" id="view-proof-${index}"></div>
      <div class="pulse" id="view-pulse-${index}" aria-label="Live match statistics"></div>
      <div id="view-settings-${index}" class="settings-popover" popover>
        <div class="popover-head"><strong>View ${index + 2} settings</strong><button class="icon-button" type="button" popovertarget="view-settings-${index}" popovertargetaction="hide" aria-label="Close view ${index + 2} settings">×</button></div>
        <div class="controls">
          <label>Viewer<select data-field="persona" aria-label="View ${index + 2} persona">${Object.entries(personaNames).map(([value, label]) => `<option value="${value}" ${value === viewer.persona ? "selected" : ""}>${label}</option>`).join("")}</select></label>
          <label>Team<select data-field="team_id" aria-label="View ${index + 2} team"><option value="BLUE" ${viewer.team_id === "BLUE" ? "selected" : ""}>Blue</option><option value="RED" ${viewer.team_id === "RED" ? "selected" : ""}>Red</option></select></label>
          <label>Language<select data-field="language" aria-label="View ${index + 2} language"><option value="en" ${viewer.language === "en" ? "selected" : ""}>English</option><option value="hi" ${viewer.language === "hi" ? "selected" : ""}>Hindi</option></select></label>
          <label>Focus player<select id="view-player-${index}" data-field="player_id" aria-label="View ${index + 2} focus player" ${viewer.persona === "player" ? "" : "disabled"}>${playerOptions(viewer)}</select></label>
          <p class="viewer-help">Same verified moment. Choose an independent persona, team and language.</p>
          <button class="remove-view" type="button" data-remove="${index}">Remove this view</button>
        </div>
      </div>
    </section>`).join("");
  host.querySelectorAll(".settings-popover").forEach(positionPopover);
  host.querySelectorAll("section").forEach((panel, index) => {
    panel.querySelectorAll("select").forEach(select => select.addEventListener("change", () => {
      const viewer = state.comparisonViewers[index];
      viewer[select.dataset.field] = select.value;
      panel.querySelector(".view-label").textContent = `${personaNames[viewer.persona]} · ${viewer.team_id === "BLUE" ? "Blue" : "Red"} · ${viewer.language === "hi" ? "Hindi" : "English"}`;
      panel.querySelector('[data-field="player_id"]').disabled = viewer.persona !== "player";
      state.comparisonOverlays = [];
      renderComparison();
      send({ type: "views", views: comparisonViews() });
    }));
    panel.querySelector(".remove-view").addEventListener("click", () => {
      state.comparisonViewers.splice(index, 1);
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
  state.comparisonViewers.push({ ...state.viewer, persona: state.comparisonViewers.length ? "fan" : "analyst" });
  state.comparisonOverlays = [];
  buildComparisonCards();
  send({ type: "views", views: comparisonViews() });
});
positionPopover(document.getElementById("primary-settings"));
updatePrimaryLabel();

const timeline = document.getElementById("timeline");
timeline.addEventListener("input", () => {
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
    host.replaceChildren(document.createElement("h2"), document.createElement("p"));
    host.children[1].className = "note";
  }
  host.dataset.run = run.id; host.dataset.revision = String(run.revision);
  host.children[0].textContent = run.label;
  host.children[1].textContent = `${run.id} · ${run.status}${run.error ? " · " + run.error : ""}${run.quiet ? " · Trigger did not fire; no model call" : ""}`;
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
