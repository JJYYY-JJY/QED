#!/usr/bin/env node

import { fileURLToPath } from 'node:url';

import { readLiveServerInfo } from './lib/impeccable-paths.mjs';
import { normalizePreviewOrigin } from './live/browser-authorization.mjs';

function argValue(args, flag) {
  const inline = args.find((arg) => arg.startsWith(`${flag}=`));
  if (inline) return inline.slice(flag.length + 1);
  const index = args.indexOf(flag);
  return index === -1 ? null : args[index + 1] || null;
}

export async function authorizeBrowserCli(args = process.argv.slice(2), cwd = process.cwd()) {
  if (args.includes('--help') || args.includes('-h')) {
    console.log(`Usage: node live-authorize-browser.mjs --origin APP_URL

Register the exact app preview origin with the running Impeccable helper.
Run this after resolving the app URL and before opening or navigating to it.`);
    return;
  }

  const rawOrigin = argValue(args, '--origin');
  if (!rawOrigin) throw new Error('Missing --origin APP_URL');
  const origin = normalizePreviewOrigin(rawOrigin);
  const record = readLiveServerInfo(cwd);
  if (!record?.info?.port || !record.info.token) {
    throw new Error('No running live server found');
  }

  const response = await fetch(`http://127.0.0.1:${record.info.port}/authorize-browser`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${record.info.token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ origin }),
    signal: AbortSignal.timeout(5_000),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Browser authorization failed with HTTP ${response.status}`);
  }
  console.log(JSON.stringify({ ok: true, origin: payload.origin }));
}

const runningFile = process.argv[1];
if (runningFile && fileURLToPath(import.meta.url) === runningFile) {
  authorizeBrowserCli().catch((error) => {
    console.error(JSON.stringify({ ok: false, error: error.message }));
    process.exitCode = 1;
  });
}
