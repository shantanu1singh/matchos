const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function demoController({ready=true, stops=[], open=true}={}) {
  const source=fs.readFileSync('matchos/web/app.js','utf8');
  const elements=new Map();
  const document={
    getElementById(id) {
      if (!elements.has(id)) elements.set(id,{hidden:true,textContent:''});
      return elements.get(id);
    },
    querySelectorAll:()=>[],
    querySelector:()=>({click(){}}),
  };
  const sent=[];
  class WebSocket {
    static OPEN=1;
    constructor(){this.readyState=open ? 1 : 3;}
    send(payload){sent.push(JSON.parse(payload));}
  }
  const state={ws:new WebSocket(),serverReady:ready,candidates:['existing'],
    demo:{active:false,step:0,phase:'idle',stops,outcomes:{}}};
  let renders=0;
  const context=vm.createContext({state,document,WebSocket,location:{protocol:'http:',host:'localhost'},
    setLive(text){state.live=text;},markSpeed(){},renderScenarios(){},clearEvidence(){},
    renderDemoGuide(){renders++;},renderAudioControls(){},onMessage(){}});
  vm.runInContext(source.slice(source.indexOf('function connect('),source.indexOf('function onMessage(')),context);
  vm.runInContext(source.slice(source.indexOf('function requestDemo('),source.indexOf('function receiveDemo(')),context);
  return {context,state,sent,notice:document.getElementById('demo-notice'),renders:()=>renders};
}

test('an older backend shows restart instructions instead of silently ignoring Judge demo',()=>{
  const {context,state,sent,notice}=demoController();
  context.requestDemo();
  assert.equal(notice.hidden,false);
  assert.match(notice.textContent,/older version/);
  assert.match(notice.textContent,/\.venv\/bin\/python -m matchos/);
  assert.equal(state.demo.active,false);
  assert.deepEqual(state.candidates,['existing']);
  assert.deepEqual(sent,[]);
});

test('Judge demo explains unavailable and unfinished connections without losing the live view',()=>{
  for (const options of [{open:false},{ready:false}]) {
    const {context,state,sent,notice}=demoController(options);
    context.requestDemo();
    assert.equal(notice.hidden,false);
    assert.match(notice.textContent,options.open===false ? /live connection/ : /Still connecting/);
    assert.equal(state.demo.active,false);
    assert.deepEqual(sent,[]);
  }
});

test('a supported backend receives the requested demo checkpoint and clears earlier notices',()=>{
  const {context,state,sent,notice,renders}=demoController({stops:[{id:'press'}]});
  notice.hidden=false; notice.textContent='Earlier warning';
  context.requestDemo();
  assert.equal(state.demo.active,true);
  assert.equal(state.demo.phase,'preparing');
  assert.equal(renders(),1);
  assert.deepEqual(sent,[{type:'demo',step:0,requestId:1}]);
  assert.equal(notice.hidden,true);
});

test('losing the socket during review stops the waiting state and shows a recovery action',()=>{
  const {context,state,notice}=demoController({stops:[{id:'press'}]});
  context.connect();
  state.demo.active=true; state.demo.phase='evaluating';
  state.ws.onclose();
  assert.equal(state.serverReady,false);
  assert.equal(state.demo.phase,'error');
  assert.equal(state.live,'Disconnected');
  assert.match(state.demo.message,/refresh/i);
  assert.equal(notice.hidden,false);
});

function evidenceRenderer() {
  const source=fs.readFileSync('matchos/web/app.js','utf8');
  const paths=[];
  const ctx={save(){},restore(){},beginPath(){},stroke(){},fill(){},closePath(){},arc(){},fillText(){},
             moveTo:(x,y)=>paths.push(['start',x,y]),lineTo:(x,y)=>paths.push(['end',x,y])};
  const state={evidence:null};
  const context=vm.createContext({state,ctx});
  vm.runInContext(source.slice(source.indexOf('function evidenceMetric('),source.indexOf('function statCapsules(')),context);
  return {context,state,paths};
}

test('evidence comparison uses attached facts and does not change canonical data',()=>{
  const {context}=evidenceRenderer();
  const moment={frame:'PRESS_BREAKDOWN',facts:{attackingTeam:'Liverpool',defendingTeam:'Manchester City',pressBypassBefore:.364,pressBypassNow:.879}};
  const original=JSON.stringify(moment);
  const metric=context.evidenceMetric(moment);
  assert.equal(metric.before,36); assert.equal(metric.after,88); assert.equal(metric.unit,'%');
  assert.match(metric.label,/Liverpool through Manchester City's first press/);
  assert.equal(JSON.stringify(moment),original);
  assert.equal(context.evidenceMetric({frame:'RED_CARD',facts:{}}),null);
});

test('event map draws recorded source locations and ignores current player positions and invalid coordinates',()=>{
  const {context,state,paths}=evidenceRenderer();
  state.frame={players:[{x:999,y:999}]};
  state.evidence={selected:null,moment:{evidence_detail:[
    {location:{x:20,y:30},end_location:{x:40,y:30}},
    {location:{x:NaN,y:20},end_location:{x:60,y:30}},
    {location:{x:-3,y:20}},
  ]}};
  context.drawEvidenceMap(x=>x,y=>y);
  assert.deepEqual(paths[0],['start',20,30]);
  assert.deepEqual(paths[1],['end',40,30]);
  assert.equal(paths.filter(path=>path[0]==='start').length,2); // Actual segment and its arrowhead.
  assert.ok(paths.every(path=>path[1]!==999 && Number.isFinite(path[1])));
});

test('demo ignores late results from a previous retry and records held outcomes without inventing a story',()=>{
  const source=fs.readFileSync('matchos/web/app.js','utf8');
  const state={demo:{active:true,step:0,requestId:2,phase:'evaluating',settled:0,outcomes:{}}};
  const context=vm.createContext({state,renderDemoGuide(){}});
  vm.runInContext(source.slice(source.indexOf('function receiveDemo('),source.indexOf('function renderDemoGuide(')),context);
  context.receiveDemo({type:'demo_status',step:0,requestId:1,phase:'ready',results:[]});
  assert.equal(state.demo.phase,'evaluating');
  const held={index:0,explanation:{status:'no_interruption'},result:{moment:null,overlay:null}};
  context.receiveDemo({type:'demo_status',step:0,requestId:2,phase:'ready',results:[held]});
  assert.equal(state.demo.phase,'ready'); assert.equal(state.demo.settled,1);
  assert.equal(state.demo.outcomes[0].result.moment,null);
  state.demo.active=false;
  context.receiveDemo({type:'demo_status',step:0,requestId:2,phase:'preparing'});
  assert.equal(state.demo.phase,'ready');
});
