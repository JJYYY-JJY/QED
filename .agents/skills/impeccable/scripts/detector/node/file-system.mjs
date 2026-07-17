import fs from 'node:fs';
import path from 'node:path';

import { readContainedFile } from '../../lib/safe-fs.mjs';

// ---------------------------------------------------------------------------
// File walker
// ---------------------------------------------------------------------------

const SKIP_DIRS = new Set([
  'node_modules', '.git', 'dist', 'build', '.next', '.nuxt', '.output',
  '.svelte-kit', '__pycache__', '.turbo', '.vercel',
]);

const SCANNABLE_EXTENSIONS = new Set([
  '.html', '.htm', '.css', '.scss', '.sass', '.less',
  '.jsx', '.tsx', '.js', '.ts',
  '.vue', '.svelte', '.astro',
]);

const HTML_EXTENSIONS = new Set(['.html', '.htm']);

function walkDir(dir) {
  const files = [];
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return files; }
  for (const entry of entries) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) files.push(...walkDir(full));
    else if (entry.isFile() && SCANNABLE_EXTENSIONS.has(path.extname(entry.name).toLowerCase())) files.push(full);
  }
  return files;
}


// ---------------------------------------------------------------------------
// Import graph (multi-file awareness)
// ---------------------------------------------------------------------------

function resolveImport(specifier, fromDir, fileSet) {
  if (!/^[./]/.test(specifier)) return null; // skip bare specifiers
  const base = path.resolve(fromDir, specifier);
  if (fileSet.has(base)) return base;
  for (const ext of SCANNABLE_EXTENSIONS) {
    const withExt = base + ext;
    if (fileSet.has(withExt)) return withExt;
  }
  // index file convention
  for (const ext of SCANNABLE_EXTENSIONS) {
    const indexFile = path.join(base, 'index' + ext);
    if (fileSet.has(indexFile)) return indexFile;
  }
  return null;
}

function buildImportGraph(files, options = {}) {
  const readFile = typeof options.readFile === 'function'
    ? options.readFile
    : (file) => fs.readFileSync(file, 'utf-8');
  const fileSet = new Set(files);
  const graph = new Map();

  for (const file of files) {
    const content = readFile(file);
    const dir = path.dirname(file);
    const imports = new Set();

    // ES imports: import ... from '...' and import '...'
    const esRe = /import\s+(?:[\s\S]*?from\s+)?['"]([^'"]+)['"]/g;
    let m;
    while ((m = esRe.exec(content)) !== null) {
      const resolved = resolveImport(m[1], dir, fileSet);
      if (resolved) imports.add(resolved);
    }

    // CSS @import
    const cssRe = /@import\s+(?:url\(\s*)?['"]?([^'");\s]+)['"]?\s*\)?/g;
    while ((m = cssRe.exec(content)) !== null) {
      const resolved = resolveImport(m[1], dir, fileSet);
      if (resolved) imports.add(resolved);
    }

    // SCSS @use / @forward
    const scssRe = /@(?:use|forward)\s+['"]([^'"]+)['"]/g;
    while ((m = scssRe.exec(content)) !== null) {
      const resolved = resolveImport(m[1], dir, fileSet);
      if (resolved) imports.add(resolved);
    }

    graph.set(file, imports);
  }
  return graph;
}

// ---------------------------------------------------------------------------
// Framework dev server detection
// ---------------------------------------------------------------------------

const FRAMEWORK_CONFIGS = [
  { name: 'Next.js', files: ['next.config.js', 'next.config.mjs', 'next.config.ts'], defaultPort: 3000,
    portRe: /port\s*[:=]\s*(\d+)/ },
  { name: 'SvelteKit', files: ['svelte.config.js', 'svelte.config.ts'], defaultPort: 5173,
    portRe: /port\s*[:=]\s*(\d+)/ },
  { name: 'Nuxt', files: ['nuxt.config.js', 'nuxt.config.ts'], defaultPort: 3000,
    portRe: /port\s*[:=]\s*(\d+)/ },
  { name: 'Vite', files: ['vite.config.js', 'vite.config.ts', 'vite.config.mjs'], defaultPort: 5173,
    portRe: /port\s*[:=]\s*(\d+)/ },
  { name: 'Astro', files: ['astro.config.js', 'astro.config.ts', 'astro.config.mjs'], defaultPort: 4321,
    portRe: /port\s*[:=]\s*(\d+)/ },
  { name: 'Angular', files: ['angular.json'], defaultPort: 4200,
    portRe: /"port"\s*:\s*(\d+)/ },
  { name: 'Remix', files: ['remix.config.js', 'remix.config.ts'], defaultPort: 3000,
    portRe: /port\s*[:=]\s*(\d+)/ },
];

const MAX_FRAMEWORK_CONFIG_BYTES = 1024 * 1024;

function isValidPort(port) {
  return Number.isInteger(port) && port >= 1 && port <= 65535;
}

function detectFrameworkConfig(dir) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return null; }
  const fileNames = new Set(
    entries.filter(entry => entry.isFile()).map(entry => entry.name),
  );

  for (const cfg of FRAMEWORK_CONFIGS) {
    const match = cfg.files.find(file => fileNames.has(file));
    if (!match) continue;

    const configPath = path.join(dir, match);
    let content;
    try {
      content = readContainedFile(
        dir,
        match,
        'utf-8',
        { maxBytes: MAX_FRAMEWORK_CONFIG_BYTES },
      );
    } catch {
      continue;
    }

    let port = cfg.defaultPort;
    const portMatch = content.match(cfg.portRe);
    const configuredPort = portMatch ? Number(portMatch[1]) : null;
    if (isValidPort(configuredPort)) port = configuredPort;

    return { name: cfg.name, port, configPath };
  }
  return null;
}

export {
  SKIP_DIRS,
  SCANNABLE_EXTENSIONS,
  HTML_EXTENSIONS,
  walkDir,
  resolveImport,
  buildImportGraph,
  FRAMEWORK_CONFIGS,
  detectFrameworkConfig,
};
