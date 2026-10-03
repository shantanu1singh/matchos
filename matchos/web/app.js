const state = {
  ws: null,
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
  candidates: [],
  selected: 0,
  pulse: null,
  provider: "",
  raw: [],
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
    if (arrivals.some((item) => item.type === "SHOT" && item.outcome === "GOAL")) flashGoal();
    const shown = arrivals.filter((item) => RAW_TYPES.has(item.type));
    if (shown.length) state.raw = state.raw.concat(shown).slice(-12);
    renderRaw();
    renderPulse();
    updateScore();
    fillPlayers();
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
    window.setTimeout(() => {
      if (!state.moment || state.moment.id !== momentId) return;
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

const RAW_TYPES = new Set(["PASS", "SHOT", "CARRY", "DUEL", "INTERCEPTION", "CARD", "SUBSTITUTION", "FOUL", "FORMATION_CHANGE"]);

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

function startMatch() {
  state.raw = [];
  renderRaw();
  send({
    type: "start",
    scenario: state.scenario,
    seed: Number(state.seed),
    speed: Number(state.speed),
    viewer: state.viewer,
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
  const host = document.getElementById("why");
  const moment = state.moment;
  if (!moment) {
    host.classList.add("hidden");
    return;
  }
  host.classList.remove("hidden");
  const stats = (state.overlay?.stats || []).map((stat) => `<div class="stat"><span>${escapeHtml(stat.label)}</span><span>${escapeHtml(stat.value)}</span></div>`).join("");
  const evidence = state.whyOpen
    ? `<ul class="evidence">${(moment.evidence_detail || []).map((item) => `<li>${String(item.minute).padStart(2, "0")}:${String(item.second).padStart(2, "0")} ${escapeHtml(item.label)} · ${escapeHtml(item.outcome || item.type)}</li>`).join("")}</ul>`
    : "";
  host.innerHTML = `<button class="why-toggle" type="button">Why?</button>${state.whyOpen ? stats + evidence : ""}`;
  host.querySelector("button").addEventListener("click", () => {
    state.whyOpen = !state.whyOpen;
    renderWhy();
  });
}

function renderPulse() {
  const host = document.getElementById("pulse");
  const pulse = state.pulse;
  if (!pulse || state.viewer.persona === "casual") {
    host.innerHTML = "";
    return;
  }
  const blue = pulse.blue || {};
  const bypass = blue.press_bypass_rate == null ? "—" : `${Math.round(blue.press_bypass_rate * 100)}%`;
  const line = blue.defensive_line_m == null ? "—" : `${blue.defensive_line_m.toFixed(1)}m`;
  host.innerHTML = `<span>Blue bypass ${bypass}</span><span>Blue line ${line}</span><span>Red possession ${Math.round((pulse.red.possession_last5 || 0) * 100)}%</span>`;
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
    const frame = result.moment?.frame || result.decision?.primary_frame || "QUIET";
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
    <h2>${escapeHtml((result.moment?.frame || decision?.primary_frame || "No moment").replaceAll("_", " "))}</h2>
    <p class="body">${escapeHtml(result.producer?.reason || result.verification?.reason || "Trigger did not continue.")}</p>
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
  if (!state.frame || select.options.length) return;
  const players = [...state.frame.players].sort((a, b) => a.team_id.localeCompare(b.team_id) || a.number - b.number);
  select.innerHTML = players.map((player) => `<option value="${player.player_id}">${player.team_id} ${player.number}</option>`).join("");
  select.value = state.viewer.player_id;
}

function flashGoal() {
  const node = document.getElementById("goal-flash");
  node.classList.remove("show");
  void node.offsetWidth;
  node.classList.add("show");
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
    if (highlights.has(player.player_id)) {
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
  });
});

["persona", "team", "language", "player"].forEach((id) => {
  document.getElementById(id).addEventListener("change", (event) => {
    const map = { persona: "persona", team: "team_id", language: "language", player: "player_id" };
    state.viewer[map[id]] = event.target.value;
    send({ type: "viewer", viewer: state.viewer });
    renderPulse();
  });
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
  state.trail = [];
  state.moment = null;
  state.overlay = null;
  state.candidates = [];
  document.querySelector(".tab[data-tab='watch']").click();
  startMatch();
});

window.addEventListener("resize", resize);
resize();
requestAnimationFrame(draw);
connect();
