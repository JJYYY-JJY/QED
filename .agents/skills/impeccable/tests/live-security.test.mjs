import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { buildTagBlock } from '../scripts/live-inject.mjs';
import { assembleLiveBrowserScript } from '../scripts/live/browser-script-parts.mjs';
import { buildSvelteLiveRootComponent } from '../scripts/live/sveltekit-adapter.mjs';

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const liveServerPath = path.resolve(testsDir, '..', 'scripts', 'live-server.mjs');

function fixture(t, prefix = 'impeccable-live-security-') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

async function reservePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function waitFor(predicate, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error('Timed out waiting for live server');
}

async function startLiveServer(t, root) {
  const port = await reservePort();
  let stdout = '';
  let stderr = '';
  const child = spawn(process.execPath, [liveServerPath, `--port=${port}`], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.setEncoding('utf-8');
  child.stderr.setEncoding('utf-8');
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });

  const serverInfoPath = path.join(root, '.impeccable', 'live', 'server.json');
  const info = await waitFor(() => {
    if (child.exitCode !== null) {
      throw new Error(`Live server exited early (${child.exitCode}): ${stderr || stdout}`);
    }
    try {
      return JSON.parse(fs.readFileSync(serverInfoPath, 'utf-8'));
    } catch {
      return null;
    }
  });

  t.after(async () => {
    try {
      await fetch(`http://127.0.0.1:${info.port}/stop?token=${encodeURIComponent(info.token)}`);
    } catch {
      child.kill('SIGTERM');
    }
    await Promise.race([
      new Promise((resolve) => child.once('exit', resolve)),
      new Promise((resolve) => setTimeout(resolve, 1_000)),
    ]);
    if (child.exitCode === null) child.kill('SIGKILL');
  });
  return info;
}

test('live bootstrap URL carries strict credentials without window token globals', () => {
  const token = crypto.randomUUID();
  const tag = buildTagBlock('html', 8400, 'index.html', token);
  const svelte = buildSvelteLiveRootComponent(8400, token);
  const script = assembleLiveBrowserScript({
    token,
    port: 8400,
    vocabulary: [],
    parts: [{ name: 'client', file: 'client.js', source: 'void 0;\n' }],
  });

  assert.match(tag, new RegExp(`/live\\.js\\?token=${token}`));
  assert.match(svelte, new RegExp(`/live\\.js\\?token=${token}`));
  assert.equal(script.includes('window.__IMPECCABLE_TOKEN__'), false);
  assert.equal(script.includes('window.__IMPECCABLE_PORT__'), false);
  assert.match(script, new RegExp(`const IMPECCABLE_TOKEN = ${JSON.stringify(token)}`));

  assert.throws(() => buildTagBlock('html', '8400junk', 'index.html', token), /port/i);
  assert.throws(() => buildTagBlock('html', 0, 'index.html', token), /port/i);
  assert.throws(() => buildTagBlock('html', 8400, 'index.html', 'not-a-token'), /token/i);
  assert.throws(() => buildSvelteLiveRootComponent(65536, token), /port/i);
  assert.throws(() => buildSvelteLiveRootComponent(8400, 'not-a-token'), /token/i);
});

test('GET /live.js requires the session token and keeps it out of window globals', async (t) => {
  const root = fixture(t);
  const info = await startLiveServer(t, root);
  const base = `http://127.0.0.1:${info.port}`;

  const anonymous = await fetch(`${base}/live.js`);
  const wrong = await fetch(`${base}/live.js?token=${crypto.randomUUID()}`);
  const authorized = await fetch(`${base}/live.js?token=${encodeURIComponent(info.token)}`);
  const body = await authorized.text();

  assert.equal(anonymous.status, 401);
  assert.equal(wrong.status, 401);
  assert.equal(authorized.status, 200);
  assert.equal(body.includes('window.__IMPECCABLE_TOKEN__'), false);
  assert.equal(body.includes('window.__IMPECCABLE_PORT__'), false);
  assert.doesNotThrow(() => new vm.Script(body));
});

test('GET /source reads regular project files but rejects sibling and symlink paths', async (t) => {
  const root = fixture(t);
  const outside = fixture(t, `${path.basename(root)}-sibling-`);
  const projectFile = path.join(root, 'inside.html');
  const outsideFile = path.join(outside, 'secret.html');
  fs.writeFileSync(projectFile, '<p>inside</p>\n');
  fs.writeFileSync(outsideFile, '<p>outside secret</p>\n');
  fs.symlinkSync(outsideFile, path.join(root, 'linked.html'));

  const info = await startLiveServer(t, root);
  const base = `http://127.0.0.1:${info.port}`;
  const auth = `token=${encodeURIComponent(info.token)}`;
  const inside = await fetch(`${base}/source?${auth}&path=inside.html`);
  const sibling = await fetch(`${base}/source?${auth}&path=${encodeURIComponent(outsideFile)}`);
  const linked = await fetch(`${base}/source?${auth}&path=linked.html`);

  assert.equal(inside.status, 200);
  assert.equal(await inside.text(), '<p>inside</p>\n');
  assert.notEqual(sibling.status, 200);
  assert.notEqual(linked.status, 200);
  assert.equal((await sibling.text()).includes('outside secret'), false);
  assert.equal((await linked.text()).includes('outside secret'), false);
});

test('live server rejects non-canonical port arguments', async (t) => {
  const root = fixture(t);
  const port = await reservePort();
  const child = spawn(process.execPath, [liveServerPath, `--port=${port}junk`], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => {
    if (child.exitCode === null) child.kill('SIGKILL');
  });

  const exitCode = await Promise.race([
    new Promise((resolve) => child.once('exit', resolve)),
    new Promise((resolve) => setTimeout(() => resolve(null), 1_000)),
  ]);
  assert.notEqual(exitCode, null, 'invalid port started a live server');
  assert.notEqual(exitCode, 0);
});
