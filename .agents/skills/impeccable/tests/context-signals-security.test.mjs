import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { gatherSignals } from '../scripts/context-signals.mjs';

test('context signal detection never interpolates repository filenames into a shell', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'impeccable-signals-security-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  fs.writeFileSync(
    path.join(root, 'src', 'x;touch${IFS}CONTEXT_SIGNALS_PWNED;#.tsx'),
    'export const App = () => <main>safe</main>;\n',
  );

  const signals = await gatherSignals(root, { withDetection: true });

  assert.equal(fs.existsSync(path.join(root, 'CONTEXT_SIGNALS_PWNED')), false);
  assert.equal(signals.detection.attempted, true);
  assert.equal(Array.isArray(signals.detection.findings), true);
});
