// Screenshot harness for the UI rework. Usage: node scripts/ui_screenshots.mjs
import { chromium } from 'playwright';
import { mkdirSync } from 'fs';

const BASE = 'http://localhost:5000';
const OUT = 'runs/ui_shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();

async function shoot(name, { theme = 'dark', width = 1440, height = 1000, full = true, prep = null, tab = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  await ctx.addInitScript(([t, tb]) => {
    localStorage.setItem('theme', t);
    localStorage.setItem('activeTab', tb || 'crawler');
  }, [theme, tab]);
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1600); // let reveals settle
  if (prep) await prep(page);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  await ctx.close();
  console.log('shot:', name);
}

// Simulate the live-run panel for visual judgement (no Ollama needed).
async function showRunPanel(page) {
  await page.evaluate(() => {
    const panel = document.querySelector('#crawlpanel');
    panel.hidden = false;
    document.querySelector('#crawl-title').textContent = 'Discovering…';
    const chip = document.querySelector('#run-chip');
    chip.setAttribute('data-state', 'running');
    document.querySelector('#run-chip-label').textContent = 'crawling';
    // stages: plan done, crawl active, extract active
    const ol = document.querySelector('#run-stages');
    ol.querySelector('[data-stage="plan"]').classList.add('done');
    ol.querySelector('[data-stage="crawl"]').classList.add('active');
    ol.querySelector('[data-stage="extract"]').classList.add('active');
    // counters
    const set = (id, v) => { document.getElementById(id).textContent = v; };
    set('cc-pct', '42%'); set('cc-records', '21'); set('cc-records-l', 'of 50 records');
    set('cc-elapsed', '3:12'); set('cc-eta', '4:25'); set('cc-fetched', '17');
    set('cc-data', '9'); set('cc-calls', '26');
    const bar = document.querySelector('#crawl-bar');
    bar.classList.add('det'); bar.style.width = '42%';
    // sparkline demo data
    const c = document.getElementById('cc-spark'); const ctx = c.getContext('2d');
    const sig = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim();
    ctx.clearRect(0, 0, c.width, c.height); ctx.beginPath();
    const pts = [0, 1, 1, 3, 5, 6, 9, 11, 12, 15, 18, 21];
    pts.forEach((r, i) => {
      const x = (i / (pts.length - 1)) * (c.width - 4) + 2;
      const y = c.height - 3 - (r / 21) * (c.height - 8);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = sig; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
    // plan box
    const plan = document.querySelector('#crawl-plan');
    plan.hidden = false;
    plan.innerHTML = '<dt>method</dt><dd>crawl</dd><dt>start URL</dt><dd>https://njrealty.example</dd>' +
      '<dt>query</dt><dd>Real-estate agents in New Jersey: full name, email, phone, agency, city</dd>' +
      '<dt>columns</dt><dd>Full Name · Email · Phone · Agency · City</dd>' +
      '<dt>maxVisits</dt><dd>∞ (until target)</dd><dt>maxExtracts</dt><dd>until 50 records</dd>' +
      '<dt>model</dt><dd>gemma4:e4b</dd>';
    // a few timeline rows
    const stream = document.querySelector('#crawl-stream');
    stream.innerHTML = [
      ['1.2s', 'extract', 'https://njrealty.example/agents/maria', '+1 record(s) (21 total)'],
      ['0.8s', 'runCompletion', '', ''],
      ['2.1s', 'visit · data', 'https://njrealty.example/agents/james', 'DATA PAGE (~1): individual profile'],
      ['1.4s', 'visit', 'https://njrealty.example/agents', 'HUB -> /agents'],
    ].map(([s, k, u, n]) =>
      `<div class="stream-row"><span class="secs">${s}</span><span class="kind kind-${k.startsWith('extract') ? 'extract' : (k.startsWith('run') ? 'runCompletion' : 'visit')}">${k}</span>` +
      `<span class="u">${u}</span>${n ? `<span class="meta" style="white-space:normal; max-width:26ch">${n}</span>` : ''}</div>`).join('');
  });
  await page.waitForTimeout(300);
}

await shoot('night_full', { theme: 'dark' });
await shoot('day_full', { theme: 'light' });
await shoot('night_running', { theme: 'dark', full: false, prep: showRunPanel });
await shoot('day_running', { theme: 'light', full: false, prep: showRunPanel });
await shoot('night_flow', { theme: 'dark', tab: 'flow', full: false });
await shoot('night_mobile', { theme: 'dark', width: 390, height: 844, full: true });
await shoot('day_mobile', { theme: 'light', width: 390, height: 844, full: true });

await browser.close();
console.log('done');
