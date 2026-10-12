const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Minimal DOM surface for exercising live updates without a server or providers.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.handlers = {}; this.scrollTop = 0; this.scrollLeft = 0; this._text = ''; this.className = ''; }
  get textContent() { return this._text; }
  set textContent(value) { this._text = value; this.replaceChildren(); }
  append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  insertBefore(child, anchor) { child.remove(); child.parent = this; this.children.splice(anchor ? this.children.indexOf(anchor) : this.children.length, 0, child); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute() {}
  querySelectorAll(selector) {
    const found = [];
    const walk = el => { for (const child of el.children) { if (child.tag === selector) found.push(child); walk(child); } };
    walk(this); return found;
  }
  querySelector(selector) {
    if (selector === '.trace-track span') return this.children.find(el => el.className === 'trace-track').children[0];
    return this.querySelectorAll(selector)[0];
  }
}
function inspector() {
  const elements = Object.fromEntries(['telemetry-detail','telemetry-runs','export-trace'].map(id => [id,new Element('div')]));
  const context = vm.createContext({ document: { getElementById: id => elements[id], createElement: tag => new Element(tag) }, setInterval() {}, clockOf: ms => String(ms), send() {} });
  const source = fs.readFileSync('matchos/web/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('const telemetryState =')), context);
  return { elements, receive: context.receiveTelemetry };
}
function run(id, revision, quiet = false) {
  return { id, revision, quiet, label:'Workflow', status:'running', metadata:{timestamp_ms:100}, steps:[{name:'provider', call_id:'call-1', status:'request', offset_ms:0, duration_ms:0, input:{evidence:Array(100).fill('fact')}, output:{tokens:Array(100).fill('text')}}] };
}
test('payload scrolling and disclosures survive polling and an in-flight response', () => {
  const {elements,receive} = inspector();
  receive({session:'match',revision:1,runs:[run('chosen',1)]});
  const detail = elements['telemetry-detail'].querySelector('details');
  const [input,output] = detail.querySelectorAll('pre');
  detail.open = true; input.scrollTop = 180; input.scrollLeft = 12; output.scrollTop = 240;
  receive({session:'match',revision:2,runs:[run('other',2)]});
  assert.equal(elements['telemetry-detail'].querySelector('details'),detail);
  const updated = run('chosen',3); updated.steps[0].status='response'; updated.steps[0].output.tokens.push('more');
  receive({session:'match',revision:3,runs:[updated]});
  assert.equal(detail.open,true);
  assert.equal(detail.querySelectorAll('pre')[0],input);
  assert.equal(input.scrollTop,180); assert.equal(input.scrollLeft,12);
  assert.equal(output.scrollTop,240);
  assert.match(output.textContent,/more/);
});
test('explicit selection survives quiet-tick replacement and history eviction', () => {
  const {elements,receive} = inspector();
  receive({session:'match',revision:1,runs:[run('quiet-old',1,true)]});
  elements['telemetry-runs'].children[0].handlers.click();
  for (let revision=2;revision<45;revision++) receive({session:'match',revision,retainedIds:[`new-${revision}`],runs:[run(`new-${revision}`,revision,true)]});
  assert.equal(elements['telemetry-detail'].dataset.run,'quiet-old');
  assert.ok(elements['telemetry-runs'].children.some(button => button.dataset.run === 'quiet-old' && button.className.includes('selected')));
  receive({session:'new-match',revision:1,runs:[run('fresh',1)]});
  assert.equal(elements['telemetry-detail'].dataset.run,'fresh');
});
