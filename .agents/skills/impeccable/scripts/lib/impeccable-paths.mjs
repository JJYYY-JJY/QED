import fs from 'node:fs';
import path from 'node:path';
import { resolveProjectRoot } from '../context.mjs';
import {
  isPathInsideOrEqual,
  readContainedFile,
  removeContainedFile,
  resolveContainedPath,
  writeContainedFile,
} from './safe-fs.mjs';
export { IMPECCABLE_COMMAND_PREFIX } from './provider.mjs';

export const IMPECCABLE_DIR = '.impeccable';
export const LIVE_DIR = 'live';
export const CRITIQUE_DIR = 'critique';

export function getImpeccableDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(root, IMPECCABLE_DIR), { allowMissing: true });
}

export function getDesignSidecarPath(cwd = process.cwd(), options = {}) {
  return path.join(getImpeccableDir(cwd, options), 'design.json');
}

export function getDesignSidecarCandidates(cwd = process.cwd(), contextDir = cwd, options = {}) {
  const projectRoot = resolveProjectRoot(cwd, options);
  const candidates = [
    getDesignSidecarPath(cwd, options),
    path.join(projectRoot, 'DESIGN.json'),
  ];
  const contextLegacy = path.join(contextDir, 'DESIGN.json');
  if (!candidates.includes(contextLegacy)) candidates.push(contextLegacy);
  return candidates;
}

export function resolveDesignSidecarPath(cwd = process.cwd(), contextDir = cwd, options = {}) {
  const projectRoot = resolveProjectRoot(cwd, options);
  const externalContextRoot = path.resolve(contextDir || projectRoot);
  for (const candidate of getDesignSidecarCandidates(cwd, contextDir, options)) {
    const root = isPathInsideOrEqual(projectRoot, candidate)
      ? projectRoot
      : externalContextRoot;
    try {
      return resolveContainedPath(root, candidate, { allowMissing: false, type: 'file' });
    } catch {
      /* try next */
    }
  }
  return null;
}

export function getLiveDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(getImpeccableDir(cwd, options), LIVE_DIR), { allowMissing: true });
}

export function getLiveConfigPath(cwd = process.cwd(), options = {}) {
  return path.join(getLiveDir(cwd, options), 'config.json');
}

export function getLegacyLiveConfigPath(scriptsDir) {
  return path.join(scriptsDir, 'config.json');
}

export function resolveLiveConfigPath({ cwd = process.cwd(), scriptsDir, env = process.env, targetPath } = {}) {
  const root = resolveProjectRoot(cwd, { targetPath });
  if (env.IMPECCABLE_LIVE_CONFIG && env.IMPECCABLE_LIVE_CONFIG.trim()) {
    const configured = env.IMPECCABLE_LIVE_CONFIG.trim();
    const resolved = path.isAbsolute(configured) ? configured : path.resolve(cwd, configured);
    return resolveContainedPath(root, resolved, { allowMissing: false, type: 'file' });
  }
  const primary = getLiveConfigPath(cwd, { targetPath });
  if (fs.existsSync(primary)) {
    resolveContainedPath(resolveProjectRoot(cwd, { targetPath }), primary, { allowMissing: false, type: 'file' });
    return primary;
  }
  if (scriptsDir) {
    const legacy = getLegacyLiveConfigPath(scriptsDir);
    if (fs.existsSync(legacy)) {
      return resolveContainedPath(root, legacy, { allowMissing: false, type: 'file' });
    }
  }
  return primary;
}

export function getLiveServerPath(cwd = process.cwd(), options = {}) {
  return path.join(getLiveDir(cwd, options), 'server.json');
}

export function getLegacyLiveServerPath(cwd = process.cwd(), options = {}) {
  return path.join(resolveProjectRoot(cwd, options), '.impeccable-live.json');
}

export function readLiveServerInfo(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  for (const filePath of [getLiveServerPath(cwd, options), getLegacyLiveServerPath(cwd, options)]) {
    try {
      const info = JSON.parse(readContainedFile(root, filePath, 'utf-8'));
      if (!isValidLiveServerInfo(info)) continue;
      if (!isLiveServerPidReachable(info.pid)) {
        try { removeContainedFile(root, filePath, { force: true }); } catch {}
        continue;
      }
      return { info, path: filePath };
    } catch {
      /* try next */
    }
  }
  return null;
}

export function isValidLiveServerInfo(info) {
  return !!(
    info
    && typeof info === 'object'
    && !Array.isArray(info)
    && Number.isSafeInteger(info.pid)
    && info.pid > 0
    && Number.isInteger(info.port)
    && info.port >= 1
    && info.port <= 65535
    && typeof info.token === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(info.token)
  );
}

export function isLiveServerPidReachable(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // ESRCH means "no such process". EPERM means the process exists but this
    // user cannot signal it, so the live server info is still valid.
    return err?.code !== 'ESRCH';
  }
}

export function writeLiveServerInfo(cwd = process.cwd(), info, options = {}) {
  if (!isValidLiveServerInfo(info)) throw new Error('Invalid live server metadata');
  const root = resolveProjectRoot(cwd, options);
  const filePath = getLiveServerPath(cwd, options);
  writeContainedFile(root, filePath, JSON.stringify(info), { encoding: 'utf-8' });
  return filePath;
}

export function removeLiveServerInfo(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  for (const filePath of [getLiveServerPath(cwd, options), getLegacyLiveServerPath(cwd, options)]) {
    try { removeContainedFile(root, filePath, { force: true }); } catch {}
  }
}

export function getLiveSessionsDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(getLiveDir(cwd, options), 'sessions'), { allowMissing: true });
}

export function getLegacyLiveSessionsDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(root, '.impeccable-live', 'sessions'), { allowMissing: true });
}

export function getLiveAnnotationsDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(getLiveDir(cwd, options), 'annotations'), { allowMissing: true });
}

export function getCritiqueDir(cwd = process.cwd(), options = {}) {
  const root = resolveProjectRoot(cwd, options);
  return resolveContainedPath(root, path.join(getImpeccableDir(cwd, options), CRITIQUE_DIR), { allowMissing: true });
}

export function getLegacyLiveAnnotationsDir(cwd = process.cwd(), options = {}) {
  return path.join(resolveProjectRoot(cwd, options), '.impeccable-live', 'annotations');
}
