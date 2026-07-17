import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
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
import { fetchServerStatus as fetchAgentServerStatus } from '../scripts/live-poll.mjs';
import { buildSvelteLiveRootComponent } from '../scripts/live/sveltekit-adapter.mjs';

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const liveServerPath = path.resolve(testsDir, '..', 'scripts', 'live-server.mjs');
const liveStatusPath = path.resolve(testsDir, '..', 'scripts', 'live-status.mjs');
const liveReferencePath = path.resolve(testsDir, '..', 'reference', 'live.md');
const liveBrowserPath = path.resolve(testsDir, '..', 'scripts', 'live-browser.js');

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

async function authorizeBrowser(info, previewOrigin = 'http://127.0.0.1:4173') {
  const base = `http://127.0.0.1:${info.port}`;
  const armed = await fetch(`${base}/authorize-browser`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${info.token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ origin: previewOrigin }),
  });
  assert.equal(armed.status, 200);
  const bootstrap = await fetch(`${base}/live.js`, {
    headers: {
      referer: `${previewOrigin}/`,
      'sec-fetch-dest': 'script',
    },
  });
  assert.equal(bootstrap.status, 200);
  const capability = (await bootstrap.text()).match(/const IMPECCABLE_BROWSER_CAPABILITY = "([^"]+)"/)?.[1];
  assert.ok(capability);
  return {
    base,
    headers: {
      authorization: `Bearer ${capability}`,
      origin: previewOrigin,
    },
  };
}

test('live bootstrap URL is tokenless and browser credentials stay in lexical scope', () => {
  const browserCapability = crypto.randomUUID();
  const tag = buildTagBlock('html', 8400, 'index.html');
  const svelte = buildSvelteLiveRootComponent(8400);
  const script = assembleLiveBrowserScript({
    browserCapability,
    port: 8400,
    vocabulary: [],
    parts: [{ name: 'client', file: 'client.js', source: 'void 0;\n' }],
  });

  assert.match(tag, /src="http:\/\/localhost:8400\/live\.js"/);
  assert.match(svelte, /http:\/\/localhost:8400\/live\.js/);
  assert.equal(tag.includes('?'), false);
  assert.equal(svelte.includes('/live.js?'), false);
  assert.equal(tag.includes(browserCapability), false);
  assert.equal(svelte.includes(browserCapability), false);
  assert.equal(script.includes('window.__IMPECCABLE_BROWSER_CAPABILITY__'), false);
  assert.equal(script.includes('window.__IMPECCABLE_PORT__'), false);
  assert.match(script, new RegExp(`const IMPECCABLE_BROWSER_CAPABILITY = ${JSON.stringify(browserCapability)}`));

  assert.throws(() => buildTagBlock('html', '8400junk', 'index.html'), /port/i);
  assert.throws(() => buildTagBlock('html', 0, 'index.html'), /port/i);
  assert.throws(() => buildSvelteLiveRootComponent(65536), /port/i);
});

test('browser authorization issues a lexical capability and enforces capability plus exact origin', async (t) => {
  const root = fixture(t);
  const info = await startLiveServer(t, root);
  const base = `http://127.0.0.1:${info.port}`;
  const previewOrigin = 'http://127.0.0.1:4173';

  const anonymous = await fetch(`${base}/live.js`);
  const wrongAgent = await fetch(`${base}/authorize-browser`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${crypto.randomUUID()}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ origin: previewOrigin }),
  });
  const armed = await fetch(`${base}/authorize-browser`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${info.token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ origin: previewOrigin }),
  });
  const armedBody = await armed.text();
  const wrongOriginBootstrap = await fetch(`${base}/live.js`, {
    headers: {
      referer: 'http://127.0.0.1:4174/',
      'sec-fetch-dest': 'script',
    },
  });
  const authorized = await fetch(`${base}/live.js`, {
    headers: {
      referer: `${previewOrigin}/settings`,
      'sec-fetch-dest': 'script',
    },
  });
  const scriptBody = await authorized.text();
  const capability = scriptBody.match(/const IMPECCABLE_BROWSER_CAPABILITY = "([^"]+)"/)?.[1];

  assert.equal(anonymous.status, 401);
  assert.equal(wrongAgent.status, 401);
  assert.equal(armed.status, 200);
  assert.equal(armedBody.includes(info.token), false);
  assert.equal(wrongOriginBootstrap.status, 403);
  assert.equal(authorized.status, 200);
  assert.ok(capability);
  assert.notEqual(capability, info.token);
  assert.equal(scriptBody.includes(info.token), false);
  assert.equal(scriptBody.split(capability).length - 1, 1);
  assert.doesNotMatch(scriptBody, /[?&]token=|msg\.token|new EventSource/);
  assert.equal(scriptBody.includes('window.__IMPECCABLE_BROWSER_CAPABILITY__'), false);
  assert.equal(scriptBody.includes('window.__IMPECCABLE_PORT__'), false);
  assert.doesNotThrow(() => new vm.Script(scriptBody));

  const sameOriginWithoutCapability = await fetch(`${base}/status`, {
    headers: { origin: previewOrigin },
  });
  const wrongOriginWithCapability = await fetch(`${base}/status`, {
    headers: {
      authorization: `Bearer ${capability}`,
      origin: 'http://127.0.0.1:4174',
    },
  });
  const browserAuthorized = await fetch(`${base}/status`, {
    headers: {
      authorization: `Bearer ${capability}`,
      origin: previewOrigin,
    },
  });

  assert.equal(sameOriginWithoutCapability.status, 401);
  assert.equal(wrongOriginWithCapability.status, 403);
  assert.equal(browserAuthorized.status, 200);
  assert.equal(browserAuthorized.headers.get('access-control-allow-origin'), previewOrigin);

  const replacementOrigin = 'http://127.0.0.1:4174';
  const replaced = await fetch(`${base}/authorize-browser`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${info.token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({ origin: replacementOrigin }),
  });
  assert.equal(replaced.status, 200);
  const revokedCapability = await fetch(`${base}/status`, {
    headers: {
      authorization: `Bearer ${capability}`,
      origin: previewOrigin,
    },
  });
  const revokedBootstrap = await fetch(`${base}/live.js`, {
    headers: {
      referer: `${previewOrigin}/`,
      'sec-fetch-dest': 'script',
    },
  });
  const replacementBootstrap = await fetch(`${base}/live.js`, {
    headers: {
      origin: replacementOrigin,
      'sec-fetch-dest': 'script',
    },
  });

  assert.equal(revokedCapability.status, 401);
  assert.equal(revokedBootstrap.status, 403);
  assert.equal(replacementBootstrap.status, 200);
});

test('agent status clients use bearer authentication while browser status keeps exact-origin capability auth', async (t) => {
  const root = fixture(t);
  const info = await startLiveServer(t, root);
  const base = `http://127.0.0.1:${info.port}`;

  const legacyQuery = await fetch(`${base}/status?token=${encodeURIComponent(info.token)}`);
  const agentHeader = await fetch(`${base}/status`, {
    headers: { authorization: `Bearer ${info.token}` },
  });
  const pollStatus = await fetchAgentServerStatus(base, info.token);
  const cliStatus = JSON.parse(execFileSync(
    process.execPath,
    [liveStatusPath],
    { cwd: root, encoding: 'utf-8' },
  ));

  assert.equal(legacyQuery.status, 401);
  assert.equal(agentHeader.status, 200);
  assert.equal((await agentHeader.json()).status, 'ok');
  assert.equal(pollStatus.status, 'ok');
  assert.equal(cliStatus.liveServer?.status, 'ok');
});

test('GET /source only reads agent-issued active-session files', async (t) => {
  const root = fixture(t);
  const outside = fixture(t, `${path.basename(root)}-sibling-`);
  const projectFile = path.join(root, 'inside.html');
  const outsideFile = path.join(outside, 'secret.html');
  fs.writeFileSync(projectFile, '<p>inside</p>\n');
  fs.writeFileSync(outsideFile, '<p>outside secret</p>\n');
  fs.symlinkSync(outsideFile, path.join(root, 'linked.html'));

  const info = await startLiveServer(t, root);
  const browser = await authorizeBrowser(info);
  const unissued = await fetch(`${browser.base}/source?path=inside.html`, { headers: browser.headers });
  assert.equal(unissued.status, 403);

  const generated = await fetch(`${browser.base}/events`, {
    method: 'POST',
    headers: {
      ...browser.headers,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      type: 'generate',
      id: 'deadbeef',
      action: 'polish',
      count: 3,
      pageUrl: '/',
      element: { outerHTML: '<p>inside</p>' },
    }),
  });
  assert.equal(generated.status, 200);
  const replied = await fetch(`${browser.base}/poll`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      token: info.token,
      id: 'deadbeef',
      type: 'done',
      file: 'inside.html',
    }),
  });
  assert.equal(replied.status, 200);

  const inside = await fetch(`${browser.base}/source?path=inside.html`, { headers: browser.headers });
  const sibling = await fetch(
    `${browser.base}/source?path=${encodeURIComponent(outsideFile)}`,
    { headers: browser.headers },
  );
  const linked = await fetch(`${browser.base}/source?path=linked.html`, { headers: browser.headers });

  assert.equal(inside.status, 200);
  assert.equal(await inside.text(), '<p>inside</p>\n');
  assert.notEqual(sibling.status, 200);
  assert.notEqual(linked.status, 200);
  assert.equal((await sibling.text()).includes('outside secret'), false);
  assert.equal((await linked.text()).includes('outside secret'), false);

  const accepted = await fetch(`${browser.base}/events`, {
    method: 'POST',
    headers: { ...browser.headers, 'content-type': 'application/json' },
    body: JSON.stringify({
      type: 'accept',
      id: 'deadbeef',
      variantId: '1',
    }),
  });
  assert.equal(accepted.status, 200);
  const inactive = await fetch(`${browser.base}/source?path=inside.html`, { headers: browser.headers });
  assert.equal(inactive.status, 403);
});

test('terminal agent replies revoke browser source access', async (t) => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root, 'completed.html'), '<p>completed</p>\n');
  fs.writeFileSync(path.join(root, 'discarded.html'), '<p>discarded</p>\n');
  fs.writeFileSync(path.join(root, 'errored.html'), '<p>errored</p>\n');
  const info = await startLiveServer(t, root);
  const browser = await authorizeBrowser(info);

  for (const terminal of [
    { id: 'deadbeef', type: 'complete', file: 'completed.html' },
    { id: 'cafebabe', type: 'discard', file: 'discarded.html' },
    { id: 'baadf00d', type: 'error', file: 'errored.html' },
  ]) {
    const generated = await fetch(`${browser.base}/events`, {
      method: 'POST',
      headers: { ...browser.headers, 'content-type': 'application/json' },
      body: JSON.stringify({
        type: 'generate',
        id: terminal.id,
        action: 'polish',
        count: 1,
        pageUrl: '/',
        element: { outerHTML: '<p>inside</p>' },
      }),
    });
    assert.equal(generated.status, 200);

    const replied = await fetch(`${browser.base}/poll`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        token: info.token,
        ...terminal,
      }),
    });
    assert.equal(replied.status, 200);

    const source = await fetch(
      `${browser.base}/source?path=${encodeURIComponent(terminal.file)}`,
      { headers: browser.headers },
    );
    assert.equal(source.status, 403, `${terminal.type} left ${terminal.file} readable`);
  }
});

test('generate events only accept screenshot paths issued for that event by the live server', async (t) => {
  const root = fixture(t);
  const outside = fixture(t, 'impeccable-live-screenshot-outside-');
  const outsideFile = path.join(outside, 'secret.png');
  fs.writeFileSync(outsideFile, 'outside secret');
  const info = await startLiveServer(t, root);
  const browser = await authorizeBrowser(info);
  const event = {
    type: 'generate',
    id: 'deadbeef',
    action: 'polish',
    count: 3,
    pageUrl: '/',
    element: { outerHTML: '<button>Save</button>' },
  };

  const arbitrary = await fetch(`${browser.base}/events`, {
    method: 'POST',
    headers: { ...browser.headers, 'content-type': 'application/json' },
    body: JSON.stringify({ ...event, screenshotPath: outsideFile }),
  });
  assert.equal(arbitrary.status, 400);

  const upload = await fetch(`${browser.base}/annotation?eventId=${event.id}`, {
    method: 'POST',
    headers: { ...browser.headers, 'content-type': 'image/png' },
    body: Buffer.from('89504e470d0a1a0a', 'hex'),
  });
  assert.equal(upload.status, 200);
  const { path: issuedPath } = await upload.json();
  assert.match(issuedPath, /\.impeccable\/live\/annotations\/session-[^/]+\/deadbeef\.png$/);

  const accepted = await fetch(`${browser.base}/events`, {
    method: 'POST',
    headers: { ...browser.headers, 'content-type': 'application/json' },
    body: JSON.stringify({ ...event, screenshotPath: issuedPath }),
  });
  assert.equal(accepted.status, 200);

  const replayedForAnotherEvent = await fetch(`${browser.base}/events`, {
    method: 'POST',
    headers: { ...browser.headers, 'content-type': 'application/json' },
    body: JSON.stringify({ ...event, id: 'cafebabe', screenshotPath: issuedPath }),
  });
  assert.equal(replayedForAnotherEvent.status, 400);
});

test('agent JSON routes reject oversized bodies before body-carried authentication', async (t) => {
  const root = fixture(t);
  const info = await startLiveServer(t, root);
  const base = `http://127.0.0.1:${info.port}`;
  const oversized = JSON.stringify({
    token: crypto.randomUUID(),
    padding: 'x'.repeat(1024 * 1024),
  });

  const response = await fetch(`${base}/poll`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: oversized,
  });

  assert.equal(response.status, 413);
  const unauthenticatedBrowserEvent = await fetch(`${base}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: oversized,
  });
  const unauthenticatedAuthorize = await fetch(`${base}/authorize-browser`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${crypto.randomUUID()}`,
      'content-type': 'application/json',
    },
    body: oversized,
  });
  assert.equal(unauthenticatedBrowserEvent.status, 413);
  assert.equal(unauthenticatedAuthorize.status, 413);
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

test('live instructions never elevate checkout-controlled commands and authorize the preview origin', () => {
  const reference = fs.readFileSync(liveReferencePath, 'utf-8');

  assert.doesNotMatch(reference, /require_escalated|sandbox_permissions|approval escape|elevat(?:e|ed|ion)/i);
  assert.match(reference, /live-authorize-browser\.mjs --origin/i);
  assert.match(reference, /authorize[^.\n]*before[^.\n]*(?:navigate|open)/i);
});

test('design sidecar components render in scriptless CSP-sandboxed iframes', () => {
  const browserSource = fs.readFileSync(liveBrowserPath, 'utf-8');
  const renderer = browserSource.slice(
    browserSource.indexOf('function renderComponentTiles'),
    browserSource.indexOf('function groupByKind'),
  );

  assert.match(renderer, /createElement\(['"]iframe['"]\)/);
  assert.match(renderer, /setAttribute\(['"]sandbox['"],\s*['"]['"]\)/);
  assert.match(renderer, /script-src 'none'/);
  assert.match(renderer, /default-src 'none'/);
  assert.match(renderer, /\.inert\s*=\s*true/);
  assert.match(renderer, /\.tabIndex\s*=\s*-1/);
  assert.match(renderer, /pointer-events:none/);
  assert.match(renderer, /\.srcdoc\s*=/);
  assert.doesNotMatch(renderer, /attachShadow|innerHTML\s*=\s*c\.html/);
});
