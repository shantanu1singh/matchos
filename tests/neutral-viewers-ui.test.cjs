const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function controls() {
  const source = fs.readFileSync('matchos/web/app.js','utf8');
  const context = vm.createContext({
    personaNames:{analyst:'Analyst',broadcaster:'Broadcaster',fan:'Home fan'},
    teamLabel:id=>id === 'RED' ? 'Liverpool' : 'Man City',
    state:{pulse:{score_red:1,score_blue:2,red:{possession_last5:.6,press_bypass_rate:.25,final_third_entries_last5:12},
                 blue:{possession_last5:.4,press_bypass_rate:.75,final_third_entries_last5:4}}},
  });
  vm.runInContext(source.slice(source.indexOf('function isNeutralViewer('),source.indexOf('function updatePrimaryLabel(')),context);
  vm.runInContext(source.slice(source.indexOf('function statCapsules('),source.indexOf('function renderPulse(')),context);
  return context;
}

test('switching neutral roles removes allegiance and fan mode defaults to Liverpool',()=>{
  const ui = controls();
  const viewer = {persona:'analyst',team_id:'BLUE',language:'en'};
  ui.normalizeViewer(viewer);
  assert.equal(viewer.team_id,null);
  assert.equal(ui.viewerLabel(viewer),'Analyst · Neutral · English');
  viewer.persona='fan';
  ui.normalizeViewer(viewer);
  assert.equal(viewer.team_id,'RED');
  assert.equal(ui.viewerLabel(viewer),'Home fan · Liverpool · English');
  viewer.persona='broadcaster';
  ui.normalizeViewer(viewer);
  assert.equal(viewer.team_id,null);
  assert.equal(ui.viewerLabel(viewer),'Broadcaster · Neutral · English');
});

test('neutral stat capsules compare both sides while fan stats follow Liverpool',()=>{
  const ui = controls();
  assert.deepEqual(Array.from(ui.statCapsules({persona:'analyst',team_id:null})),[
    'Press beaten LIV 25% · MCI 75%', 'Entries (5′) LIV 12 · MCI 4']);
  assert.deepEqual(Array.from(ui.statCapsules({persona:'broadcaster',team_id:null})),[
    'Score 1–2', 'Possession LIV 60% · MCI 40%']);
  assert.equal(ui.statCapsules({persona:'fan',team_id:'RED'})[0],'Liverpool possession 60%');
});

test('dismissals make the player advantage visible for every default persona',()=>{
  const ui = controls();
  Object.assign(ui.state.pulse,{score_red:2,score_blue:1});
  Object.assign(ui.state.pulse.red,{players_on_pitch:11,red_cards:0});
  Object.assign(ui.state.pulse.blue,{players_on_pitch:9,red_cards:2});
  for (const persona of ['analyst','fan','broadcaster']) {
    assert.ok(Array.from(ui.statCapsules({persona,team_id:persona==='fan'?'RED':null})).includes('Players LIV 11 · MCI 9'));
  }
  assert.equal(ui.statCapsules({persona:'broadcaster',team_id:null})[0],'Score 2–1');
});
