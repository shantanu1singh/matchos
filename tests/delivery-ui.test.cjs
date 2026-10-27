const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id))
      elements.set(id, {
        dataset: {},
        textContent: "",
        innerHTML: "",
        disabled: false,
        value: "0",
        classList: { toggle() {} },
        handlers: {},
        addEventListener(event, fn) {
          this.handlers[event] = fn;
        },
        replaceChildren() {
          this.innerHTML = "";
        },
      });
    return elements.get(id);
  };
  let channel;
  const context = {
    window: { open() {} },
    document: { getElementById: get },
    crypto: { randomUUID: () => "test" },
    BroadcastChannel: class {
      constructor() {
        channel = this;
        this.messages = [];
      }
      postMessage(message) {
        this.messages.push(message);
      }
    },
    performance: { now: () => 1000 },
    setInterval() {},
    URLSearchParams,
    Date,
    location: { hash: "#stream=test" },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("matchos/web/delivery.js", "utf8"), context);
  return {
    context,
    get,
    get channel() {
      return channel;
    },
  };
}

const packet = {
  schema_version: "matchos.insight.v1",
  event: "story.published",
  story_id: "story-1",
  viewer_index: 0,
  viewer: { persona: "broadcaster", language: "en" },
  clock: { valid_from_ms: 1000, expires_at_ms: 2000 },
  content: {
    headline: "A <script> story",
    body: "Supported copy",
    kicker: "Momentum",
    stats: [],
  },
  published_at_match_ms: 1000,
};

test("source publishes the same versioned packet and hides expired graphics", () => {
  const fixture = setup(),
    controller = fixture.context.window.MatchOSDelivery.create();
  controller.clock({ timestamp_ms: 1000 });
  controller.receive({ type: "broadcast_packet", packet });
  assert.match(
    fixture.get("output-preview").innerHTML,
    /A &lt;script&gt; story/,
  );
  assert.equal(fixture.channel.messages.at(-1).packet, packet);
  assert.doesNotMatch(fixture.get("output-validity").textContent, /story-1/);
  assert.match(fixture.get("output-packet").textContent, /story-1/);
  controller.receive({
    type: "story_history",
    views: [
      {
        index: 0,
        viewer: packet.viewer,
        stories: [{ ...packet, frame: "GOAL" }],
        recap: { title: "Match so far", through_ms: 1000, items: [] },
      },
    ],
  });
  assert.match(fixture.get("story-timeline").innerHTML, /Goal/);
  assert.doesNotMatch(fixture.get("story-timeline").innerHTML, /story-1/);
  controller.clock({ timestamp_ms: 2000 });
  assert.match(fixture.get("output-preview").innerHTML, /No current graphic/);
  assert.doesNotMatch(
    fixture.get("output-preview").innerHTML,
    /Supported copy/,
  );
  controller.disconnect();
  assert.equal(fixture.channel.messages.at(-1).type, "disconnect");
});

function savedReport() {
  return {
    report_id: "saved-opaque-id",
    created_at: "2026-10-26T14:08:00Z",
    mode: "configured-providers",
    dataset_version: "1",
    seed: 47129,
    sample_size: 3,
    authored_conditions: 3,
    metrics: {
      all_checks: { passed: 2, evaluated: 3, rate: 2 / 3 },
      numerical_fidelity: { passed: 0, evaluated: 0, rate: null },
    },
    latency: { p50: 1000, p95: 2000, samples: 3 },
    model_calls: 4,
    models: ["azure-foundry:Decision-1"],
    cost: { note: "Cost not recorded." },
    cases: [
      {
        case_id: "goal",
        persona: "broadcaster",
        selected_frame: "GOAL",
        publication: "AIR",
        passed: true,
        duration_ms: 2000,
        checks: { publication_expectation: true, profile_matches: true },
      },
      {
        case_id: "quiet",
        persona: "fan",
        selected_frame: "NONE",
        publication: "HOLD",
        passed: true,
        duration_ms: 10,
        checks: {
          publication_expectation: true,
          unnecessary_model_calls: true,
          numbers_supported: null,
        },
      },
      {
        case_id: "press",
        persona: "analyst",
        selected_frame: "PRESS_BREAKDOWN",
        publication: "AIR",
        passed: false,
        duration_ms: 1000,
        fallback: true,
        checks: { numbers_supported: false },
      },
    ],
  };
}

test("saved evaluations explain the selected run, audiences, held results and failed checks", async () => {
  const fixture = setup(),
    report = savedReport();
  fixture.context.fetch = async (url) => ({
    ok: true,
    json: async () =>
      url === "/api/evaluations"
        ? { reports: [report], storage: "azure-blob" }
        : report,
  });
  const controller = fixture.context.window.MatchOSDelivery.create();
  await controller.loadEvaluations();
  const options = fixture.get("evaluation-run").innerHTML;
  assert.match(options, /26 Oct 2026, 14:08 UTC/);
  assert.match(options, /Configured models · 3 results/);
  const html = fixture.get("evaluation-results").innerHTML;
  for (const phrase of [
    "2 of 3 test results passed",
    "3 audience perspectives",
    "Liverpool supporter",
    "The match from a Liverpool supporter",
    "No story selected",
    "Held back",
    "Not applicable",
    "Needs review",
    "The numbers in the published copy were supported",
    "No AI review is attached",
    "Technical run details",
    "No applicable tests in this run",
    "Goal",
  ]) {
    assert.ok(html.includes(phrase), phrase);
  }
  assert.doesNotMatch(html, />PRESS_BREAKDOWN</);
  assert.match(
    html,
    /One row is one test situation evaluated for one audience perspective/,
  );
  assert.equal(fixture.get("export-evaluation").disabled, false);
});

test("offline results and AI grades remain clearly distinguished from automatic checks", async () => {
  const fixture = setup(),
    report = savedReport();
  report.mode = "local-baseline";
  report.foundry_grading = {
    status: "completed",
    judge_model: "test-judge",
    scores: { persona_quality: { mean: 0.85, evaluated: 2 } },
  };
  fixture.context.fetch = async (url) => ({
    ok: true,
    json: async () =>
      url === "/api/evaluations" ? { reports: [report] } : report,
  });
  await fixture.context.window.MatchOSDelivery.create().loadEvaluations();
  const html = fixture.get("evaluation-results").innerHTML;
  assert.match(html, /does not measure cloud-model quality/);
  assert.match(html, /Did it suit the audience/);
  assert.match(html, /0.85 \/ 1/);
  assert.match(html, /not calibrated accuracy probabilities/);
});

test("reference renderer consumes source output without creating a websocket", () => {
  const fixture = setup();
  fixture.context.window.MatchOSDelivery.reference();
  assert.equal(fixture.channel.messages[0].type, "subscribe");
  fixture.channel.onmessage({ data: { type: "snapshot", packet, now: 1000 } });
  assert.match(fixture.get("output-preview").innerHTML, /Supported copy/);
  fixture.channel.onmessage({ data: { type: "disconnect" } });
  assert.match(fixture.get("output-preview").innerHTML, /Source disconnected/);
  assert.equal(
    fixture.context.window.MatchOSDelivery.validPacket({
      ...packet,
      schema_version: "unknown",
    }),
    false,
  );
});
