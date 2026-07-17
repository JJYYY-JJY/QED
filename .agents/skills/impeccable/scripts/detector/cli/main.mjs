import fs from 'node:fs';
import path from 'node:path';

import { loadDesignSystemForCwd } from '../design-system.mjs';
import { RULE_SCOPES, filterByScopes } from '../registry/antipatterns.mjs';
import { createBrowserDetector, detectUrl } from '../engines/browser/detect-url.mjs';
import { detectHtml } from '../engines/static-html/detect-html.mjs';
import { detectText } from '../engines/regex/detect-text.mjs';
import {
  filterDetectionFindings,
  readDetectionConfig,
  shouldIgnoreDetectionFile,
} from '../../lib/impeccable-config.mjs';
import {
  readContainedFile,
  resolveContainedPath,
  walkContainedFiles,
} from '../../lib/safe-fs.mjs';
import {
  HTML_EXTENSIONS,
  SCANNABLE_EXTENSIONS,
  SKIP_DIRS,
  buildImportGraph,
  detectFrameworkConfig,
  isPortListening,
} from '../node/file-system.mjs';

const MAX_SCAN_FILE_BYTES = 1024 * 1024;
const MAX_SCAN_TREE_BYTES = 64 * 1024 * 1024;
const MAX_SCAN_TREE_ITEMS = 20_000;
const MAX_SCAN_TREE_DEPTH = 32;
const EXTRA_SKIP_DIRS = ['.venv', 'venv', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'coverage'];

// ---------------------------------------------------------------------------
// Output formatting
// ---------------------------------------------------------------------------

const TERMINAL_CONTROL_BYTES = /[\u0000-\u001f\u007f-\u009f]/gu;

function sanitizeTerminalValue(value) {
  return String(value).replace(
    TERMINAL_CONTROL_BYTES,
    character => `\\u${character.codePointAt(0).toString(16).padStart(4, '0')}`,
  );
}

function formatFindingSummary(count) {
  return `${sanitizeTerminalValue(count)} anti-pattern${count === 1 ? '' : 's'} found.`;
}

function formatFindings(findings, jsonMode) {
  if (jsonMode) {
    return JSON.stringify(findings, null, 2).replace(
      /[\u007f-\u009f]/gu,
      character => `\\u${character.codePointAt(0).toString(16).padStart(4, '0')}`,
    );
  }

  const grouped = {};
  for (const f of findings) {
    if (!grouped[f.file]) grouped[f.file] = [];
    grouped[f.file].push(f);
  }
  const out = [];
  for (const [file, items] of Object.entries(grouped)) {
    const importNote = items[0]?.importedBy?.length
      ? ` (imported by ${items[0].importedBy.map(sanitizeTerminalValue).join(', ')})`
      : '';
    out.push(`\n${sanitizeTerminalValue(file)}${importNote}`);
    for (const item of items) {
      const location = item.line ? `line ${sanitizeTerminalValue(item.line)}: ` : '';
      out.push(
        `  ${location}[${sanitizeTerminalValue(item.antipattern)}] ${sanitizeTerminalValue(item.snippet)}`,
      );
      out.push(`    → ${sanitizeTerminalValue(item.description)}`);
    }
  }
  out.push(`\n${formatFindingSummary(findings.length)}`);
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// Stdin handling
// ---------------------------------------------------------------------------

async function handleStdin(options = {}) {
  const projectRoot = path.resolve(options.projectRoot || process.cwd());
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = Buffer.concat(chunks).toString('utf-8');
  try {
    const parsed = JSON.parse(input);
    const fp = parsed?.tool_input?.file_path;
    if (typeof fp === 'string' && fp) {
      try {
        const resolved = resolveContainedPath(
          projectRoot,
          path.isAbsolute(fp) ? fp : path.resolve(projectRoot, fp),
          { allowMissing: false, type: 'file' },
        );
        const scanOptions = { ...options, projectRoot };
        if (HTML_EXTENSIONS.has(path.extname(resolved).toLowerCase())) {
          return await detectHtml(resolved, scanOptions);
        }
        const content = readContainedFile(
          projectRoot,
          resolved,
          'utf-8',
          { maxBytes: MAX_SCAN_FILE_BYTES },
        );
        return detectText(content, resolved, scanOptions);
      } catch {
        return [];
      }
    }
  } catch { /* not JSON */ }
  return detectText(input, '<stdin>', options);
}


// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function confirm(question) {
  const rl = (await import('node:readline')).default.createInterface({
    input: process.stdin, output: process.stderr,
  });
  return new Promise((resolve) => {
    rl.question(`${sanitizeTerminalValue(question)} [Y/n] `, (answer) => {
      rl.close();
      resolve(!answer || /^y(es)?$/i.test(answer.trim()));
    });
  });
}

function printUsage() {
  console.log(`Usage: impeccable detect [options] [file-or-dir-or-url...]

Scan files or URLs for UI anti-patterns and design quality issues.

Options:
  --json              Output results as JSON
  --quiet             In text mode, only print the final findings count
  --gpt               Also report GPT-specific provider tells (off by default)
  --gemini            Also report Gemini-specific provider tells (off by default)
  --scope <name>      Only report rules in the given design domain
                      (type, layout). Comma-separated.
  --no-config         Do not apply project config, detector ignores, inline
                      ignore comments, or DESIGN.md
  --no-inline-ignores Do not honor in-file impeccable-disable* ignore comments
  --no-design-system  Do not load local DESIGN.md / .impeccable/design.json context
  --help              Show this help message

Project config:
  Respects .impeccable/config.json and .impeccable/config.local.json detector
  settings: detector.ignoreRules, detector.ignoreFiles, detector.ignoreValues,
  and detector.designSystem.enabled.

Inline ignores:
  In-file comments waive a finding where it lives and travel with the file:
    <!-- impeccable-disable overused-font -- exported brand doc -->
    .brand { font-family: Inter } /* impeccable-disable-line overused-font */
    // impeccable-disable-next-line bounce-easing: intentional bounce
  impeccable-disable applies to the whole file; -line / -next-line are scoped.
  List one or more rule ids (comma-separated), or omit them / use * for all.

Detection modes:
  HTML files     Static HTML/CSS analysis (default, catches linked CSS)
  Non-HTML files Regex pattern matching (CSS, JSX, TSX, etc.)
  URLs           Puppeteer full browser rendering (auto-detected)

Examples:
  impeccable detect src/
  impeccable detect index.html
  impeccable detect https://example.com
  impeccable detect --json .
  impeccable detect --no-config src/`);
}

async function detectCli() {
  const projectRoot = path.resolve(process.cwd());
  let args = process.argv.slice(2).map(arg => {
    if (arg === '-json') return '--json';
    if (arg === '-fast') return '--fast';
    return arg;
  });
  if (args[0] === 'detect') args = args.slice(1);
  const jsonMode = args.includes('--json');
  const quietMode = args.includes('--quiet');
  const helpMode = args.includes('--help');
  // --fast (regex-only) is deprecated: since the jsdom removal, the static
  // HTML/CSS analysis is fast and covers every rule, so the regex-only path
  // only loses coverage for no real speed win. Accept the flag for back-compat
  // but ignore it and run the full scan.
  if (args.includes('--fast')) {
    process.stderr.write(
      'Note: --fast is deprecated and ignored. The full scan is fast now and runs every rule.\n',
    );
  }
  const configEnabled = !args.includes('--no-config');
  const detectionConfig = configEnabled
    ? readDetectionConfig(projectRoot)
    : { ignoreRules: [], ignoreFiles: [], ignoreValues: [] };
  const providers = [];
  if (args.includes('--gpt')) providers.push('gpt');
  if (args.includes('--gemini')) providers.push('gemini');
  const scopes = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] !== '--scope' && !args[i].startsWith('--scope=')) continue;
    const inline = args[i].startsWith('--scope=');
    const value = inline ? args[i].slice('--scope='.length) : args[i + 1];
    const parsed = (value && !value.startsWith('--'))
      ? value.split(',').map(s => s.trim()).filter(Boolean)
      : [];
    // A bare `--scope` would otherwise fall out of `targets` and scan unscoped;
    // fail loudly so a mistyped pre-scan never runs the wrong rule set.
    if (parsed.length === 0) {
      process.stderr.write(
        `Error: --scope requires a value. Valid scopes: ${[...RULE_SCOPES].join(', ')}\n`,
      );
      process.exit(1);
    }
    scopes.push(...parsed);
    args.splice(i, inline ? 1 : 2);
    i -= 1;
  }
  const unknownScopes = scopes.filter(s => !RULE_SCOPES.has(s));
  if (unknownScopes.length > 0) {
    process.stderr.write(
      `Error: unknown --scope value(s): ${unknownScopes.map(sanitizeTerminalValue).join(', ')}. Valid scopes: ${[...RULE_SCOPES].join(', ')}\n`,
    );
    process.exit(1);
  }
  const designSystemEnabled = configEnabled && !args.includes('--no-design-system') && detectionConfig.designSystem?.enabled !== false;
  const designSystem = designSystemEnabled ? loadDesignSystemForCwd(projectRoot) : null;
  // Inline `impeccable-disable*` waivers are part of the scanned file, so they
  // apply by default. `--no-config` (raw scan) and the dedicated
  // `--no-inline-ignores` both turn them off.
  const inlineIgnoresEnabled = configEnabled && !args.includes('--no-inline-ignores');
  const scanOptions = { providers, inlineIgnores: inlineIgnoresEnabled, projectRoot };
  if (designSystem) scanOptions.designSystem = designSystem;
  const targets = args.filter(a => !a.startsWith('--'));

  if (helpMode) { printUsage(); process.exit(0); }

  let allFindings = [];
  let scanIncomplete = false;

  if (!process.stdin.isTTY && targets.length === 0) {
    allFindings = await handleStdin(scanOptions);
  } else {
    const paths = targets.length > 0 ? targets : [projectRoot];
    const urlTargetCount = paths.filter(target => /^https?:\/\//i.test(target)).length;
    const browserDetector = urlTargetCount > 1 ? await createBrowserDetector() : null;

    try {
      for (const target of paths) {
        if (/^https?:\/\//i.test(target)) {
          try {
            const scanner = browserDetector
              ? (url) => browserDetector.detectUrl(url, scanOptions)
              : (url) => detectUrl(url, scanOptions);
            allFindings.push(...await scanner(target));
          } catch (e) {
            process.stderr.write(`Error: ${sanitizeTerminalValue(e?.message ?? e)}\n`);
          }
          continue;
        }

        const requested = path.resolve(projectRoot, target);
        let resolved;
        let stat;
        try {
          resolved = resolveContainedPath(
            projectRoot,
            requested,
            { allowRoot: true, allowMissing: false },
          );
          stat = fs.lstatSync(resolved);
          if (stat.isSymbolicLink()) throw new Error(`Path contains a symbolic link: ${resolved}`);
          if (!stat.isDirectory() && !stat.isFile()) {
            throw new Error(`Path is not a regular file or directory: ${resolved}`);
          }
          resolveContainedPath(projectRoot, resolved, {
            allowRoot: true,
            allowMissing: false,
            type: stat.isDirectory() ? 'directory' : 'file',
          });
        } catch {
          process.stderr.write(`Warning: cannot access ${sanitizeTerminalValue(target)}\n`);
          continue;
        }

        if (stat.isDirectory()) {
          // Check for framework dev server config (skip in JSON/quiet modes to avoid polluting output)
          if (!jsonMode && !quietMode) {
            const fwConfig = detectFrameworkConfig(resolved);
            if (fwConfig) {
              const probe = await isPortListening(fwConfig.port, fwConfig.fingerprint);
              const frameworkName = sanitizeTerminalValue(fwConfig.name);
              const frameworkPort = sanitizeTerminalValue(fwConfig.port);
              const configName = sanitizeTerminalValue(path.basename(fwConfig.configPath));
              if (probe.listening && probe.matched) {
                process.stderr.write(
                  `\n${frameworkName} dev server detected on localhost:${frameworkPort}.\n` +
                  `For more accurate results, scan the running site:\n` +
                  `  npx impeccable detect http://localhost:${frameworkPort}\n\n`
                );
              } else if (probe.listening && !probe.matched) {
                process.stderr.write(
                  `\n${frameworkName} project detected (${configName}).\n` +
                  `Port ${frameworkPort} is in use by another service. Start the ${frameworkName} dev server and scan via URL for best results.\n\n`
                );
              } else {
                process.stderr.write(
                  `\n${frameworkName} project detected (${configName}).\n` +
                  `Start the dev server and scan via URL for best results:\n` +
                  `  npx impeccable detect http://localhost:${frameworkPort}\n\n`
                );
              }
            }
          }

          let files;
          try {
            files = walkContainedFiles(projectRoot, resolved, {
              maxDepth: MAX_SCAN_TREE_DEPTH,
              maxItems: MAX_SCAN_TREE_ITEMS,
              maxBytes: MAX_SCAN_TREE_BYTES,
              skipDirectories: [...SKIP_DIRS, ...EXTRA_SKIP_DIRS],
              includeExtensions: SCANNABLE_EXTENSIONS,
            })
              .filter(file => !shouldIgnoreDetectionFile(file, projectRoot, detectionConfig));
          } catch {
            process.stderr.write(`Warning: cannot safely scan ${sanitizeTerminalValue(target)}\n`);
            scanIncomplete = true;
            continue;
          }
          const htmlCount = files.filter(f => HTML_EXTENSIONS.has(path.extname(f).toLowerCase())).length;

          // Warn and confirm if scanning many files (static HTML/CSS processes each HTML file)
          if (files.length > 50 && process.stdin.isTTY && !jsonMode && !quietMode) {
            process.stderr.write(
              `\nFound ${files.length} files (${htmlCount} HTML) in ${sanitizeTerminalValue(target)}.\n` +
              `Scanning may take a while${htmlCount > 10 ? ' (static HTML/CSS processes each HTML file individually)' : ''}.\n` +
              `Target a specific subdirectory to narrow scope.\n`
            );
            const ok = await confirm('Continue?');
            if (!ok) { process.stderr.write('Aborted.\n'); process.exit(0); }
          }

          // Build import graph for multi-file awareness
          let graph;
          try {
            graph = buildImportGraph(files, {
              readFile: file => readContainedFile(
                projectRoot,
                file,
                'utf-8',
                { maxBytes: MAX_SCAN_FILE_BYTES },
              ),
            });
          } catch {
            process.stderr.write(`Warning: cannot safely scan ${sanitizeTerminalValue(target)}\n`);
            scanIncomplete = true;
            continue;
          }
          // Build reverse map: file -> set of files that import it
          const importedByMap = new Map();
          for (const [importer, imports] of graph) {
            for (const imported of imports) {
              if (!importedByMap.has(imported)) importedByMap.set(imported, new Set());
              importedByMap.get(imported).add(importer);
            }
          }

          for (const file of files) {
            const ext = path.extname(file).toLowerCase();
            let fileFindings;
            try {
              if (HTML_EXTENSIONS.has(ext)) {
                fileFindings = await detectHtml(file, scanOptions);
              } else {
                fileFindings = detectText(
                  readContainedFile(
                    projectRoot,
                    file,
                    'utf-8',
                    { maxBytes: MAX_SCAN_FILE_BYTES },
                  ),
                  file,
                  scanOptions,
                );
              }
            } catch {
              process.stderr.write(`Warning: cannot safely scan ${sanitizeTerminalValue(file)}\n`);
              continue;
            }
            // Annotate findings with import context
            const importers = importedByMap.get(file);
            if (importers && importers.size > 0) {
              const importerNames = [...importers].map(f => path.basename(f));
              for (const f of fileFindings) {
                f.importedBy = importerNames;
              }
            }
            allFindings.push(...fileFindings);
          }
        } else if (stat.isFile()) {
          if (shouldIgnoreDetectionFile(resolved, projectRoot, detectionConfig)) continue;
          const ext = path.extname(resolved).toLowerCase();
          if (HTML_EXTENSIONS.has(ext)) {
            try {
              allFindings.push(...await detectHtml(resolved, scanOptions));
            } catch {
              process.stderr.write(`Warning: cannot safely scan ${sanitizeTerminalValue(target)}\n`);
            }
          } else {
            try {
              const content = readContainedFile(
                projectRoot,
                resolved,
                'utf-8',
                { maxBytes: MAX_SCAN_FILE_BYTES },
              );
              allFindings.push(...detectText(content, resolved, scanOptions));
            } catch {
              process.stderr.write(`Warning: cannot safely scan ${sanitizeTerminalValue(target)}\n`);
            }
          }
        }
      }
    } finally {
      if (browserDetector) await browserDetector.close();
    }
  }

  allFindings = filterDetectionFindings(allFindings, detectionConfig);
  allFindings = filterByScopes(allFindings, scopes);

  if (allFindings.length > 0) {
    if (jsonMode) process.stdout.write(formatFindings(allFindings, true) + '\n');
    else if (quietMode) process.stderr.write(formatFindingSummary(allFindings.length) + '\n');
    else process.stderr.write(formatFindings(allFindings, false) + '\n');
    process.exit(2);
  }
  if (scanIncomplete) process.exit(1);
  if (jsonMode) process.stdout.write('[]\n');
  process.exit(0);
}

export {
  sanitizeTerminalValue,
  formatFindings,
  handleStdin,
  confirm,
  printUsage,
  detectCli,
};
