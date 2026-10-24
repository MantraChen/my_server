// Run against the pinned upstream after its production build. No real credentials/data.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

const upstream = resolve(process.argv[2] || '.devblog-upstream');
const source = JSON.parse(readFileSync(new URL('../source.json', import.meta.url), 'utf8'));
const require = createRequire(join(upstream, 'package.json'));
const Database = require('better-sqlite3');
const bcrypt = require('bcryptjs');
const { migrate } = await import(pathToFileURL(join(upstream, 'scripts/migrate.mjs')));
const directory = mkdtempSync(join(tmpdir(), 'devblog-proxy-'));
const database = join(directory, 'test.db');
const db = new Database(database);
migrate(db);
db.close();
const probe = createServer().listen(0, '127.0.0.1');
await once(probe, 'listening');
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const password = crypto.randomUUID();
const child = spawn(process.execPath, ['dist/server/entry.mjs'], {
  cwd: upstream, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env, HOST: '127.0.0.1', PORT: String(port), DATABASE_PATH: database,
    ADMIN_PASSWORD_HASH: bcrypt.hashSync(password, 4) },
});
let output = '';
child.stdout.on('data', chunk => { output += chunk; });
child.stderr.on('data', chunk => { output += chunk; });
const direct = `http://127.0.0.1:${port}`;
// Caddy keeps Host and rewrites forwarding headers before calling the loopback app.
const proxyHeaders = { Host: new URL(source.site).host, 'X-Forwarded-Host': new URL(source.site).host,
  'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '198.51.100.7' };
try {
  let healthy = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      const response = await fetch(direct + source.base + '/api/health');
      if (response.ok) {
        assert.equal((await response.json()).release, source.commit);
        healthy = true;
        break;
      }
    } catch (error) {
      if (error instanceof assert.AssertionError) throw error;
    }
    if (child.exitCode !== null) throw new Error(output);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(healthy, output);
  const page = await fetch(direct + source.base + '/', { headers: proxyHeaders });
  assert.equal(page.status, 200);
  assert.ok((await page.text()).includes(`rel="canonical" href="${source.site}${source.base}/"`));
  const login = (origin) => fetch(direct + source.base + '/api/auth/login', {
    method: 'POST', redirect: 'manual', headers: { ...proxyHeaders, Origin: origin, 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  assert.equal((await login('https://evil.example')).status, 403);
  const signedIn = await login(source.site);
  assert.equal(signedIn.status, 200, await signedIn.clone().text());
  assert.match(signedIn.headers.get('set-cookie'), /HttpOnly; Secure; SameSite=Strict/);
  assert.ok(signedIn.headers.get('set-cookie').startsWith('howielog_admin_session='));
  assert.ok(signedIn.headers.get('set-cookie').includes(`Path=${source.base}/;`));
  const cookie = signedIn.headers.get('set-cookie').split(';')[0];
  const admin = await fetch(direct + source.base + '/api/posts', { headers: { ...proxyHeaders, Cookie: cookie } });
  assert.equal(admin.status, 200);
  assert.match(admin.headers.get('strict-transport-security'), /max-age=/);
  assert.match(admin.headers.get('cache-control'), /no-store/);
  console.log('PASS: pinned release health and Caddy-style HTTPS origin, secure cookie, admin and CSRF checks');
} finally {
  if (child.exitCode === null) {
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  }
  rmSync(directory, { recursive: true, force: true });
}
