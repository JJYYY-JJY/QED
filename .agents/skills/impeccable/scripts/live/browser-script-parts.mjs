import fs from 'node:fs';
import path from 'node:path';

export const LIVE_BROWSER_SCRIPT_PARTS = Object.freeze([
  Object.freeze({ name: 'session-state', file: 'live-browser-session.js' }),
  Object.freeze({ name: 'dom-helpers', file: 'live-browser-dom.js' }),
  Object.freeze({ name: 'browser-ui', file: 'live-browser.js' }),
]);

export function resolveLiveBrowserScriptParts(scriptsDir, parts = LIVE_BROWSER_SCRIPT_PARTS) {
  if (!scriptsDir) throw new Error('scriptsDir is required');
  return parts.map((part, index) => ({
    ...part,
    index,
    path: path.join(scriptsDir, part.file),
  }));
}

export function assertLiveBrowserScriptParts(parts, exists = fs.existsSync) {
  for (const part of parts) {
    if (!exists(part.path)) {
      throw new Error(`Live browser script part missing: ${part.name} (${part.path})`);
    }
  }
  return parts;
}

export function readLiveBrowserScriptParts(parts, readFile = (filePath) => fs.readFileSync(filePath, 'utf-8')) {
  return parts.map((part) => ({
    ...part,
    source: readFile(part.path),
  }));
}

export function assertLiveBootstrapCredentials(port, token) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('Live bootstrap port must be an integer from 1 to 65535');
  }
  if (
    typeof token !== 'string'
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(token)
  ) {
    throw new Error('Live bootstrap token must be a UUID');
  }
}

export function assembleLiveBrowserScript({ token, port, vocabulary, commandPrefix = '/', parts }) {
  assertLiveBootstrapCredentials(port, token);
  const prelude =
    `(function () {\n` +
    `'use strict';\n` +
    `const IMPECCABLE_TOKEN = ${JSON.stringify(token)};\n` +
    `const IMPECCABLE_PORT = ${port};\n` +
    `const IMPECCABLE_COMMAND_PREFIX = ${JSON.stringify(commandPrefix)};\n` +
    // Canonical command vocabulary (values + labels + icons). live-browser.js
    // builds its action picker from this instead of an inline copy.
    `const IMPECCABLE_VOCAB = ${JSON.stringify(vocabulary)};\n`;

  const body = parts.map((part) => {
    const file = part.file || path.basename(part.path || '');
    return `// --- impeccable live script part: ${part.name} (${file}) ---\n${part.source}`;
  }).join('\n');

  return prelude + body + '\n})();\n';
}
