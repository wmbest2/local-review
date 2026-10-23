// Regenerates the README screenshots: builds the example repo (docs/example/make-example.sh), serves
// it, seeds a submitted review with agent replies and a chat, and captures the UI with headless Chrome over the
// DevTools protocol. Node 22+ (built-in WebSocket and fetch), no packages.
//
//   node docs/screenshots.mjs [path to Chrome]
//
// It also attaches a second worktree to the same server (for the worktree switcher) and runs a watcher
// (so the chat panel says an agent is watching).
import {spawn, execFileSync} from 'node:child_process';
import {mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const tool = join(here, '..', 'local_review.py');
const chromePath = process.argv[2] || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 7899, CDP_PORT = 9339, WIDTH = 1440, HEIGHT = 900;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const work = mkdtempSync(join(tmpdir(), 'local-review-shots-'));
const repo = join(work, 'tipcalc');
// Keep the attached-worktree registry out of the real ~/.local-review (children inherit this).
process.env.LOCAL_REVIEW_HOME = join(work, 'home');
execFileSync(join(here, 'example', 'make-example.sh'), [repo], {stdio: 'inherit'});

const procs = [];
const cleanup = () => { procs.forEach(p => p.kill()); rmSync(work, {recursive: true, force: true}); };
process.on('exit', cleanup);

procs.push(spawn('python3', [tool, 'serve', '--base', 'main', '--port', String(PORT), '--no-open'], {cwd: repo, stdio: 'ignore'}));
const base = `http://127.0.0.1:${PORT}`;
await waitFor(() => fetch(`${base}/api/worktrees`).then(r => r.ok));

// A second worktree on the same server: `serve` in it finds the running server and attaches.
const second = join(work, 'tipcalc-checkout');
execFileSync('git', ['worktree', 'add', '-q', '-b', 'feature/checkout-tips', second, 'main'], {cwd: repo});
execFileSync('python3', [tool, 'serve', '--base', 'main', '--port', String(PORT), '--no-open'], {cwd: second, stdio: 'ignore'});

// A submitted review: an answered question, an addressed range comment and one still open.
const post = (path, body) => fetch(base + path, {
  method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-Review': '1'}, body: JSON.stringify(body),
}).then(r => r.json());
const ask = (file, line, body, start_line) => post('/api/comments', {file, side: 'new', line, body, ...(start_line ? {start_line} : {})});
const calc = 'src/main/kotlin/tips/TipCalculator.kt';
const capped = await ask(calc, 10, 'Is 100% really the cap? People tip more than that on a small bill.');
const leftover = await ask(calc, 22, 'Worth a word on why leftover cents go to the *first* shares rather than spreading them?', 19);
await ask('src/main/kotlin/tips/Split.kt', 8, 'Nit: `shares` could take `percent` too, so callers aren\'t stuck with the default.');
await post('/api/submit', {});
const reply = (c, msg, ...flags) => execFileSync('python3', [tool, 'reply', c.id, '-m', msg, ...flags], {cwd: repo});
reply(capped, 'The cap stops a typo like `180` tipping 180%. Happy to raise it to 200 if you\'d rather.');
reply(leftover, 'It keeps every share within a cent of the others and the order stable, so person 1 always rounds up. Added that to the KDoc.', '--addressed');

// A chat: a question about a range of lines, answered with follow-ups the reviewer can click.
await post('/api/chat', {body: 'Why work in cents here instead of `BigDecimal`?', ref: {file: calc, side: 'new', start_line: 19, line: 22}});
execFileSync('python3', [tool, 'chat', '-m', [
  'Cents keep it exact without `BigDecimal`\'s ceremony: every amount is a whole `Long`, so nothing rounds until the split.',
  '',
  '| | Cents (`Long`) | `BigDecimal` |',
  '|---|---|---|',
  '| Exact | yes | yes |',
  '| Rounding mode to pick | no | yes |',
  '| Allocation | none | per value |',
  '',
  'The one catch is overflow, past about 92 quadrillion dollars.',
].join('\n'), '--suggest', 'Add a KDoc note on why amounts are in cents', '--suggest', 'Leave it as is'], {cwd: repo});

// An agent listening: the chat panel shows "Agent watching" while a watcher's heartbeat is fresh.
procs.push(spawn('python3', [tool, 'watch'], {cwd: repo, stdio: 'ignore'}));
await sleep(2500);

procs.push(spawn(chromePath, ['--headless=new', `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${join(work, 'chrome')}`,
  '--hide-scrollbars', '--no-first-run', 'about:blank'], {stdio: 'ignore'}));
const target = await waitFor(async () => (await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()).find(t => t.type === 'page'));
const cdp = await connect(target.webSocketDebuggerUrl);
await cdp('Emulation.setDeviceMetricsOverride', {width: WIDTH, height: HEIGHT, deviceScaleFactor: 2, mobile: false});
await cdp('Emulation.setEmulatedMedia', {features: [{name: 'prefers-color-scheme', value: 'dark'}]});

await shoot('screenshot.png', {sideBySide: false, scrollTo: calc});
await shoot('side-by-side.png', {sideBySide: true, scrollTo: calc});
// The header's per-kind totals popover, cropped to the top right.
await shoot('totals.png', {sideBySide: false, focus: '#totals', clip: {x: WIDTH - 720, y: 0, width: 720, height: 170}});
await shoot('chat.png', {sideBySide: false, scrollTo: calc, chat: true});
// The worktree switcher at the top of the file tree, shown as an open list (a closed <select> can't be captured
// open), cropped to the top left of the unscrolled page.
await shoot('worktrees.png', {sideBySide: false, prepare: `document.querySelector('#worktree').size = 2`,
  clip: {x: 0, y: 72, width: 300, height: 190}});
console.log('Screenshots written to', here);
process.exit(0);

async function shoot(name, {sideBySide, scrollTo, focus, clip, chat, prepare}) {
  await cdp('Page.navigate', {url: base});
  await waitFor(() => evaluate(`location.origin === ${JSON.stringify(base)} && document.readyState === 'complete'`));
  await evaluate(`localStorage.clear(); ${sideBySide ? "localStorage.setItem('lr:side-by-side', '1');" : ''} true`);
  if (chat) await evaluate(`fetch('/api/meta').then(r => r.json()).then(m => { localStorage.setItem('lr:' + m.root + ':chat', '1'); return true; })`);
  await cdp('Page.navigate', {url: base});
  await waitFor(() => evaluate(`!document.querySelector('#content').hidden && document.querySelectorAll('.thread').length >= 3`));
  if (scrollTo) await evaluate(`(() => {
    const file = [...document.querySelectorAll('#diff .file')].find(f => f.querySelector('.fhead')?.textContent.includes(${JSON.stringify(scrollTo.split('/').pop())}));
    window.scrollTo(0, file.getBoundingClientRect().top + scrollY - 60);
    return true;
  })()`);
  if (prepare) await evaluate(`${prepare}; true`);
  if (focus) await evaluate(`document.querySelector(${JSON.stringify(focus)}).focus(); true`);
  await sleep(300);
  const {data} = await cdp('Page.captureScreenshot', {format: 'png', ...(clip ? {clip: {...clip, scale: 1}} : {})});
  writeFileSync(join(here, name), Buffer.from(data, 'base64'));
}

async function evaluate(expression) {
  const {result} = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  return result.value;
}

async function waitFor(check, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    try { const v = await check(); if (v) return v; } catch {}
    if (Date.now() > end) throw new Error('timed out waiting for ' + check);
    await sleep(200);
  }
}

function connect(url) {
  const ws = new WebSocket(url);
  let id = 0;
  const pending = new Map();
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  };
  return new Promise(resolve => ws.onopen = () => resolve((method, params = {}) => new Promise((ok, fail) => {
    pending.set(++id, m => m.error ? fail(new Error(`${method}: ${m.error.message}`)) : ok(m.result));
    ws.send(JSON.stringify({id, method, params}));
  })));
}
