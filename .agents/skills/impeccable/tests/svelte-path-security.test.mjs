import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { resolveDesignSidecarPath } from '../scripts/lib/impeccable-paths.mjs';
import {
  findSvelteComponentManifest,
  inlineSvelteComponentAccept,
  removeAllSvelteComponentSessions,
  removeSvelteComponentSession,
  scaffoldSvelteComponentSession,
} from '../scripts/live/svelte-component.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-svelte-path-security-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

const liveAcceptPath = fileURLToPath(new URL('../scripts/live-accept.mjs', import.meta.url));

test('Svelte session cleanup never follows the component root through a symlink', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const outside = path.join(root, 'outside');
  const singleSession = path.join(outside, 'deadbeef');
  const bulkSession = path.join(outside, 'keep-dir');
  fs.mkdirSync(path.join(project, 'node_modules'), { recursive: true });
  fs.mkdirSync(singleSession, { recursive: true });
  fs.mkdirSync(bulkSession, { recursive: true });
  fs.writeFileSync(path.join(singleSession, 'sentinel.txt'), 'single\n');
  fs.writeFileSync(path.join(bulkSession, 'sentinel.txt'), 'bulk\n');
  fs.symlinkSync(outside, path.join(project, 'node_modules', '.impeccable-live'));

  removeSvelteComponentSession('deadbeef', project);
  removeAllSvelteComponentSessions(project);

  assert.equal(fs.readFileSync(path.join(singleSession, 'sentinel.txt'), 'utf-8'), 'single\n');
  assert.equal(fs.readFileSync(path.join(bulkSession, 'sentinel.txt'), 'utf-8'), 'bulk\n');
});

test('Svelte session cleanup pins the component root across a parent swap', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const componentRoot = path.join(project, 'node_modules', '.impeccable-live');
  const heldRoot = path.join(project, 'node_modules', '.impeccable-live-held');
  const outsideRoot = path.join(root, 'outside');
  fs.mkdirSync(path.join(componentRoot, 'deadbeef'), { recursive: true });
  fs.mkdirSync(path.join(outsideRoot, 'deadbeef'), { recursive: true });
  fs.writeFileSync(path.join(componentRoot, 'deadbeef', 'inside.txt'), 'inside\n');
  fs.writeFileSync(path.join(outsideRoot, 'deadbeef', 'outside.txt'), 'outside\n');

  const originalOpen = fs.openSync;
  let swapped = false;
  fs.openSync = function openWithParentSwap(file, ...args) {
    if (!swapped && String(file).startsWith('/proc/self/fd/') && path.basename(String(file)) === 'deadbeef') {
      const descriptor = originalOpen.call(fs, file, ...args);
      swapped = true;
      fs.renameSync(componentRoot, heldRoot);
      fs.symlinkSync(outsideRoot, componentRoot, 'dir');
      return descriptor;
    }
    return originalOpen.call(fs, file, ...args);
  };
  t.after(() => { fs.openSync = originalOpen; });

  removeSvelteComponentSession('deadbeef', project);

  assert.equal(swapped, true);
  assert.equal(fs.existsSync(path.join(heldRoot, 'deadbeef')), false);
  assert.equal(fs.readFileSync(path.join(outsideRoot, 'deadbeef', 'outside.txt'), 'utf-8'), 'outside\n');
});

test('Svelte session scaffolding refuses a symlinked component root', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const outside = path.join(root, 'outside');
  fs.mkdirSync(path.join(project, 'node_modules'), { recursive: true });
  fs.mkdirSync(outside, { recursive: true });
  fs.symlinkSync(outside, path.join(project, 'node_modules', '.impeccable-live'));

  assert.throws(
    () => scaffoldSvelteComponentSession({
      id: 'deadbeef',
      count: 1,
      sourceFile: 'src/App.svelte',
      sourceStartLine: 1,
      sourceEndLine: 1,
      originalLines: ['<p>Original</p>'],
      cwd: project,
    }),
    /symbolic link/,
  );

  assert.equal(fs.existsSync(path.join(outside, '__runtime.js')), false);
  assert.equal(fs.existsSync(path.join(outside, 'deadbeef')), false);
});

test('Svelte accept rejects a manifest redirected to a non-Svelte project file', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const source = path.join(project, 'src', 'App.svelte');
  const packageFile = path.join(project, 'package.json');
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, '<p>Original</p>\n');
  fs.writeFileSync(packageFile, '{"private":true}\n');

  const { manifestFile, componentDir } = scaffoldSvelteComponentSession({
    id: 'deadbeef',
    count: 1,
    sourceFile: 'src/App.svelte',
    sourceStartLine: 1,
    sourceEndLine: 1,
    originalLines: ['<p>Original</p>'],
    cwd: project,
  });
  const manifestPath = path.join(project, manifestFile);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));
  manifest.sourceFile = 'package.json';
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  fs.writeFileSync(path.join(project, componentDir, 'v1.svelte'), '<p>Replaced</p>\n');

  assert.throws(
    () => findSvelteComponentManifest('deadbeef', project),
    /manifest|Svelte source/i,
  );
  assert.equal(fs.readFileSync(packageFile, 'utf-8'), '{"private":true}\n');
});

test('Svelte accept refuses a stale source range instead of overwriting newer source', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const source = path.join(project, 'src', 'App.svelte');
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, '<p>Original</p>\n');

  const { componentDir } = scaffoldSvelteComponentSession({
    id: 'deadbeef',
    count: 1,
    sourceFile: 'src/App.svelte',
    sourceStartLine: 1,
    sourceEndLine: 1,
    originalLines: ['<p>Original</p>'],
    cwd: project,
  });
  fs.writeFileSync(path.join(project, componentDir, 'v1.svelte'), '<p>Variant</p>\n');
  fs.writeFileSync(source, '<p>Changed elsewhere</p>\n');

  const manifest = findSvelteComponentManifest('deadbeef', project);
  assert.throws(
    () => inlineSvelteComponentAccept(manifest, 1, null, project),
    /digest|source.*changed|stale/i,
  );
  assert.equal(fs.readFileSync(source, 'utf-8'), '<p>Changed elsewhere</p>\n');
});

test('Svelte accept still applies an edited variant when the source binding is unchanged', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const source = path.join(project, 'src', 'App.svelte');
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.writeFileSync(source, '<p>{name}</p>\n');

  const { componentDir } = scaffoldSvelteComponentSession({
    id: 'deadbeef',
    count: 1,
    sourceFile: 'src/App.svelte',
    sourceStartLine: 1,
    sourceEndLine: 1,
    originalLines: ['<p>{name}</p>'],
    cwd: project,
  });
  fs.writeFileSync(path.join(project, componentDir, 'v1.svelte'), '<p class="chosen">{name}</p>\n');

  const manifest = findSvelteComponentManifest('deadbeef', project);
  const result = inlineSvelteComponentAccept(manifest, 1, null, project);

  assert.equal(result.handled, true);
  assert.equal(fs.readFileSync(source, 'utf-8'), '<p class="chosen">{name}</p>\n');
});

test('design sidecar resolution ignores symlinks to files outside the project', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const outside = path.join(root, 'outside.json');
  fs.mkdirSync(project, { recursive: true });
  fs.writeFileSync(outside, '{"secret":"outside"}\n');
  fs.symlinkSync(outside, path.join(project, 'DESIGN.json'));

  assert.equal(resolveDesignSidecarPath(project, project), null);
});

test('live accept never searches or rewrites a symlinked top-level source directory', (t) => {
  const root = fixture(t);
  const project = path.join(root, 'project');
  const outside = path.join(root, 'outside');
  const outsideSource = path.join(outside, 'page.html');
  const original = [
    '<!-- impeccable-variants-start deadbeef -->',
    '<div data-impeccable-variant="original">Original</div>',
    '<div data-impeccable-variant="1">Variant</div>',
    '<!-- impeccable-variants-end deadbeef -->',
    '',
  ].join('\n');
  fs.mkdirSync(project, { recursive: true });
  fs.mkdirSync(outside, { recursive: true });
  fs.writeFileSync(outsideSource, original);
  fs.symlinkSync(outside, path.join(project, 'src'));

  const output = execFileSync(
    process.execPath,
    [liveAcceptPath, '--id', 'deadbeef', '--discard'],
    { cwd: project, encoding: 'utf-8' },
  );

  assert.equal(JSON.parse(output).handled, false);
  assert.equal(fs.readFileSync(outsideSource, 'utf-8'), original);
});
