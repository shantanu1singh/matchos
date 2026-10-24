const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function setup() {
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,URL});
  vm.runInContext(fs.readFileSync('matchos/web/audio.js','utf8')+'\nthis.Player=CommentaryPlayer;',context);
  const stories={primary:{id:'broadcast-1',label:'Broadcaster',delivery:'Composed',fresh:true},
                 'view-0':{id:'fan-1',label:'Home fan',delivery:'Warm',fresh:true}};
  const requests=[],audios=[],revoked=[];
  const player=new context.Player({getStory:key=>stories[key],onState(){},
    fetchAudio:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
    makeAudio:()=>{ const audio={paused:false,load(){},removeAttribute(){},pause(){this.paused=true;},async play(){}};audios.push(audio);return audio;},
    urls:{createObjectURL:()=>`blob:${audios.length}`,revokeObjectURL:url=>revoked.push(url)}});
  const respond=index=>requests[index].resolve({ok:true,blob:async()=>({type:'audio/mpeg'})});
  return {player,stories,requests,audios,revoked,respond};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('switching personas aborts the previous request and cannot play its late response',async()=>{
  const {player,requests,audios,respond}=setup();
  const old=player.play('primary');
  const next=player.play('view-0');
  assert.equal(requests[0].options.signal.aborted,true);
  respond(1); await next;
  assert.equal(player.stage,'playing');assert.equal(player.view,'view-0');assert.equal(audios.length,1);
  respond(0);await old;
  assert.equal(audios.length,1);assert.equal(audios[0].paused,false);
  player.reset();
});

test('automatic commentary speaks only new aired stories in the selected view',async()=>{
  const {player,stories,requests,respond}=setup();
  player.setAuto(true);
  assert.equal(requests.length,1);
  respond(0);await flush();
  player.offer('view-0',true);player.offer('primary',false);player.offer('primary',true);
  assert.equal(requests.length,1); // A held update and duplicate publication stay silent.
  stories.primary={...stories.primary,id:'broadcast-2'};
  player.offer('primary',true);
  assert.equal(requests.length,2);
  respond(1);await flush();player.reset();
});

test('seeking or reset discards pending audio, stops playback and releases its URL',async()=>{
  const {player,requests,audios,revoked,respond}=setup();
  const pending=player.play('primary');player.reset();
  assert.equal(requests[0].options.signal.aborted,true);
  respond(0);await pending;assert.equal(audios.length,0);
  const next=player.play('primary');respond(1);await next;
  player.reset();assert.equal(audios[0].paused,true);assert.equal(revoked.length,1);
  assert.equal(player.stage,'idle');
});

test('automatic audio that expires during synthesis is discarded; manual historical replay remains available',async()=>{
  const {player,stories,respond,audios}=setup();
  const automatic=player.play('primary',true);
  stories.primary.fresh=false;
  respond(0);await automatic;assert.equal(audios.length,0);
  const manual=player.play('primary');respond(1);await manual;
  assert.equal(audios.length,1);player.reset();
});

test('browser autoplay restrictions and server failures produce recovery messages',async()=>{
  const {player,requests,respond}=setup();
  player.makeAudio=()=>({load(){},pause(){},removeAttribute(){},async play(){throw Object.assign(new Error('blocked'),{name:'NotAllowedError'});}});
  const blocked=player.play('primary');respond(0);await blocked;
  assert.match(player.message,/Press Listen/);assert.equal(player.stage,'idle');
  const failed=player.play('primary');
  requests[1].resolve({ok:false,json:async()=>({detail:'Speech resource is unavailable'})});await failed;
  assert.equal(player.message,'Speech resource is unavailable');assert.equal(player.stage,'idle');
});
