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
test('a held follow-up keeps the valid story and explains the latest decision',()=>{
  const {render,card,state,proof}=renderer();
  render(update());
  const held=update('no_interruption'); held.explanation.reason='Already shown recently';
  render(held);
  assert.match(card.innerHTML,/Press breakdown/);
  assert.match(card.innerHTML,/On screen · next update held/);
  assert.match(card.innerHTML,/Already shown recently/);
  assert.equal(proof()[1].id,'story-1');
  state.frame.timestamp_ms=6000;
  render(held);
  assert.doesNotMatch(card.innerHTML,/Supported update/);
  assert.match(card.innerHTML,/No interruption/);
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
