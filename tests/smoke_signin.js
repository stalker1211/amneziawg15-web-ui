// Changing the panel's password, in a real browser signed in the ordinary way
// (credentials cached by the browser; no puppeteer auth hook, which would hide the
// problem). Two cases:
//
// 1. In this tab's settings drawer: the page keeps working with no sign-in prompt.
//    The drawer hands the new credential to the browser (ApiClient.rememberCredentials),
//    so a new event stream and a reload go through.
// 2. Elsewhere (another browser; here a plain request): this tab's next stream gets a
//    401, the browser gives it up, and the page reloads into the sign-in -- once, not
//    in a loop.
//
// It CHANGES THE PASSWORD, twice: run it against a throwaway container on a fresh volume.
//
//   docker run -d --name awgpw --cap-add NET_ADMIN --device /dev/net/tun -p 18080:80 amneziawg-web-ui:local
//   NODE_PATH=<dir with puppeteer>/node_modules node tests/smoke_signin.js http://127.0.0.1:18080
const puppeteer = require('puppeteer');

const BASE = (process.argv[2] || 'http://127.0.0.1:18080').replace(/\/$/, '');
const USER = process.argv[3] || 'admin';
const OLD = process.argv[4] || 'changeme';
const NEW = process.argv[5] || 'n3w-secret-9';
const ELSEWHERE = `${NEW}-elsewhere`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const basic = (password) => 'Basic ' + Buffer.from(`${USER}:${password}`).toString('base64');

(async () => {
  const b = await puppeteer.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const p = await b.newPage();
  const log = [];
  let stream401 = 0;
  p.on('response', (r) => {
    const u = new URL(r.url());
    if (u.pathname === '/api/events' && r.status() === 401) stream401 += 1;
    if (!u.pathname.startsWith('/static') && r.status() !== 200) log.push(`!! ${r.request().method()} ${u.pathname} ${r.status()}`);
  });
  p.on('pageerror', (e) => log.push('ERR ' + e.message));
  // The stream never ends, so a page load is never network-idle: networkidle2 allows it.
  await p.goto(`${BASE.replace('://', `://${USER}:${OLD}@`)}/api/servers`);
  await p.goto(BASE + '/', { waitUntil: 'networkidle2' });
  log.push('banner before: ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));

  // 1. In the drawer.
  await p.evaluate(() => amneziaApp.openSettings());
  await sleep(1200);
  const type = async (id, v) => { await p.focus('#' + id); await p.keyboard.type(v); };
  await type('s-current', OLD); await type('s-password', NEW); await type('s-password2', NEW);
  await sleep(1000);
  await p.click('#drawerPrimary');
  await sleep(3000);
  log.push('toast: ' + await p.evaluate(() => document.getElementById('toasts').textContent.trim()));
  log.push('banner after: ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));
  log.push('servers load: ' + await p.evaluate(async () => (await fetch('/api/servers')).status));
  await p.evaluate(() => amneziaApp.openEvents());  // a new stream, as after any drop
  await sleep(3000);
  log.push('new stream: ' + await p.evaluate(() => amneziaApp.events?.readyState === EventSource.OPEN)
    + ' / pill ' + await p.evaluate(() => document.getElementById('status').textContent));
  await p.reload({ waitUntil: 'networkidle2' });
  log.push('reload: ' + await p.title() + ', banner ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));

  // 2. Elsewhere. Chrome holds a stream that meets a 401 for its own sign-in dialog,
  // which headless Chrome cannot show; dismiss it, as someone without the new password
  // would, so the 401 reaches the page.
  const cdp = await p.createCDPSession();
  cdp.on('Fetch.requestPaused', (e) => cdp.send('Fetch.continueRequest', { requestId: e.requestId }).catch(() => {}));
  cdp.on('Fetch.authRequired', (e) => cdp.send('Fetch.continueWithAuth',
    { requestId: e.requestId, authChallengeResponse: { response: 'CancelAuth' } }).catch(() => {}));
  await cdp.send('Fetch.enable', { handleAuthRequests: true });
  const changed = await fetch(`${BASE}/api/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: basic(NEW) },
    body: JSON.stringify({ access: { current_password: NEW, password: ELSEWHERE } }),
  });
  log.push('changed elsewhere: ' + changed.status);
  await p.evaluate(() => amneziaApp.openEvents());  // this tab's next stream
  await sleep(8000);
  const landed = await p.title();
  const after = stream401;
  await sleep(8000);  // and then nothing more
  log.push(`after the change elsewhere: "${landed}", 401s on the stream ${after}, ${stream401 - after} more in the next 8 s`);

  console.log(log.join('\n'));
  await b.close();
  const ok = log.some((l) => /uses the new one/.test(l)) && log.includes('banner after: false') && log.includes('servers load: 200')
    && log.some((l) => /^new stream: true/.test(l)) && log.some((l) => /^reload: AmneziaWG Web UI/.test(l))
    && log.includes('changed elsewhere: 200') && !/AmneziaWG/.test(landed) && after >= 1 && stream401 === after;
  console.log(ok ? '\nOK' : '\nFAILED');
  process.exit(ok ? 0 : 1);
})();
