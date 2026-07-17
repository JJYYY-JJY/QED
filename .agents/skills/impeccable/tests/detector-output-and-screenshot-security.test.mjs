import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { PNG } from 'pngjs';

import { formatFindings } from '../scripts/detector/cli/main.mjs';
import {
  compareScreenshotContrast,
} from '../scripts/detector/engines/visual/screenshot-contrast.mjs';

const testDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(testDir, '..', '..', '..', '..');
const detectorPath = path.join(
  projectRoot,
  '.agents',
  'skills',
  'impeccable',
  'scripts',
  'detector',
  'detect-antipatterns.mjs',
);

function makePngBase64(width, height, color) {
  const png = new PNG({ width, height });
  for (let offset = 0; offset < png.data.length; offset += 4) {
    png.data[offset] = color.r;
    png.data[offset + 1] = color.g;
    png.data[offset + 2] = color.b;
    png.data[offset + 3] = color.a ?? 255;
  }
  return PNG.sync.write(png).toString('base64');
}

test('human detector output neutralizes terminal controls while JSON remains lossless', () => {
  const finding = {
    file: 'proof\u001b]8;;https://attacker.invalid\u0007link\u001b]8;;\u0007.tsx',
    importedBy: ['caller\u001b[2J\nspoofed.tsx'],
    line: 7,
    antipattern: 'low-contrast\u009b31m',
    snippet: 'unsafe\u0007\r\nsnippet',
    description: 'description\u001b[31mred\u001b[0m',
  };

  const human = formatFindings([finding], false);
  assert.doesNotMatch(human, /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/u);
  assert.doesNotMatch(human, /\nspoofed\.tsx/u);
  assert.match(human, /\\u001b/u);
  assert.match(human, /\\u000a/u);

  const json = formatFindings([finding], true);
  assert.deepEqual(JSON.parse(json), [finding]);
  assert.doesNotMatch(json, /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/u);
});

test('detector CLI sanitizes control bytes supplied in human-facing arguments', () => {
  const maliciousScope = 'type\u001b]0;owned\u0007';
  const result = spawnSync(process.execPath, [detectorPath, `--scope=${maliciousScope}`], {
    cwd: projectRoot,
    encoding: 'utf8',
  });

  assert.equal(result.status, 1);
  assert.doesNotMatch(result.stderr, /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/u);
  assert.match(result.stderr, /type\\u001b/u);
});

test('screenshot contrast decoding and pixel analysis run without a browser page', async () => {
  const before = makePngBase64(4, 3, { r: 0, g: 0, b: 0 });
  const after = makePngBase64(4, 3, { r: 255, g: 255, b: 255 });

  const metrics = await compareScreenshotContrast(before, after, {
    preferRenderedForeground: true,
  });

  assert.equal(metrics.glyphPixels, 12);
  assert.equal(metrics.strongestDelta, 765);
  assert.equal(metrics.worstRatio, 21);
  assert.equal(metrics.p10Ratio, 21);
  assert.equal(metrics.medianRatio, 21);
});
