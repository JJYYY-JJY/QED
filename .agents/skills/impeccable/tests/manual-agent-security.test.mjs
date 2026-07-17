import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  buildClaudeArgs,
  buildCodexArgs,
  buildCopyEditBatchPrompt,
  runCopyEditBatchAgent,
  runCopyEditPostApplyChecks,
} from '../scripts/live-copy-edit-agent.mjs';
import {
  collectManualApplyFiles,
  writeManualApplyTransaction,
} from '../scripts/live/manual-apply.mjs';
import { applyBufferedManualEditToLines } from '../scripts/live-wrap.mjs';

const scriptsDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'scripts');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-manual-security-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test('copy-edit agent runners keep repository writes sandboxed', () => {
  const codex = buildCodexArgs({
    cwd: '/tmp/project',
    env: {},
    resultPath: '/tmp/result.json',
  });
  const claude = buildClaudeArgs({ env: {} });

  assert.equal(codex.includes('--dangerously-bypass-approvals-and-sandbox'), false);
  assert.deepEqual(codex.slice(codex.indexOf('--sandbox'), codex.indexOf('--sandbox') + 2), [
    '--sandbox',
    'workspace-write',
  ]);
  assert.equal(claude.includes('bypassPermissions'), false);
  assert.equal(claude.includes('acceptEdits'), true);
});

test('copy-edit prompt makes the canonical repository boundary explicit', () => {
  const prompt = buildCopyEditBatchPrompt({ entries: [] }, { cwd: '/tmp/project' });

  assert.match(prompt, /canonical repository root/i);
  assert.match(prompt, /symbolic link/i);
});

test('project validation scripts require explicit opt-in', (t) => {
  const root = fixture(t);
  const marker = path.join(root, 'VALIDATION_RAN');
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({
    scripts: { 'impeccable:manual-edit-validate': 'touch VALIDATION_RAN' },
  }));

  const checks = runCopyEditPostApplyChecks({ cwd: root, files: [] });

  assert.equal(fs.existsSync(marker), false);
  assert.equal(checks.ok, true);
  assert.equal(checks.warnings.some((warning) => warning.reason === 'manual_edit_validation_requires_opt_in'), true);
});

test('mock copy-edit writes reject symlink destinations', async (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'sentinel.txt');
  fs.writeFileSync(sentinel, 'unchanged\n');
  fs.symlinkSync(sentinel, path.join(root, 'linked.txt'));

  await assert.rejects(
    runCopyEditBatchAgent(
      { entries: [] },
      {
        cwd: root,
        provider: 'mock',
        env: {
          IMPECCABLE_LIVE_COPY_AGENT_MOCK_WRITES: JSON.stringify({ 'linked.txt': 'changed\n' }),
        },
      },
    ),
    /symbolic link/,
  );
  assert.equal(fs.readFileSync(sentinel, 'utf-8'), 'unchanged\n');
});

test('wrap and insert CLIs reject explicit symlinked source files', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const sentinel = path.join(outside, 'sentinel.html');
  fs.writeFileSync(sentinel, '<main id="target">unchanged</main>\n');
  fs.symlinkSync(sentinel, path.join(root, 'linked.html'));

  const common = ['--id', 'session', '--count', '2', '--element-id', 'target', '--file', 'linked.html'];
  const wrapped = spawnSync(process.execPath, [path.join(scriptsDir, 'live-wrap.mjs'), ...common], {
    cwd: root,
    encoding: 'utf-8',
  });
  const inserted = spawnSync(process.execPath, [
    path.join(scriptsDir, 'live-insert.mjs'),
    ...common,
    '--position',
    'after',
  ], { cwd: root, encoding: 'utf-8' });

  assert.notEqual(wrapped.status, 0);
  assert.notEqual(inserted.status, 0);
  assert.match(wrapped.stderr, /symbolic link/i);
  assert.match(inserted.stderr, /symbolic link/i);
  assert.equal(fs.readFileSync(sentinel, 'utf-8'), '<main id="target">unchanged</main>\n');
});

test('buffered copy edits only replace one unambiguous visible text node', () => {
  const visible = applyBufferedManualEditToLines(
    ['<button title="Save">Save</button>'],
    0,
    {
      originalText: 'Save',
      newText: 'Continue',
      sourceHint: { line: 1 },
      tag: 'button',
    },
  );
  assert.equal(visible.changed, true);
  assert.deepEqual(visible.lines, ['<button title="Save">Continue</button>']);

  const hostileNewText = '</button><script>alert(1)</script><button>{dangerousCall()} & done';
  const escaped = applyBufferedManualEditToLines(
    ['<button>Save</button>'],
    0,
    {
      originalText: 'Save',
      newText: hostileNewText,
    },
  );
  assert.equal(escaped.changed, true);
  assert.equal(escaped.lines.join('\n').includes('<script>'), false);
  assert.equal(escaped.lines.join('\n').includes('{dangerousCall()}'), false);
  assert.deepEqual(escaped.lines, [
    '<button>&lt;/button&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;button&gt;&#123;dangerousCall()&#125; &amp; done</button>',
  ]);

  for (const source of [
    '<button title="Save">Submit</button>',
    '<script>const label = "Save";</script>',
    '<style>.Save { color: red; }</style>',
    '<div>Save</div><span>Save</span>',
  ]) {
    const rejected = applyBufferedManualEditToLines(
      [source],
      0,
      {
        originalText: 'Save',
        newText: 'Continue',
        sourceHint: { line: 1 },
      },
    );
    assert.equal(rejected.changed, false, source);
    assert.deepEqual(rejected.lines, [source]);
  }
});

test('manual Apply state and source scopes reject repository symlinks', (t) => {
  const root = fixture(t);
  const outside = fixture(t);
  const source = path.join(outside, 'source.tsx');
  const transaction = path.join(outside, 'transaction.json');
  fs.writeFileSync(source, 'export const secret = true;\n');
  fs.writeFileSync(transaction, '{"unchanged":true}\n');
  fs.mkdirSync(path.join(root, 'src'), { recursive: true });
  fs.mkdirSync(path.join(root, '.impeccable', 'live'), { recursive: true });
  fs.symlinkSync(source, path.join(root, 'src', 'linked.tsx'));
  fs.symlinkSync(
    transaction,
    path.join(root, '.impeccable', 'live', 'manual-edit-apply-transaction.json'),
  );
  const batch = {
    entries: [{ id: 'entry', ops: [{ sourceHint: { file: 'src/linked.tsx' } }] }],
  };

  assert.deepEqual(collectManualApplyFiles(batch, [], root), []);
  assert.throws(() => writeManualApplyTransaction({ cwd: root, batch }), /symbolic link/);
  assert.equal(fs.readFileSync(transaction, 'utf-8'), '{"unchanged":true}\n');
});
