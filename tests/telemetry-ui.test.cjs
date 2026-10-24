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
  return { elements, receive: context.receiveTelemetry, summarize: context.executionSummary };
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

test('execution summary distinguishes model, fallback, selected story and publication outcome',()=>{
  const {summarize}=inspector();
  const trace=run('viewer',1);
  trace.status='complete'; trace.duration_ms=550;
  trace.steps=[
    {name:'editorial selection: azure-foundry:Decision-1',status:'response',duration_ms:100,output:{selected_candidate_id:'goal',primary_frame:'GOAL'}},
    {name:'reasoning: azure-foundry:gpt-6-luna',status:'fallback',duration_ms:400},
    {name:'viewer production',output:{selection:{selected_candidate_id:'goal',primary_frame:'GOAL'},producer:{action:'AIR'}}},
  ];
  const summary=Object.fromEntries(summarize(trace));
  assert.equal(summary.Decision,'azure-foundry:Decision-1 · 100 ms');
  assert.match(summary.Language,/gpt-6-luna.*fallback/);
  assert.equal(summary.Story,'GOAL'); assert.equal(summary.Publication,'Aired'); assert.equal(summary.Total,'550 ms');
  trace.steps[2].output={selection:{selected_candidate_id:null},producer:{action:'SUPPRESS'}};
  const held=Object.fromEntries(summarize(trace));
  assert.equal(held.Story,'No update selected'); assert.equal(held.Publication,'Held');
  const quiet=Object.fromEntries(summarize({...run('quiet',1,true),status:'complete'}));
  assert.equal(quiet.Decision,'Not called'); assert.equal(quiet.Publication,'No evaluation needed');
  trace.status='running';
  trace.steps=[{name:'editorial selection: azure-foundry:Decision-1',status:'response',duration_ms:100,output:{answers:{candidate:{choice:'goal'}}}}];
  assert.equal(Object.fromEntries(summarize(trace)).Story,'Awaiting selection');
  trace.steps.push({name:'editorial selection',output:{selected_candidate_id:'goal',primary_frame:'GOAL'}});
  const pending=Object.fromEntries(summarize(trace));
  assert.equal(pending.Story,'GOAL'); assert.equal(pending.Publication,'In progress');
});

test('speech summaries expose synthesis or a cache hit without implying another editorial decision',()=>{
  const {summarize}=inspector();
  const speech={...run('audio',1),label:'Spoken commentary',status:'complete',duration_ms:140,
    steps:[{name:'speech: azure-speech:voice',input:{delivery:'Measured analysis'},duration_ms:140,status:'response'}]};
  const result=Object.fromEntries(summarize(speech));
  assert.equal(result.Speech,'azure-speech:voice');assert.equal(result.Delivery,'Measured analysis');assert.equal(result.Audio,'Ready');
  speech.steps=[{name:'speech cache',output:{cache_hit:true}}];
  assert.equal(Object.fromEntries(summarize(speech)).Speech,'Cached audio');
});
