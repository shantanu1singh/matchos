const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function renderer() {
  const state = {frame:{timestamp_ms:1000}, viewerDisplays:{}, expandedProof:new Set()};
  let proof;
  const context = vm.createContext({state, requestAnimationFrame:()=>{},
    renderProof: (...args)=>{proof=args;}, clockOf:ms=>String(ms),
    escapeHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')});
  const source = fs.readFileSync('matchos/web/app.js','utf8');
  vm.runInContext(source.slice(source.indexOf('function storyName('),source.indexOf('function renderWhy(')),context);
  const card = {dataset:{},classList:{toggle(){}},querySelector:()=>null,innerHTML:''};
  return {state,card,render:(update,key='primary')=>context.renderEditorialCard(card,null,{},update,key,false),proof:()=>proof};
}
function update(status='update') {
  return {result:{moment:status==='update'?{id:'story-1',expires_at_ms:5000}:null,
                  overlay:status==='update'?{headline:'Press breakdown',body:'Supported update'}:null},
          explanation:{status,chosen:'PRESS_BREAKDOWN',reason:'Selected tactical evidence',candidateCount:2,
                       evidence:['Five passes'],held:[],sourceEventCount:5,timestamp_ms:1000,reasonSource:'model'}};
}
test('a held follow-up stays below the aired story, including after its live window',()=>{
  const {render,card,state,proof}=renderer();
  render(update());
  const held=update('no_interruption'); held.explanation.reason='Already shown recently';
  render(held);
  assert.match(card.innerHTML,/Press breakdown/);
  assert.match(card.innerHTML,/Stories held/);
  assert.match(card.innerHTML,/Already shown recently/);
  assert.ok(card.innerHTML.indexOf('held-update') > card.innerHTML.indexOf('Supported update'));
  assert.equal(proof()[1].id,'story-1');
  state.frame.timestamp_ms=6000;
  render(held);
  assert.match(card.innerHTML,/Supported update/);
  assert.match(card.innerHTML,/Last aired/);
  assert.match(card.innerHTML,/Stories held/);
  assert.doesNotMatch(card.innerHTML,/Let the match breathe/);
  assert.equal(card.dataset.displayAge,'historical');
  assert.equal(proof()[1].id,'story-1');
});

test('only a new current aired story replaces the retained card and clears the held notice',()=>{
  const {render,card,state,proof}=renderer();
  render(update());
  state.frame.timestamp_ms=6000;
  render(update('no_interruption'));
  const stale=update(); stale.result.moment.id='stale-story';
  stale.result.overlay.body='Late outdated response';
  render(stale);
  assert.match(card.innerHTML,/Supported update/);
  assert.doesNotMatch(card.innerHTML,/Late outdated response/);
  const next=update();
  next.result.moment={id:'story-2',expires_at_ms:10000};
  next.result.overlay={headline:'New story',body:'New finished copy'};
  render(next);
  assert.match(card.innerHTML,/New finished copy/);
  assert.doesNotMatch(card.innerHTML,/Supported update|held-update|Last aired/);
  assert.equal(card.dataset.displayAge,'current');
  assert.equal(proof()[1].id,'story-2');
});

test('before the first aired story the card keeps its waiting state',()=>{
  const {render,card,state}=renderer();
  render(update('no_interruption'));
  assert.match(card.innerHTML,/Watching for your next story/);
  assert.doesNotMatch(card.innerHTML,/Let the match breathe|Last aired|held-update/);
  assert.equal(state.viewerDisplays.primary,undefined);
});

test('reset or changed viewer clears the old story, and no candidates is not labelled suppression',()=>{
  const {render,card,state}=renderer();
  render(update());
  const quiet=update('no_interruption');
  quiet.explanation.candidateCount=0;
  quiet.explanation.reason='No story passed the evidence checks.';
  render(quiet);
  assert.match(card.innerHTML,/No new story/);
  assert.doesNotMatch(card.innerHTML,/Stories held/);
  state.viewerDisplays={};
  render(null);
  assert.doesNotMatch(card.innerHTML,/Supported update|held-update/);
  assert.match(card.innerHTML,/Watching for your next story/);
});
test('one viewer abstaining does not replace another viewer story',()=>{
  const {render,state,card}=renderer();
  const aired=update(); render(aired,'primary');
  const held=update('no_interruption'); held.explanation.reason='<script>unsafe</script>';
  render(held,'view-0');
  assert.equal(state.viewerDisplays.primary,aired);
  assert.equal(state.viewerDisplays['view-0'],undefined);
  assert.match(card.innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(card.innerHTML,/<script>/);
});
