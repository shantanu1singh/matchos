import { chromium } from '/Users/shantanu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const envText = await fs.readFile(path.resolve(root, '../../.env.production.local'), 'utf8');
const env = Object.fromEntries(envText.trim().split(/\r?\n/).filter(l=>l.includes('=')).map(l=>[l.slice(0,l.indexOf('=')),l.slice(l.indexOf('=')+1)]));
const browser = await chromium.launch({headless:true,executablePath:'/Users/shantanu/.cache/puppeteer/chrome-headless-shell/mac_arm-152.0.7977.42/chrome-headless-shell-mac-arm64/chrome-headless-shell'});
const context = await browser.newContext({viewport:{width:1920,height:1080}, deviceScaleFactor:1,
  httpCredentials:{username:env.MATCHOS_BASIC_AUTH_USERNAME,password:env.MATCHOS_BASIC_AUTH_PASSWORD},
  recordVideo:{dir:path.join(root,'capture/recordings'),size:{width:1920,height:1080}}});
const page = await context.newPage();
const start = performance.now();
const marks = [];
const messages = [];
page.on('websocket',socket=>socket.on('framereceived',event=>{
  try { const message=JSON.parse(String(event.payload)); if(message.type==='demo_status' && message.phase==='ready') messages.push(message); } catch {}
}));
const shot = async(name, locator) => {
  await (locator || page).screenshot({path:path.join(root, 'capture/screenshots', name+'.png')});
};
await fs.mkdir(path.join(root,'capture/screenshots'),{recursive:true});
await fs.mkdir(path.join(root,'capture/extracted'),{recursive:true});
await page.goto('https://matchos-prod.niceground-69655e6e.eastus2.azurecontainerapps.io',{waitUntil:'networkidle'});
await page.waitForSelector('#judge-demo:not([disabled])');
await page.waitForTimeout(1200);
await shot('scroll-000');
const tokens=await page.evaluate(()=>({colors:Object.fromEntries(['bg','bg-2','ink','muted','red','blue','gold','grass-a','grass-b','card'].map(k=>[k,getComputedStyle(document.documentElement).getPropertyValue('--'+k).trim()])),fonts:[...new Set([...document.querySelectorAll('body,button,h2,.mark')].map(e=>getComputedStyle(e).fontFamily))],sections:document.querySelectorAll('section.panel').length,headings:document.querySelectorAll('h1,h2,h3').length,ctas:document.querySelectorAll('button').length}));
await fs.writeFile(path.join(root,'capture/extracted/tokens.json'),JSON.stringify(tokens,null,2));
await fs.writeFile(path.join(root,'capture/extracted/visible-text.txt'),await page.locator('body').innerText());
for (const [index, label] of ['press','goal','card'].entries()) {
  if(index===0) await page.locator('#judge-demo').click(); else await page.locator('#demo-next').click();
  await page.waitForFunction(()=>document.querySelector('#demo-status').textContent.includes('3/3 viewers finished'),{},{timeout:120000});
  await page.waitForTimeout(800);
  const clipStart=(performance.now()-start)/1000;
  await shot(label);
  await shot(label+'-rail',page.locator('.rail'));
  await shot(label+'-broadcaster',page.locator('#primary-panel'));
  const extra=page.locator('#extra-views .persona-panel');
  for(let i=0;i<await extra.count();i++) await shot(label+(i===0?'-fan':'-analyst'),extra.nth(i));
  if(index===0) {
    await page.waitForTimeout(9000);
    marks.push({name:'press',start:clipStart,end:(performance.now()-start)/1000});
    await page.locator('#demo-evidence').click();
    await page.waitForTimeout(1000);
    await shot('evidence');
    const evidenceStart=(performance.now()-start)/1000;
    await page.waitForTimeout(5000);
    marks.push({name:'evidence',start:evidenceStart,end:(performance.now()-start)/1000});
    await page.locator('#close-evidence').click();
  } else {
    await page.waitForTimeout(6500);
    marks.push({name:label,start:clipStart,end:(performance.now()-start)/1000});
  }
}
await page.locator('[data-tab="telemetry"]').click();
await page.waitForTimeout(1500);
await shot('telemetry');
const runs=page.locator('#telemetry-runs button');
if(await runs.count()) { await runs.first().click(); await page.waitForTimeout(700); }
await shot('telemetry-detail');
const telemetryStart=(performance.now()-start)/1000;
await page.waitForTimeout(7000);
marks.push({name:'telemetry',start:telemetryStart,end:(performance.now()-start)/1000});
await fs.writeFile(path.join(root,'capture/extracted/demo-results.json'),JSON.stringify(messages,null,2));
await fs.writeFile(path.join(root,'capture/extracted/clip-marks.json'),JSON.stringify(marks,null,2));
const video=page.video();
await context.close();
await video.saveAs(path.join(root,'capture/recording.webm'));
await browser.close();
console.log('Captured real Azure app: '+messages.length+' settled checkpoints, '+marks.length+' clip ranges; credentials excluded.');
