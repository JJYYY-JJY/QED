import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { commitManualEdits } from '../scripts/live-commit-manual-edits.mjs';

function makeBatch(sourceFile) {
  return {
    pageUrl: '/',
    entries: [{
      id: 'entry-1',
      pageUrl: '/',
      ops: [{
        ref: 'copy-1',
        originalText: 'before',
        newText: 'after',
        sourceHint: { file: sourceFile, line: 1 },
      }],
    }],
    candidates: [],
  };
}

test('rollback never follows a source-hint symlink swapped to an external victim', async (t) => {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-rollback-'));
  t.after(() => fs.rmSync(fixtureRoot, { recursive: true, force: true }));

  const projectRoot = path.join(fixtureRoot, 'project');
  const sourceDir = path.join(projectRoot, 'src');
  const sourcePath = path.join(sourceDir, 'linked.tsx');
  const snapshotVictim = path.join(fixtureRoot, 'snapshot-victim.tsx');
  const rollbackVictim = path.join(fixtureRoot, 'rollback-victim.tsx');
  fs.mkdirSync(sourceDir, { recursive: true });
  fs.writeFileSync(snapshotVictim, 'snapshot content', 'utf8');
  fs.writeFileSync(rollbackVictim, 'keep this content', 'utf8');
  fs.symlinkSync(snapshotVictim, sourcePath);

  await commitManualEdits({
    cwd: projectRoot,
    provider: 'chat',
    batch: makeBatch('src/linked.tsx'),
    applyBatchToSource: async () => {
      fs.unlinkSync(sourcePath);
      fs.symlinkSync(rollbackVictim, sourcePath);
      return {
        status: 'error',
        message: 'forced failure',
        failed: [{ entryId: 'entry-1', reason: 'forced failure' }],
        files: ['src/linked.tsx'],
      };
    },
  });

  assert.equal(fs.readFileSync(rollbackVictim, 'utf8'), 'keep this content');
});

test('rollback refuses a regular source whose parent is swapped to an external symlink', async (t) => {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-rollback-'));
  t.after(() => fs.rmSync(fixtureRoot, { recursive: true, force: true }));

  const projectRoot = path.join(fixtureRoot, 'project');
  const sourceDir = path.join(projectRoot, 'src', 'section');
  const sourcePath = path.join(sourceDir, 'component.tsx');
  const externalDir = path.join(fixtureRoot, 'external');
  const externalVictim = path.join(externalDir, 'component.tsx');
  fs.mkdirSync(sourceDir, { recursive: true });
  fs.mkdirSync(externalDir);
  fs.writeFileSync(sourcePath, 'snapshot content', 'utf8');
  fs.writeFileSync(externalVictim, 'keep this content', 'utf8');

  const result = await commitManualEdits({
    cwd: projectRoot,
    provider: 'chat',
    batch: makeBatch('src/section/component.tsx'),
    applyBatchToSource: async () => {
      fs.rmSync(sourceDir, { recursive: true });
      fs.symlinkSync(externalDir, sourceDir, 'dir');
      return {
        status: 'error',
        message: 'forced failure',
        failed: [{ entryId: 'entry-1', reason: 'forced failure' }],
        files: ['src/section/component.tsx'],
      };
    },
  });

  assert.equal(fs.readFileSync(externalVictim, 'utf8'), 'keep this content');
  assert.equal(result.rolledBackFiles.includes('src/section/component.tsx'), false);
  assert.equal(
    result.rollbackFailures.some((failure) => (
      failure.file === 'src/section/component.tsx'
      && failure.reason === 'restore_failed'
    )),
    true,
  );
});

test('a missing source hint cannot delete a file through a swapped parent symlink', async (t) => {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-rollback-'));
  t.after(() => fs.rmSync(fixtureRoot, { recursive: true, force: true }));

  const projectRoot = path.join(fixtureRoot, 'project');
  const sourceDir = path.join(projectRoot, 'src', 'section');
  const externalDir = path.join(fixtureRoot, 'external');
  const externalVictim = path.join(externalDir, 'new-component.tsx');
  fs.mkdirSync(sourceDir, { recursive: true });
  fs.mkdirSync(externalDir);
  fs.writeFileSync(externalVictim, 'keep this content', 'utf8');

  const result = await commitManualEdits({
    cwd: projectRoot,
    provider: 'chat',
    batch: makeBatch('src/section/new-component.tsx'),
    applyBatchToSource: async () => {
      fs.rmSync(sourceDir, { recursive: true });
      fs.symlinkSync(externalDir, sourceDir, 'dir');
      return {
        status: 'error',
        message: 'forced failure',
        failed: [{ entryId: 'entry-1', reason: 'forced failure' }],
        files: ['src/section/new-component.tsx'],
      };
    },
  });

  assert.equal(fs.readFileSync(externalVictim, 'utf8'), 'keep this content');
  assert.deepEqual(result.rolledBackFiles, []);
});

test('rollback still restores a modified regular file inside the project', async (t) => {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-rollback-'));
  t.after(() => fs.rmSync(fixtureRoot, { recursive: true, force: true }));

  const projectRoot = path.join(fixtureRoot, 'project');
  const sourceDir = path.join(projectRoot, 'src');
  const sourcePath = path.join(sourceDir, 'component.tsx');
  fs.mkdirSync(sourceDir, { recursive: true });
  fs.writeFileSync(sourcePath, 'before', 'utf8');

  const result = await commitManualEdits({
    cwd: projectRoot,
    provider: 'chat',
    batch: makeBatch('src/component.tsx'),
    applyBatchToSource: async () => {
      fs.writeFileSync(sourcePath, 'after', 'utf8');
      return {
        status: 'error',
        message: 'forced failure',
        failed: [{ entryId: 'entry-1', reason: 'forced failure' }],
        files: ['src/component.tsx'],
      };
    },
  });

  assert.equal(fs.readFileSync(sourcePath, 'utf8'), 'before');
  assert.deepEqual(result.rolledBackFiles, ['src/component.tsx']);
  assert.deepEqual(result.rollbackFailures, []);
});
