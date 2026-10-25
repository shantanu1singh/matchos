// Guided demo controller; state and shared UI helpers are supplied by app.js.
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

