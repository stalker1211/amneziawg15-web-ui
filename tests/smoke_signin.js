// Changing the panel's password in the settings drawer, in a real browser: signed in
// the ordinary way (credentials cached by the browser, no puppeteer auth hook, which
// would hide the problem), then the drawer's password change. Checks that the page
// keeps working without a sign-in prompt: the drawer hands the new credential to the
// browser (ApiClient.rememberCredentials), the socket reconnects after the session
// key rotation, and a reload goes through.
//
// It CHANGES THE PASSWORD: run it against a throwaway container on a fresh volume.
//
//   docker run -d --name awgpw --cap-add NET_ADMIN --device /dev/net/tun -p 18080:80 amneziawg-web-ui:local
//   NODE_PATH=<dir with puppeteer>/node_modules node tests/smoke_signin.js http://127.0.0.1:18080
const puppeteer = require('puppeteer');

const BASE = (process.argv[2] || 'http://127.0.0.1:18080').replace(/\/$/, '');
const USER = process.argv[3] || 'admin';
const OLD = process.argv[4] || 'changeme';
const NEW = process.argv[5] || 'n3w-secret-9';

(async () => {
  const b = await puppeteer.launch({ headless: 'new' });
  const p = await b.newPage();
  const log = [];
  p.on('response', (r) => { const u = new URL(r.url()); if (!u.pathname.startsWith('/static') && r.status() !== 200) log.push(`!! ${r.request().method()} ${u.pathname} ${r.status()}`); });
  p.on('pageerror', (e) => log.push('ERR ' + e.message));
  await p.goto(`${BASE.replace('://', `://${USER}:${OLD}@`)}/api/servers`);
  await p.goto(BASE + '/', { waitUntil: 'networkidle0' });
  log.push('banner before: ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));
  await p.evaluate(() => amneziaApp.openSettings());
  await new Promise((r) => setTimeout(r, 1200));
  const type = async (id, v) => { await p.focus('#' + id); await p.keyboard.type(v); };
  await type('s-current', OLD); await type('s-password', NEW); await type('s-password2', NEW);
  await new Promise((r) => setTimeout(r, 1000));
  await p.click('#drawerPrimary');
  await new Promise((r) => setTimeout(r, 3000));
  log.push('toast: ' + await p.evaluate(() => document.getElementById('toasts').textContent.trim()));
  log.push('banner after: ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));
  log.push('servers load: ' + await p.evaluate(async () => (await fetch('/api/servers')).status));
  await p.evaluate(() => { amneziaApp.socket.io.engine.close(); });  // force a reconnect: the old session cookie is void now
  await new Promise((r) => setTimeout(r, 4000));
  log.push('socket after reconnect: ' + await p.evaluate(() => amneziaApp.socket.connected) + ' / pill ' + await p.evaluate(() => document.getElementById('status').textContent));
  await p.reload({ waitUntil: 'networkidle0' });
  log.push('reload: ' + await p.title() + ', banner ' + !(await p.evaluate(() => document.getElementById('passwordBanner').hidden)));
  console.log(log.join('\n'));
  await b.close();
  const ok = log.some((l) => /uses the new one/.test(l)) && log.includes('banner after: false') && log.includes('servers load: 200')
    && log.some((l) => /socket after reconnect: true/.test(l)) && log.some((l) => /^reload: AmneziaWG Web UI/.test(l));
  console.log(ok ? '\nOK' : '\nFAILED');
  process.exit(ok ? 0 : 1);
})();
