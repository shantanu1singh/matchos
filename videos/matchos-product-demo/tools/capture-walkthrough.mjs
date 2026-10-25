import { chromium } from '/Users/shantanu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const text = await fs.readFile(path.resolve(root, '../../.env.production.local'), 'utf8');
const env = Object.fromEntries(text.split(/\r?\n/).filter(l=>l.includes('=')).map(l=>[l.slice(0,l.indexOf('=')),l.slice(l.indexOf('=')+1).replace(/^['"]|['"]$/g,'')]));
const browser = await chromium.launch({headless:true,executablePath:'/Users/shantanu/.cache/puppeteer/chrome-headless-shell/mac_arm-152.0.7977.42/chrome-headless-shell-mac-arm64/chrome-headless-shell'});
const context = await browser.newContext({viewport:{width:1920,height:1080},deviceScaleFactor:1,
  httpCredentials:{username:env.MATCHOS_BASIC_AUTH_USERNAME,password:env.MATCHOS_BASIC_AUTH_PASSWORD},
  recordVideo:{dir:path.join(root,'capture/recordings'),size:{width:1920,height:1080}}});
const page=await context.newPage();
const born=performance.now(), messages=[], marks=[];
let spokenAudio=null;
page.on('response',async response=>{
  if(response.url().includes('/api/speech/')&&response.status()===200){
    spokenAudio=await response.body();
  }
});
page.on('websocket',s=>s.on('framereceived',e=>{try{const m=JSON.parse(String(e.payload));if(m.type==='demo_status'&&m.phase==='ready')messages.push(m);}catch{}}));
await page.goto('https://matchos-prod.niceground-69655e6e.eastus2.azurecontainerapps.io',{waitUntil:'networkidle'});
await page.waitForFunction(()=>document.querySelector('#clock')?.textContent!=='00:00');
await page.waitForSelector('#judge-demo:not([disabled])');
// A recording cursor follows genuine pointer events; it changes no app content.
await page.evaluate(()=>{
 const p=document.createElement('div');p.id='recording-pointer';
 p.style.cssText='position:fixed;left:0;top:0;width:25px;height:30px;z-index:2147483647;pointer-events:none;transform:translate(900px,950px)';
 p.innerHTML='<svg viewBox="0 0 25 30" width="25" height="30"><path d="M2 2L3 24L9 18L14 28L19 25L14 16L23 16Z" fill="white" stroke="#07110d" stroke-width="2" stroke-linejoin="round"/></svg>';
 document.body.append(p);addEventListener('mousemove',e=>p.style.transform=`translate(${e.clientX}px,${e.clientY}px)`);
});
await page.mouse.move(900,950);
const cutStart=(performance.now()-born)/1000;
const elapsed=()=> (performance.now()-born)/1000-cutStart;
const until=async(t)=>{const wait=t-elapsed();if(wait>0)await page.waitForTimeout(wait*1000);};
const move=async(locator)=>{
 await locator.scrollIntoViewIfNeeded();const box=await locator.boundingBox();
 if(!box)throw new Error('Missing visible capture target');
 await page.mouse.move(box.x+box.width*.5,box.y+box.height*.5,{steps:24});
 await page.waitForTimeout(350);
};
const click=async(locator)=>{await move(locator);await locator.click();};
const shot=async(name)=>{await page.screenshot({path:path.join(root,'capture/screenshots',name+'.png')});marks.push({name,time:elapsed()});};
await fs.mkdir(path.join(root,'capture/screenshots'),{recursive:true});
await fs.mkdir(path.join(root,'capture/extracted'),{recursive:true});
await shot('before-launch');
// Guided replay locks settings, so show real language controls before launch.
await until(.5);await click(page.locator('[popovertarget="view-settings-0"]').first());
const languageSelect=page.locator('#view-settings-0 [data-field="language"]');
if(await languageSelect.inputValue()!=='es')throw new Error('Expected the real Spanish fan preset');
const languages=await languageSelect.locator('option').allTextContents();
await click(languageSelect);await page.waitForTimeout(1000);await shot('four-languages');
await page.keyboard.press('Escape');
if(await page.locator('#view-settings-0').isVisible())await click(page.locator('[aria-label="Close view 2 settings"]'));
await until(4);await click(page.locator('#judge-demo'));marks.push({name:'launch',time:elapsed()});
await page.waitForFunction(()=>document.querySelector('#demo-status').textContent.includes('3/3 viewers finished'),{},{timeout:120000});
marks.push({name:'all-personas-ready',time:elapsed()});
await shot('personas-ready');
await until(15);await move(page.locator('#primary-panel .card h2'));
await until(20);await move(page.locator('#extra-views .persona-panel').nth(0).locator('h2'));
await until(27);await click(page.locator('[data-listen-view="view-0"]'));
await page.waitForFunction(()=>document.querySelector('[data-listen-view="view-0"]').dataset.state==='Stop',{},{timeout:15000});
const speechStart=elapsed();
marks.push({name:'spanish-speech-start',time:speechStart});
await shot('spanish-listen');
await until(33.5);await click(page.locator('[data-listen-view="view-0"]'));
const speechEnd=elapsed();marks.push({name:'spanish-speech-stop',time:speechEnd});
await until(35);await move(page.locator('#extra-views .persona-panel').nth(1).locator('h2'));
await shot('compare-personas');
await until(39);await click(page.locator('#primary-panel .editorial-explanation > summary'));
await shot('decision-open');
await until(43);await click(page.locator('#primary-panel .editorial-explanation > summary'));
await until(44);await click(page.locator('#primary-panel details.proof > summary'));
await shot('facts-open');
await until(47);await click(page.locator('#primary-panel details.proof > summary'));
await until(48);await click(page.locator('#demo-evidence'));
await shot('pitch-evidence');
await page.mouse.move(850,680,{steps:24});
await until(53);
const cards=await page.locator('.persona-panel').evaluateAll(nodes=>nodes.map(n=>({viewer:n.querySelector('.view-label')?.textContent,title:n.querySelector('h2')?.textContent,body:n.querySelector('.body')?.textContent})));
const video=page.video();
if(!spokenAudio)throw new Error('No real Spanish Speech response was captured');
await fs.mkdir(path.join(root,'.tts-cache'),{recursive:true});
await fs.writeFile(path.join(root,'.tts-cache/spanish-broadcast.mp3'),spokenAudio);
await fs.writeFile(path.join(root,'capture/extracted/walkthrough.json'),JSON.stringify({source:'Authenticated deployed MatchOS Judge demo',cutStart,duration:elapsed(),speed:1,marks,cards,languages,speechStart,speechEnd},null,2));
await fs.writeFile(path.join(root,'capture/extracted/demo-results.json'),JSON.stringify(messages,null,2));
await context.close();await video.saveAs(path.join(root,'capture/recording.webm'));await browser.close();
console.log(JSON.stringify({captured:true,readyAt:marks.find(m=>m.name==='all-personas-ready')?.time,duration:marks.at(-1)?.time,cards:cards.map(x=>({viewer:x.viewer,title:x.title}))}));
