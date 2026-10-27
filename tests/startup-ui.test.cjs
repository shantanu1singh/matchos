const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Load every browser script in the real HTML order. Isolated controller tests
// cannot catch a cross-script reference error that breaks page initialization.
function boot({ insecure = false, blockedChannel = false, brokenOutput = false } = {}) {
  const elements = new Map(), sockets = [], errors = [];
  function get(id) {
    if (!elements.has(id)) elements.set(id, {
      id, dataset: {}, style: {}, value: '', textContent: '', innerHTML: '',
      disabled: false, hidden: false, handlers: {},
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      addEventListener(event, handler) { this.handlers[event] = handler; },
      querySelectorAll: () => [], querySelector: () => null,
      closest: () => ({ hidden: false }), setAttribute() {},
      getContext: () => ({ setTransform() {} }),
      parentElement: { getBoundingClientRect: () => ({ width: 1000, height: 600 }) },
    });
    return elements.get(id);
  }
  class WebSocket {
    static OPEN = 1;
    constructor() { this.readyState = 1; this.sent = []; sockets.push(this); }
    send(payload) { this.sent.push(JSON.parse(payload)); }
  }
  const context = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [], addEventListener() {} },
    location: { protocol: 'http:', host: '192.168.1.10:8000' },
    window: { addEventListener() {} }, WebSocket,
    crypto: insecure ? {} : { randomUUID: () => 'test-stream' },
    BroadcastChannel: class {
      constructor() { if (blockedChannel) throw Error('Browser policy'); }
      postMessage() {}
    },
    console: { error: (...args) => errors.push(args) },
    performance: { now: () => 1000 }, requestAnimationFrame() {}, setInterval() {},
    setTimeout, clearTimeout, AbortController, URL,
  });
  const html = fs.readFileSync('matchos/web/index.html', 'utf8');
  for (const [, script] of html.matchAll(/<script src="\/assets\/([^"?]+)"/g)) {
    if (script === 'app.js' && brokenOutput) {
      vm.runInContext('window.MatchOSDelivery.create = () => { throw Error("Output unavailable"); };', context);
    }
    vm.runInContext(fs.readFileSync(`matchos/web/${script}`, 'utf8'), context, { filename: script });
  }
  return { get, sockets, errors };
}

for (const [name, options] of [
  ['normal browser', {}],
  ['HTTP LAN browser without randomUUID', { insecure: true }],
  ['browser blocking BroadcastChannel', { blockedChannel: true }],
  ['unavailable optional output tools', { brokenOutput: true }],
]) {
  test(`opening the app automatically starts playback: ${name}`, () => {
    const ui = boot(options);
    assert.equal(ui.sockets.length, 1);
    const socket = ui.sockets[0];
    socket.onopen();
    socket.onmessage({ data: JSON.stringify({
      type: 'hello', scenarios: [{ id: 'press_collapse', startMinute: 50 }],
    }) });
    assert.equal(socket.sent.length, 1);
    assert.equal(socket.sent[0].type, 'start');
    assert.equal(socket.sent[0].viewer.persona, 'broadcaster');
    assert.equal(socket.sent[0].views.length, 2);
    assert.equal(ui.get('pause').textContent, 'Starting…');
    assert.equal(ui.get('pause').disabled, true);
    assert.equal(ui.get('clock').textContent, '50:00');
    socket.onmessage({ data: JSON.stringify({ type: 'status', running: true }) });
    assert.equal(ui.get('pause').textContent, 'Pause');
    assert.equal(ui.get('pause').disabled, false);
    assert.equal(ui.get('live-pill').textContent, 'Live · 1×');
    assert.equal(ui.errors.length, options.brokenOutput ? 1 : 0);
    if (options.blockedChannel) assert.equal(ui.get('open-renderer').disabled, true);
    assert.equal(typeof ui.get('demo-next').handlers.click, 'function');
    assert.equal(typeof ui.get('close-evidence').handlers.click, 'function');
  });
}
