// Session telemetry inspector: stable selections, payload scrolling and JSON export.
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
