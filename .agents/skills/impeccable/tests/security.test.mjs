import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { loadContext } from '../scripts/context.mjs';
import { readLatestSnapshot } from '../scripts/critique-storage.mjs';
import { walkDir } from '../scripts/detector/node/file-system.mjs';
import { persistCache, writeAuditLog } from '../scripts/hook-lib.mjs';
import { repairHookManifests } from '../scripts/hook-admin.mjs';
import { writeDetectionConfig } from '../scripts/lib/impeccable-config.mjs';
import { isGeneratedFile } from '../scripts/lib/is-generated.mjs';
import { readLiveServerInfo, writeLiveServerInfo } from '../scripts/lib/impeccable-paths.mjs';
import {
  appendContainedFile,
  readContainedFile,
  removeContainedFile,
  resolveContainedPath,
  writeContainedFile,
} from '../scripts/lib/safe-fs.mjs';
import * as liveModule from '../scripts/live.mjs';
import { resolveFiles } from '../scripts/live-inject.mjs';
import { buildManualEditEvidence } from '../scripts/live-manual-edit-evidence.mjs';
import { stageEntry } from '../scripts/live/manual-edits-buffer.mjs';
import { createLiveSessionStore } from '../scripts/live/session-store.mjs';
import { componentSessionDir, resolveSourceFile } from '../scripts/live/svelte-component.mjs';
import { applySvelteKitLiveAdapter } from '../scripts/live/sveltekit-adapter.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-security-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test('generated-file checks do not evaluate shell syntax in filenames', (t) => {
  const root = fixture(t);
  const marker = path.join(root, 'IS_GENERATED_PWNED');
  const crafted = path.join(root, 'source-$(touch IS_GENERATED_PWNED).tsx');
  fs.writeFileSync(crafted, 'export const value = true;\n');
  execFileSync('git', ['init', '--quiet'], { cwd: root });

  isGeneratedFile(crafted, { cwd: root });

  assert.equal(fs.existsSync(marker), false);
});

test('live metadata rejects non-integer ports before command dispatch', (t) => {
  const root = fixture(t);
  const liveDir = path.join(root, '.impeccable', 'live');
  fs.mkdirSync(liveDir, { recursive: true });
  fs.writeFileSync(
    path.join(liveDir, 'server.json'),
    JSON.stringify({ pid: process.pid, port: '43210$(touch LIVE_PORT_PWNED)', token: crypto.randomUUID() }),
  );

  assert.equal(readLiveServerInfo(root), null);
  assert.equal(fs.existsSync(path.join(root, 'LIVE_PORT_PWNED')), false);
});

test('live helper passes arguments without a shell', (t) => {
  const root = fixture(t);
  const liveDir = path.join(root, '.impeccable', 'live');
  fs.mkdirSync(liveDir, { recursive: true });
  fs.writeFileSync(path.join(root, 'index.html'), '<html><body></body></html>\n');
  fs.writeFileSync(
    path.join(liveDir, 'config.json'),
    JSON.stringify({
      files: ['index.html'],
      insertBefore: '</body>',
      commentSyntax: 'html',
      cspChecked: true,
    }),
  );

  assert.equal(typeof liveModule.runScript, 'function');
  liveModule.runScript(
    'live-inject.mjs',
    ['--port', '43210$(touch LIVE_PORT_PWNED)'],
    { cwd: root },
  );

  assert.equal(fs.existsSync(path.join(root, 'LIVE_PORT_PWNED')), false);
});

test('contained file helpers reject traversal and symlink components', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'sentinel.txt');
  fs.writeFileSync(sentinel, 'unchanged\n');
  fs.mkdirSync(path.join(root, 'state'), { recursive: true });
  fs.symlinkSync(sentinel, path.join(root, 'state', 'linked.txt'));

  assert.throws(
    () => resolveContainedPath(root, path.join(root, '..', path.basename(outside), 'sentinel.txt')),
    /escapes project root/,
  );
  assert.throws(
    () => readContainedFile(root, path.join(root, 'state', 'linked.txt'), 'utf-8'),
    /symbolic link/,
  );
  assert.throws(
    () => writeContainedFile(root, path.join(root, 'state', 'linked.txt'), 'changed\n'),
    /symbolic link/,
  );
  assert.throws(
    () => appendContainedFile(root, path.join(root, 'state', 'linked.txt'), 'changed\n'),
    /symbolic link/,
  );
  assert.equal(fs.readFileSync(sentinel, 'utf-8'), 'unchanged\n');
});

test('contained file helpers create and update regular project files', (t) => {
  const root = fixture(t);
  const target = path.join(root, 'nested', 'state.jsonl');

  writeContainedFile(root, target, 'one\n');
  appendContainedFile(root, target, 'two\n');

  assert.equal(readContainedFile(root, target, 'utf-8'), 'one\ntwo\n');
});

test('contained writes stay pinned when a validated parent is swapped', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const stateDir = path.join(root, 'state');
  const heldDir = path.join(root, 'state-held');
  fs.mkdirSync(stateDir);
  fs.writeFileSync(path.join(stateDir, 'target.txt'), 'inside\n');
  fs.writeFileSync(path.join(outside, 'target.txt'), 'outside\n');

  const originalOpen = fs.openSync;
  let swapped = false;
  fs.openSync = function openWithParentSwap(file, ...args) {
    if (!swapped && path.basename(String(file)).startsWith('.target.txt.')) {
      swapped = true;
      fs.renameSync(stateDir, heldDir);
      fs.symlinkSync(outside, stateDir, 'dir');
    }
    return originalOpen.call(fs, file, ...args);
  };
  t.after(() => { fs.openSync = originalOpen; });

  writeContainedFile(root, 'state/target.txt', 'updated\n', { encoding: 'utf-8' });

  assert.equal(swapped, true);
  assert.equal(fs.readFileSync(path.join(outside, 'target.txt'), 'utf-8'), 'outside\n');
  assert.equal(fs.readFileSync(path.join(heldDir, 'target.txt'), 'utf-8'), 'updated\n');
});

test('contained reads stay pinned when their parent is swapped before leaf open', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const stateDir = path.join(root, 'state');
  const heldDir = path.join(root, 'state-held');
  fs.mkdirSync(stateDir);
  fs.writeFileSync(path.join(stateDir, 'target.txt'), 'inside\n');
  fs.writeFileSync(path.join(outside, 'target.txt'), 'outside secret\n');

  const originalOpen = fs.openSync;
  let swapped = false;
  fs.openSync = function openWithParentSwap(file, ...args) {
    if (!swapped && String(file).startsWith('/proc/self/fd/') && path.basename(String(file)) === 'target.txt') {
      swapped = true;
      fs.renameSync(stateDir, heldDir);
      fs.symlinkSync(outside, stateDir, 'dir');
    }
    return originalOpen.call(fs, file, ...args);
  };
  t.after(() => { fs.openSync = originalOpen; });

  assert.equal(readContainedFile(root, 'state/target.txt', 'utf-8'), 'inside\n');
  assert.equal(swapped, true);
});

test('contained file removal stays pinned when its parent is swapped', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const stateDir = path.join(root, 'state');
  const heldDir = path.join(root, 'state-held');
  fs.mkdirSync(stateDir);
  fs.writeFileSync(path.join(stateDir, 'target.txt'), 'inside\n');
  fs.writeFileSync(path.join(outside, 'target.txt'), 'outside\n');

  const originalLstat = fs.lstatSync;
  let swapped = false;
  fs.lstatSync = function lstatWithParentSwap(file, ...args) {
    if (!swapped && String(file).startsWith('/proc/self/fd/') && path.basename(String(file)) === 'target.txt') {
      swapped = true;
      fs.renameSync(stateDir, heldDir);
      fs.symlinkSync(outside, stateDir, 'dir');
    }
    return originalLstat.call(fs, file, ...args);
  };
  t.after(() => { fs.lstatSync = originalLstat; });

  assert.equal(removeContainedFile(root, 'state/target.txt'), true);
  assert.equal(swapped, true);
  assert.equal(fs.existsSync(path.join(heldDir, 'target.txt')), false);
  assert.equal(fs.readFileSync(path.join(outside, 'target.txt'), 'utf-8'), 'outside\n');
});

test('project context and critique readers ignore symlinked host files', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const product = path.join(outside, 'secret-product.md');
  const critique = path.join(outside, 'secret-critique.md');
  fs.writeFileSync(product, 'TOP_SECRET_PRODUCT\n');
  fs.writeFileSync(critique, 'TOP_SECRET_CRITIQUE\n');
  fs.symlinkSync(product, path.join(root, 'PRODUCT.md'));
  fs.mkdirSync(path.join(root, '.impeccable', 'critique'), { recursive: true });
  fs.symlinkSync(
    critique,
    path.join(root, '.impeccable', 'critique', '20260716T000000Z__index.md.md'),
  );

  const context = loadContext(root);
  const latest = readLatestSnapshot('index.md', { cwd: root });

  assert.equal(context.hasProduct, false);
  assert.equal(context.product, null);
  assert.equal(latest, null);
});

test('detector directory walks skip symlinked source files', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const secret = path.join(outside, 'secret.tsx');
  fs.writeFileSync(secret, 'const TOP_SECRET = true;\n');
  fs.symlinkSync(secret, path.join(root, 'linked.tsx'));

  assert.deepEqual(walkDir(root), []);
});

test('project state writers refuse symlinked destinations', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'sentinel.json');
  fs.writeFileSync(sentinel, '{"unchanged":true}\n');
  fs.mkdirSync(path.join(root, '.impeccable', 'live'), { recursive: true });

  fs.symlinkSync(sentinel, path.join(root, '.impeccable', 'config.json'));
  assert.throws(() => writeDetectionConfig(root, { ignoreRules: [] }), /symbolic link/);

  fs.symlinkSync(sentinel, path.join(root, '.impeccable', 'live', 'server.json'));
  assert.throws(
    () => writeLiveServerInfo(root, {
      pid: process.pid,
      port: 8400,
      token: crypto.randomUUID(),
    }),
    /symbolic link/,
  );

  assert.equal(fs.readFileSync(sentinel, 'utf-8'), '{"unchanged":true}\n');
});

test('manual-edit state and evidence never follow repository symlinks', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sourceSecret = path.join(outside, 'source-secret.tsx');
  const bufferSecret = path.join(outside, 'buffer-secret.json');
  fs.writeFileSync(sourceSecret, 'export const TOP_SECRET_SOURCE = true;\n');
  fs.writeFileSync(bufferSecret, '{"version":1,"entries":[]}\n');
  fs.mkdirSync(path.join(root, 'src'), { recursive: true });
  fs.mkdirSync(path.join(root, '.impeccable', 'live'), { recursive: true });
  fs.symlinkSync(sourceSecret, path.join(root, 'src', 'linked.tsx'));

  const entry = {
    id: 'entry1',
    pageUrl: '/',
    element: {},
    ops: [{
      ref: 'ref1',
      originalText: 'TOP_SECRET_SOURCE',
      newText: 'safe',
      sourceHint: { file: 'src/linked.tsx', line: 1 },
    }],
  };
  stageEntry(root, entry);
  const evidence = buildManualEditEvidence({ cwd: root, pageUrl: '/' });
  assert.equal(JSON.stringify(evidence).includes('export const TOP_SECRET_SOURCE'), false);
  assert.equal(evidence.candidates[0].sourceHint.status, 'unsafe_path');

  fs.rmSync(path.join(root, '.impeccable', 'live', 'pending-manual-edits.json'));
  fs.symlinkSync(bufferSecret, path.join(root, '.impeccable', 'live', 'pending-manual-edits.json'));
  assert.throws(() => stageEntry(root, entry), /manual_edit_buffer_unreadable|symbolic link/);
  assert.equal(fs.readFileSync(bufferSecret, 'utf-8'), '{"version":1,"entries":[]}\n');
});

test('manual-edit evidence rejects a symlinked search root', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  fs.writeFileSync(path.join(outside, 'secret.tsx'), 'export const SEARCH_ROOT_SECRET = true;\n');
  fs.symlinkSync(outside, path.join(root, 'src'));
  stageEntry(root, {
    id: 'entry1',
    pageUrl: '/',
    element: {},
    ops: [{
      ref: 'ref1',
      tag: 'div',
      originalText: 'SEARCH_ROOT_SECRET',
      newText: 'safe',
    }],
  });

  const evidence = buildManualEditEvidence({ cwd: root, pageUrl: '/' });

  assert.equal(JSON.stringify(evidence).includes('export const SEARCH_ROOT_SECRET'), false);
  assert.deepEqual(evidence.candidates[0].textMatches, []);
});

test('live drift scan rejects a symlinked page root', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  fs.writeFileSync(path.join(outside, 'outside.html'), '<p>outside checkout</p>\n');
  fs.symlinkSync(outside, path.join(root, 'public'));

  assert.equal(liveModule.scanForDrift(root, [], {}), null);
});

test('session and hook state cannot escape through symlinks or config paths', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const cacheSecret = path.join(outside, 'cache.json');
  fs.writeFileSync(cacheSecret, 'unchanged\n');
  fs.mkdirSync(path.join(root, '.impeccable', 'live'), { recursive: true });
  fs.symlinkSync(outside, path.join(root, '.impeccable', 'live', 'sessions'));
  assert.throws(() => createLiveSessionStore({ cwd: root }), /symbolic link/);

  fs.symlinkSync(cacheSecret, path.join(root, '.impeccable', 'hook.cache.json'));
  assert.equal(persistCache(root, { version: 1, sessions: {} }), false);
  fs.writeFileSync(
    path.join(root, '.impeccable', 'config.json'),
    JSON.stringify({ hook: { auditLog: '../outside-audit.jsonl' } }),
  );
  assert.equal(writeAuditLog({}, { cwd: root, event: 'test' }, root), false);
  assert.equal(fs.existsSync(path.join(path.dirname(root), 'outside-audit.jsonl')), false);
  assert.equal(fs.readFileSync(cacheSecret, 'utf-8'), 'unchanged\n');
});

test('live source paths must remain regular files below the project root', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const source = path.join(outside, 'outside.svelte');
  fs.writeFileSync(source, '<p>outside</p>\n');
  fs.symlinkSync(source, path.join(root, 'linked.svelte'));

  assert.throws(
    () => resolveFiles(root, { files: ['../outside.html'], exclude: [] }),
    /escapes project root/,
  );
  assert.throws(() => resolveSourceFile('linked.svelte', root), /symbolic link/);
});

test('Svelte component session IDs cannot address arbitrary repository paths', (t) => {
  const root = fixture(t);

  assert.equal(
    componentSessionDir('deadbeef', root),
    path.join(root, 'node_modules', '.impeccable-live', 'deadbeef'),
  );
  assert.throws(() => componentSessionDir('../../escape', root), /session id/i);
  assert.throws(() => componentSessionDir('not-a-session', root), /session id/i);
});

test('hook installation refuses provider-manifest symlinks', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'hooks.json');
  fs.writeFileSync(sentinel, '{"unchanged":true}\n');
  fs.mkdirSync(path.join(root, '.agents', 'skills', 'impeccable'), { recursive: true });
  fs.mkdirSync(path.join(root, '.codex'), { recursive: true });
  fs.symlinkSync(sentinel, path.join(root, '.codex', 'hooks.json'));

  assert.throws(() => repairHookManifests(root), /symbolic link/);
  assert.equal(fs.readFileSync(sentinel, 'utf-8'), '{"unchanged":true}\n');
});

test('SvelteKit live adapter refuses symlinked project sources', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'layout.svelte');
  fs.writeFileSync(sentinel, '<p>unchanged</p>\n');
  fs.mkdirSync(path.join(root, 'src', 'routes'), { recursive: true });
  fs.writeFileSync(
    path.join(root, 'src', 'app.html'),
    '<html><head>%sveltekit.head%</head><body>%sveltekit.body%</body></html>\n',
  );
  fs.writeFileSync(path.join(root, 'svelte.config.js'), 'export default {};\n');
  fs.symlinkSync(sentinel, path.join(root, 'src', 'routes', '+layout.svelte'));

  assert.throws(() => applySvelteKitLiveAdapter({ cwd: root, port: 8400 }), /symbolic link/);
  assert.equal(fs.readFileSync(sentinel, 'utf-8'), '<p>unchanged</p>\n');
});
