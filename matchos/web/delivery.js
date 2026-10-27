/* Reference publication consumer. No match simulation or model calls live here. */
(function (root) {
  "use strict";
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const clock = (ms) =>
    `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
  const label = (view) =>
    `${view.persona === "fan" ? "Supporter" : view.persona[0].toUpperCase() + view.persona.slice(1)} · ${view.language.toUpperCase()}`;
  const words = (value) =>
    String(value ?? "")
      .replaceAll("_", " ")
      .replaceAll("-", " ");
  const modeName = (mode) =>
    ({
      "local-baseline": "Offline baseline",
      "configured-providers": "Configured models",
    })[mode] || words(mode);
  function runDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Date not recorded"
      : `${date.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`;
  }
  function download(name, value) {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  }
  function validPacket(packet) {
    return (
      packet?.schema_version === "matchos.insight.v1" &&
      packet.event === "story.published" &&
      Number.isFinite(packet.clock?.expires_at_ms) &&
      typeof packet.content?.headline === "string"
    );
  }
  function paint(packet, now, connected = true) {
    const host = document.getElementById("output-preview"),
      stamp = document.getElementById("output-clock"),
      status = document.getElementById("output-validity"),
      raw = document.getElementById("output-packet");
    if (!host) return;
    stamp.textContent = clock(now || 0);
    const valid =
      connected &&
      validPacket(packet) &&
      now >= packet.clock.valid_from_ms &&
      now < packet.clock.expires_at_ms;
    host.classList.toggle("output-empty", !valid);
    host.innerHTML = valid
      ? `<p class="kicker">${esc(label(packet.viewer))} · ${esc(packet.content.kicker)}</p><h2>${esc(packet.content.headline)}</h2><p>${esc(packet.content.body)}</p><div class="output-stats">${(packet.content.stats || []).map((s) => `<span>${esc(s.label)} <b>${esc(s.value)}</b></span>`).join("")}</div>`
      : `<p>${connected ? "No current graphic on air" : "Source disconnected · graphic cleared"}</p>`;
    status.textContent = packet
      ? `${valid ? "On air" : "Off air"} · match-time window ${clock(packet.clock.valid_from_ms)}–${clock(packet.clock.expires_at_ms)}`
      : "Waiting for a completed, published story.";
    if (raw && raw.dataset.story !== packet?.story_id) {
      raw.textContent = packet
        ? JSON.stringify(packet, null, 2)
        : "No packet yet";
      raw.dataset.story = packet?.story_id || "";
    }
  }
  function create() {
    if (!document.getElementById("output-view")) return null;
    // This is a channel name, not an access credential. randomUUID is unavailable
    // on plain HTTP LAN previews; output tools must never prevent match startup.
    const session =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `source-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    let channel = null;
    try {
      if (typeof BroadcastChannel !== "undefined")
        channel = new BroadcastChannel(`matchos-output-${session}`);
    } catch {
      // Browser privacy policies can disable cross-window messaging.
    }
    const renderer = document.getElementById("open-renderer");
    renderer.disabled = !channel;
    if (!channel)
      renderer.title = "Cross-window output is unavailable in this browser.";
    let history = { views: [] },
      packets = {},
      now = 0,
      selected = 0,
      lastClock = 0,
      report = null,
      loading = 0;
    const viewSelect = document.getElementById("output-view");
    function share() {
      channel?.postMessage({
        type: "snapshot",
        packet: packets[selected] || null,
        now,
        source: true,
      });
    }
    function render() {
      const signature = JSON.stringify(
        history.views.map((v) => [v.index, v.viewer]),
      );
      if (viewSelect.dataset.signature !== signature) {
        viewSelect.innerHTML = history.views
          .map(
            (v) =>
              `<option value="${v.index}">${esc(label(v.viewer))}</option>`,
          )
          .join("");
        viewSelect.dataset.signature = signature;
      }
      if (!history.views.some((v) => v.index === selected))
        selected = history.views[0]?.index || 0;
      viewSelect.value = String(selected);
      paint(packets[selected], now);
      document.getElementById("export-packet").disabled = !packets[selected];
      const view = history.views.find((v) => v.index === selected);
      document.getElementById("story-timeline").innerHTML =
        (view?.stories || [])
          .map(
            (p) =>
              `<article class="story-thread"><time>${clock(p.published_at_match_ms)}</time><span>${esc(storyTitle(p.frame))}</span><strong>${esc(p.content.headline)}</strong></article>`,
          )
          .join("") ||
        '<p class="note">Only aired stories appear here. Held candidates are in Telemetry.</p>';
      document.getElementById("recap-views").innerHTML = history.views
        .map(
          (v) =>
            `<article class="recap-card"><p class="kicker">${esc(label(v.viewer))}</p><h3>${esc(v.recap.title)}</h3><p class="note">Through ${clock(v.recap.through_ms)}</p>${v.recap.items.map((p) => `<section><time>${clock(p.timestamp_ms)}</time><h4>${esc(p.headline)}</h4><p>${esc(p.body)}</p></section>`).join("") || '<p class="note">No story has aired for this viewer yet.</p>'}</article>`,
        )
        .join("");
      document.getElementById("export-recap").disabled = !history.views.some(
        (v) => v.recap.items.length,
      );
      share();
    }
    viewSelect.addEventListener("change", () => {
      selected = Number(viewSelect.value);
      render();
    });
    document.getElementById("open-renderer").addEventListener("click", () => {
      if (!channel) return;
      window.open(
        `/broadcast#stream=${encodeURIComponent(session)}`,
        "_blank",
        "noopener",
      );
    });
    document.getElementById("export-packet").addEventListener("click", () => {
      if (packets[selected])
        download(
          `matchos-${packets[selected].story_id}.json`,
          packets[selected],
        );
    });
    document.getElementById("export-recap").addEventListener("click", () =>
      download("matchos-recaps.json", {
        schema_version: "matchos.recap.v1",
        ...history,
      }),
    );
    if (channel)
      channel.onmessage = (e) => {
        if (e.data?.type === "subscribe") share();
      };
    if (channel) setInterval(share, 2000);
    async function loadReport(id) {
      const revision = ++loading;
      report = null;
      document.getElementById("export-evaluation").disabled = true;
      document.getElementById("evaluation-results").innerHTML =
        '<p class="note" role="status">Loading this test run…</p>';
      try {
        const response = await fetch(
          `/api/evaluations/${encodeURIComponent(id)}`,
        );
        if (!response.ok) throw Error("Report unavailable");
        const data = await response.json();
        if (revision !== loading) return;
        report = data;
        renderReport(data);
        document.getElementById("export-evaluation").disabled = false;
      } catch {
        if (revision === loading)
          document.getElementById("evaluation-results").innerHTML =
            '<p class="note" role="status">Could not load this test run. Select another run or refresh to retry.</p>';
      }
    }
    async function loadEvaluations() {
      const status = document.getElementById("evaluation-status");
      status.textContent = "Loading saved runs…";
      try {
        const response = await fetch("/api/evaluations");
        if (!response.ok) throw Error("Archive unavailable");
        const data = await response.json();
        const reports = [...data.reports].sort(
          (a, b) =>
            Date.parse(b.created_at) - Date.parse(a.created_at) ||
            b.report_id.localeCompare(a.report_id),
        );
        const select = document.getElementById("evaluation-run");
        const previous = select.value;
        select.innerHTML = reports
          .map(
            (r, i) =>
              `<option value="${esc(r.report_id)}">Run ${reports.length - i} · ${esc(runDate(r.created_at))} · ${esc(modeName(r.mode))} · ${r.sample_size} results</option>`,
          )
          .join("");
        if (!data.reports.length) {
          report = null;
          ++loading;
          document.getElementById("export-evaluation").disabled = true;
          status.textContent =
            "No saved test runs yet. Results appear here after an operator runs and saves the evaluation suite. Viewing this page does not run tests or incur model charges.";
          document.getElementById("evaluation-results").replaceChildren();
          return;
        }
        select.value = reports.some((r) => r.report_id === previous)
          ? previous
          : reports[0].report_id;
        status.textContent = `${data.reports.length} saved test run${data.reports.length === 1 ? "" : "s"} available · newest first · all dates in UTC`;
        await loadReport(select.value);
      } catch {
        status.textContent =
          "Evaluation archive unavailable. Check storage configuration or refresh to retry.";
      }
    }
    document
      .getElementById("evaluation-run")
      .addEventListener("change", (e) => loadReport(e.target.value));
    document
      .getElementById("refresh-evaluations")
      .addEventListener("click", loadEvaluations);
    document
      .getElementById("export-evaluation")
      .addEventListener("click", () => {
        if (report) download(`${report.report_id}.json`, report);
      });
    return {
      receive(message) {
        if (message.type === "story_reset") {
          packets = {};
          if (!message.retain_history) history = { views: [] };
          render();
        }
        if (
          message.type === "broadcast_packet" &&
          validPacket(message.packet)
        ) {
          packets[message.packet.viewer_index] = message.packet;
          render();
        }
        if (message.type === "story_history") {
          const old = history;
          history = message;
          for (const index of Object.keys(packets))
            if (
              JSON.stringify(
                old.views.find((v) => v.index === Number(index))?.viewer,
              ) !==
              JSON.stringify(
                history.views.find((v) => v.index === Number(index))?.viewer,
              )
            )
              delete packets[index];
          render();
        }
      },
      clock(frame) {
        const previous = now;
        now = frame.timestamp_ms;
        const expires = packets[selected]?.clock.expires_at_ms;
        if (
          performance.now() - lastClock > 200 ||
          (previous < expires && now >= expires)
        ) {
          paint(packets[selected], now);
          share();
          lastClock = performance.now();
        }
      },
      disconnect() {
        channel?.postMessage({ type: "disconnect" });
        paint(null, now, false);
      },
      loadEvaluations,
    };
  }
  const storyTitles = {
    PRESS_BREAKDOWN: "Press breakdown",
    MOMENTUM_SWING: "Momentum shift",
    PLAYER_IMPACT: "Player involvement",
    SUBSTITUTE_IMPACT: "Substitute impact",
    GOAL: "Goal",
    COMEBACK: "Comeback",
    RED_CARD: "Red card",
    SHOT_CHANCE: "Chance created",
    NONE: "No story selected",
    INVALID: "Invalid selection",
  };
  const storyTitle = (frame) =>
    storyTitles[frame] || words(frame) || "No story selected";
  const audienceName = (persona) =>
    ({
      broadcaster: "Broadcaster",
      fan: "Liverpool supporter",
      analyst: "Analyst",
      casual: "Casual fan",
      player: "Player follower",
    })[persona] || words(persona);
  const caseInfo = {
    press: [
      "Pressing change",
      "A fresh change in pressing or momentum should earn an update.",
    ],
    goal: [
      "Liverpool goal",
      "The new goal and 2–1 score should take priority.",
    ],
    dismissal: [
      "City red card",
      "The new dismissal and player disadvantage should earn an update.",
    ],
    quiet: [
      "Quiet opening",
      "Stay quiet when there is no verified story; skip model calls.",
    ],
    "recent-update": [
      "Update just aired",
      "Avoid interrupting again with stories that just aired.",
    ],
    expired: ["Story too old", "Hold expired stories and skip model calls."],
    "goal-already-aired": [
      "Goal already shown",
      "Do not repeat the same goal or interrupt with a routine story immediately afterward.",
    ],
    "dismissal-already-aired": [
      "Red card already shown",
      "Do not repeat the same dismissal or invent a new development.",
    ],
  };
  const checkNames = {
    execution_complete: "The workflow finished successfully",
    selection_in_verified_pool:
      "The chosen story came from verified candidates, or no story was selected",
    editorial_expectation:
      "The story choice matched this test’s expected categories",
    publication_expectation:
      "Showing or holding the story matched the expected behavior",
    verified_source: "The published story had verified source evidence",
    published_selected_candidate:
      "The published story matched the model’s selection",
    numbers_supported:
      "The numbers in the published copy were supported by the facts",
    profile_matches:
      "The audience role, team and language matched the test profile",
    fresh_at_publication: "The story was still valid when published",
    latency_budget: "Processing finished within the test’s time budget",
    provider_succeeded: "Requested model calls completed without fallback",
    unnecessary_model_calls: "No unnecessary model calls were made",
  };
  function renderReport(report) {
    const definitions = {
      all_checks: [
        "Tests passing",
        "Results where every applicable check passed, including the authored story and delivery expectations.",
      ],
      numerical_fidelity: [
        "Supported numbers",
        "Published copy passed the numeric fact guard. This does not verify every meaning or causal claim.",
      ],
      repeat_case_restraint: [
        "Repeated stories held",
        "Tests where an already-aired update was correctly held back.",
      ],
      expired_case_restraint: [
        "Expired stories held",
        "Tests where old evidence was correctly kept off air.",
      ],
      quiet_case_restraint: [
        "Quiet play stays quiet",
        "Quiet-opening tests held the story and made no model calls.",
      ],
      fresh_publications: [
        "Published while fresh",
        "Published updates were still inside their valid match-time window.",
      ],
      fallback_rows: [
        "Fallback used",
        "Results using a backup path. Lower is better; 0% means none needed.",
      ],
      timeout_rows: [
        "Model call timed out",
        "Results with a timed-out model call. Lower is better; 0% means none recorded.",
      ],
    };
    const cases = report.cases || [];
    const personas = [...new Set(cases.map((row) => row.persona))];
    const passed = cases.filter((row) => row.passed).length;
    const seconds = (value) =>
      value == null
        ? "Not measured"
        : value < 1000
          ? `${value < 1 ? "<1" : Math.round(value)}ms`
          : `${(value / 1000).toFixed(2)}s`;
    const profileNotes = {
      broadcaster: "Neutral, timely coverage for an on-air audience.",
      fan: "The match from a Liverpool supporter’s perspective.",
      analyst: "Neutral tactical analysis backed by evidence.",
    };
    const metricCards = Object.entries(report.metrics)
      .map(([key, m]) => {
        const [title, description] = definitions[key] || [
          words(key),
          "See the exported results for this check’s definition.",
        ];
        return `<article class="quality-stat"><span>${esc(title)}</span><strong>${m.rate == null ? "Not measured" : `${Math.round(m.rate * 100)}%`}</strong><p>${esc(description)}</p><small>${m.evaluated ? `${m.passed} of ${m.evaluated} applicable tests` : "No applicable tests in this run"}</small></article>`;
      })
      .join("");
    const rows = cases
      .map((row) => {
        const info =
          report.dataset_version === "1" ? caseInfo[row.case_id] : null;
        const checks = Object.entries(row.checks || {});
        const evaluated = checks.filter(([, value]) => value != null);
        const checksPassed = evaluated.filter(
          ([, value]) => value === true,
        ).length;
        const checkList = checks
          .map(
            ([key, value]) =>
              `<li><span class="check-status ${value === false ? "check-failed" : ""}">${value == null ? "Not applicable" : value ? "Passed" : "Failed"}</span><span>${esc(checkNames[key] || words(key))}</span></li>`,
          )
          .join("");
        return `<tr><td><strong>${esc(info?.[0] || words(row.case_id))}</strong>${info ? `<small>Expected: ${esc(info[1])}</small>` : ""}</td><td>${esc(audienceName(row.persona))}</td><td>${esc(storyTitle(row.selected_frame))}</td><td><span class="result-badge ${row.publication === "AIR" ? "result-shown" : "result-held"}">${row.publication === "AIR" ? "Shown" : row.publication === "HOLD" ? "Held back" : esc(words(row.publication))}</span>${row.fallback ? "<small>Backup path used</small>" : ""}</td><td><details class="result-checks"><summary><span class="result-badge ${row.passed ? "result-pass" : "result-fail"}">${row.passed ? "Passed" : "Needs review"}</span><small>${checksPassed} / ${evaluated.length} checks · expand</small></summary><ul>${checkList || "<li>No individual checks recorded.</li>"}</ul></details></td><td>${esc(seconds(row.duration_ms))}</td></tr>`;
      })
      .join("");
    const graders = {
      editorial_selection: "Was the story worth telling?",
      persona_quality: "Did it suit the audience?",
      semantic_grounding: "Did the meaning stay grounded in the evidence?",
      restraint: "Did it know when to stay quiet?",
    };
    document.getElementById("evaluation-results").innerHTML = `
      <section class="evaluation-context" aria-label="Selected test run">
        <p class="kicker">Selected run · ${esc(modeName(report.mode))}</p>
        <h3>${passed} of ${cases.length} test results passed</h3>
        <p>${report.authored_conditions} test situations · ${personas.length} audience perspective${personas.length === 1 ? "" : "s"} · ${esc(runDate(report.created_at))}</p>
        <p>${report.mode === "local-baseline" ? "This is an offline baseline using local decision and rendering code. It does not measure cloud-model quality." : "This run used the configured model providers. The models actually recorded are listed below."}</p>
        <p>These are authored synthetic regression tests, not independent real matches or a general accuracy score. A held story can be the correct result.</p>
      </section>
      <section aria-label="Test audience profiles"><h3>Who are the audience perspectives?</h3><div class="evaluation-audiences">${personas.map((persona) => `<article><b>${esc(audienceName(persona))}</b><p>${esc(profileNotes[persona] || "A configured audience profile tested separately from the other perspectives.")}</p></article>`).join("")}</div><p class="note">These are the profiles saved in this test run. Changing the cards in Watch does not change these results.</p></section>
      <section aria-label="Automatic test checks"><h3>What did the automatic checks find?</h3><p class="note">Each percentage counts only tests where that check applies. Published-copy checks exclude held stories; “Not measured” means there were no applicable tests.</p><div class="quality-grid">${metricCards}</div></section>
      <section class="evaluation-performance" aria-label="Response time and models"><article><h3>How long did a result take?</h3><p><b>${esc(seconds(report.latency.p50))}</b> typical time (median) · <b>${esc(seconds(report.latency.p95))}</b> 95th-percentile time</p><p class="note">The median is the middle result; 95% of measured results finished within the 95th-percentile time. Includes decisions to hold a story and excludes replay preparation. ${report.latency.samples} measurements.</p></article><article><h3>Which models were used?</h3><p>${report.model_calls} recorded model calls across this batch.</p><ul>${report.models.map((model) => `<li>${esc(model)}</li>`).join("")}</ul><p class="note">${esc(report.cost.note)}</p></article></section>
      <section class="grading-summary"><h3>AI review of story quality</h3><p>Automatic checks test rules and supported numbers. A separate Foundry judge can review whether a story is useful, suits its audience and stays grounded in meaning.</p><p>${report.foundry_grading ? `Saved AI review status: ${esc(report.foundry_grading.status)} · judge model: ${esc(report.foundry_grading.judge_model || "not recorded")}` : "No AI review is attached to this run. The passing checks above are not a score for writing quality."}</p>${Object.entries(
        report.foundry_grading?.scores || {},
      )
        .map(
          ([name, s]) =>
            `<p>${esc(graders[name] || words(name))} <b>${s.mean.toFixed(2)} / 1</b> · ${s.evaluated} graded results</p>`,
        )
        .join(
          "",
        )}<p class="note">AI grades are opinions against a rubric, not calibrated accuracy probabilities. Viewing this page does not start grading.</p></section>
      <section class="evaluation-tests"><h3>Each test, explained</h3><p class="note">Read a row from left to right: the situation and expected behavior → the audience → the chosen story → whether it was shown → whether the checks passed → processing time. Expand the test result to see individual checks.</p><p class="evaluation-legend"><span><b>Shown</b> = an update was published.</span><span><b>Held back</b> = no new update was published.</span><span><b>Passed</b> = all applicable checks passed, even if the correct action was to hold.</span></p><div class="quality-table"><table><caption>One row is one test situation evaluated for one audience perspective.</caption><thead><tr><th scope="col">Test situation &amp; expectation</th><th scope="col">Audience</th><th scope="col">Story chosen</th><th scope="col">Delivery</th><th scope="col">Test result</th><th scope="col">Time</th></tr></thead><tbody>${rows}</tbody></table></div></section>
      <details class="packet-proof"><summary>Technical run details</summary><dl class="evaluation-metadata"><dt>Run ID</dt><dd>${esc(report.report_id)}</dd><dt>Dataset version</dt><dd>${esc(report.dataset_version)}</dd><dt>Simulation seed</dt><dd>${report.seed}</dd><dt>Pipeline fingerprint</dt><dd>${esc(report.pipeline_fingerprint || "Not recorded")}</dd></dl></details>`;
  }
  function reference() {
    const stream = new URLSearchParams(location.hash.slice(1)).get("stream");
    if (!stream || typeof BroadcastChannel === "undefined") {
      document.getElementById("output-validity").textContent =
        "Open this renderer from the Broadcast tab in MatchOS.";
      return;
    }
    const channel = new BroadcastChannel(`matchos-output-${stream}`);
    let last = 0,
      packet = null,
      now = 0;
    channel.onmessage = (e) => {
      if (e.data?.type === "snapshot") {
        last = Date.now();
        packet = validPacket(e.data.packet) ? e.data.packet : null;
        now = e.data.now;
        paint(packet, now);
      } else if (e.data?.type === "disconnect") {
        last = 0;
        paint(null, now, false);
      }
    };
    channel.postMessage({ type: "subscribe" });
    setInterval(() => {
      if (Date.now() - last > 5000) paint(packet, now, false);
    }, 1000);
  }
  root.MatchOSDelivery = { create, reference, validPacket };
})(typeof window === "undefined" ? globalThis : window);
