#!/usr/bin/env node
/**
 * Pin/unpin sub-commands as standalone skill shortcuts.
 *
 * Usage:
 *   node <scripts_path>/pin.mjs pin <command>
 *   node <scripts_path>/pin.mjs unpin <command>
 *
 * `pin audit` creates a lightweight audit skill that redirects to Impeccable's audit workflow.
 * `unpin audit` removes that shortcut.
 *
 * The script discovers harness directories (.claude/skills, .cursor/skills, etc.)
 * in the project root and creates/removes the pin in all of them.
 */

import { existsSync, readFileSync } from 'node:fs';
import { basename, join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  removeContainedFileIf,
  resolveContainedPath,
  updateContainedFile,
} from './lib/safe-fs.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

// All known harness directories
const HARNESS_DIRS = [
  '.claude', '.cursor', '.gemini', '.codex', '.agents',
  '.trae', '.trae-cn', '.pi', '.opencode', '.kiro', '.rovodev',
];

const CODEX_HARNESSES = new Set(['.codex', '.agents']);

// Valid sub-command names
const VALID_COMMANDS = [
  'craft', 'init', 'extract', 'document', 'shape',
  'critique', 'audit',
  'polish', 'bolder', 'quieter', 'distill', 'harden', 'onboard', 'live',
  'animate', 'colorize', 'typeset', 'layout', 'delight', 'overdrive',
  'clarify', 'adapt', 'optimize',
];

// Marker to identify pinned skills (so unpin doesn't delete user skills)
const PIN_MARKER = '<!-- impeccable-pinned-skill -->';
const MAX_PINNED_SKILL_BYTES = 256 * 1024;

/**
 * Walk up from startDir to find a project root.
 */
function findProjectRoot(startDir = process.cwd()) {
  let dir = resolve(startDir);
  while (dir !== '/') {
    if (
      existsSync(join(dir, 'package.json')) ||
      existsSync(join(dir, '.git')) ||
      existsSync(join(dir, 'skills-lock.json'))
    ) {
      return dir;
    }
    const parent = resolve(dir, '..');
    if (parent === dir) break;
    dir = parent;
  }
  return resolve(startDir);
}

/**
 * Find harness skill directories that have an impeccable skill installed.
 */
function findHarnessDirs(projectRoot) {
  const dirs = [];
  for (const harness of HARNESS_DIRS) {
    const skillsDir = join(projectRoot, harness, 'skills');
    // Only pin in harness dirs that already have impeccable installed
    try {
      resolveContainedPath(projectRoot, skillsDir, { allowMissing: false, type: 'directory' });
      const installed = ['impeccable', 'i-impeccable'].some((name) => {
        try {
          resolveContainedPath(
            projectRoot,
            join(skillsDir, name),
            { allowMissing: false, type: 'directory' },
          );
          return true;
        } catch {
          return false;
        }
      });
      if (installed) dirs.push(skillsDir);
    } catch {
      // A missing or linked harness is not an eligible write destination.
    }
  }
  return dirs;
}

/**
 * Load command metadata (descriptions for pinned skills).
 */
function loadCommandMetadata() {
  const metadataPath = join(__dirname, 'command-metadata.json');
  if (existsSync(metadataPath)) {
    return JSON.parse(readFileSync(metadataPath, 'utf-8'));
  }
  return {};
}

/**
 * Generate a pinned skill's SKILL.md content.
 */
function commandPrefixForSkillsDir(skillsDir) {
  return CODEX_HARNESSES.has(basename(dirname(skillsDir))) ? '$' : '/';
}

function generatePinnedSkill(command, metadata, commandPrefix) {
  const desc = metadata[command]?.description || `Shortcut for ${commandPrefix}impeccable ${command}.`;
  const hint = metadata[command]?.argumentHint || '[target]';

  return `---
name: ${command}
description: "${desc}"
argument-hint: "${hint}"
user-invocable: true
---

${PIN_MARKER}

This is a pinned shortcut for \`${commandPrefix}impeccable ${command}\`.

Invoke ${commandPrefix}impeccable ${command}, passing along any arguments provided here, and follow its instructions.
`;
}

/**
 * Pin a command: create shortcut skill in all harness dirs.
 */
function pin(command, projectRoot) {
  const metadata = loadCommandMetadata();
  const harnessDirs = findHarnessDirs(projectRoot);

  if (harnessDirs.length === 0) {
    console.log('No harness directories with impeccable installed found.');
    return false;
  }

  let created = 0;

  for (const skillsDir of harnessDirs) {
    const commandPrefix = commandPrefixForSkillsDir(skillsDir);
    const content = generatePinnedSkill(command, metadata, commandPrefix);
    const skillDir = join(skillsDir, command);
    const existingMd = join(skillDir, 'SKILL.md');
    const updated = updateContainedFile(
      projectRoot,
      existingMd,
      existing => (
        existing === null || existing.includes(PIN_MARKER)
          ? content
          : undefined
      ),
      { encoding: 'utf-8', maxBytes: MAX_PINNED_SKILL_BYTES },
    );
    if (!updated) {
      console.log(`  SKIP: ${skillDir} (non-pinned skill already exists)`);
      continue;
    }
    console.log(`  + ${skillDir}`);
    created++;
  }

  if (created > 0) {
    console.log(`\nPinned '${command}' as a standalone shortcut in ${created} location(s).`);
    console.log('Use the pinned command directly in each harness.');
  }

  return created > 0;
}

/**
 * Unpin a command: remove shortcut skill from all harness dirs.
 */
function unpin(command, projectRoot) {
  const harnessDirs = findHarnessDirs(projectRoot);
  let removed = 0;

  for (const skillsDir of harnessDirs) {
    const skillDir = join(skillsDir, command);
    const skillMd = join(skillDir, 'SKILL.md');
    let found = false;
    const didRemove = removeContainedFileIf(
      projectRoot,
      skillMd,
      content => {
        found = true;
        return content.includes(PIN_MARKER);
      },
      { encoding: 'utf-8', maxBytes: MAX_PINNED_SKILL_BYTES, force: true },
    );
    if (!found) continue;
    if (!didRemove) {
      console.log(`  SKIP: ${skillDir} (not a pinned skill)`);
      continue;
    }

    console.log(`  - ${skillDir}`);
    removed++;
  }

  if (removed > 0) {
    console.log(`\nUnpinned '${command}' from ${removed} location(s).`);
    console.log(`Use Impeccable's '${command}' workflow directly to access it.`);
  } else {
    console.log(`No pinned '${command}' shortcut found.`);
  }

  return removed > 0;
}

// --- CLI ---
const [,, action, command] = process.argv;

if (!action || !command) {
  console.log('Usage: node pin.mjs <pin|unpin> <command>');
  console.log(`\nAvailable commands: ${VALID_COMMANDS.join(', ')}`);
  process.exit(1);
}

if (action !== 'pin' && action !== 'unpin') {
  console.error(`Unknown action: ${action}. Use 'pin' or 'unpin'.`);
  process.exit(1);
}

if (!VALID_COMMANDS.includes(command)) {
  console.error(`Unknown command: ${command}`);
  console.error(`Available commands: ${VALID_COMMANDS.join(', ')}`);
  process.exit(1);
}

const root = findProjectRoot();

if (action === 'pin') {
  pin(command, root);
} else {
  unpin(command, root);
}
