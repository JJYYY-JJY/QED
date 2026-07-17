import path from 'node:path';

const MAX_GLOB_CHARS = 512;
const MAX_GLOB_PATTERNS = 128;
const MAX_PATH_CHARS = 8192;
const MAX_MATCH_WORK = 10_000_000;
const MAX_BRACE_ALTERNATIVES = 32;
const MAX_RESULT_CACHE_ENTRIES = 4096;

function tokenizeGlob(pattern, matchBraces) {
  const tokens = [];
  let literal = '';
  let index = 0;

  const flushLiteral = () => {
    if (!literal) return;
    tokens.push({ kind: 'literal', value: literal });
    literal = '';
  };

  while (index < pattern.length) {
    const character = pattern[index];
    if (character === '*') {
      flushLiteral();
      const globstar = pattern[index + 1] === '*';
      const end = index + (globstar ? 2 : 1);
      if (globstar && pattern[end] === '/') {
        tokens.push({ kind: 'globstar-slash' });
        index = end + 1;
      } else {
        tokens.push({ kind: globstar ? 'globstar' : 'star' });
        index = end;
      }
      continue;
    }
    if (character === '?') {
      flushLiteral();
      tokens.push({ kind: 'single' });
      index += 1;
      continue;
    }
    if (matchBraces && character === '{') {
      const end = pattern.indexOf('}', index + 1);
      if (end !== -1) {
        const alternatives = pattern.slice(index + 1, end).split(',');
        if (alternatives.length > MAX_BRACE_ALTERNATIVES) return null;
        flushLiteral();
        tokens.push({ kind: 'alternatives', values: alternatives });
        index = end + 1;
        continue;
      }
    }
    literal += character;
    index += 1;
  }

  flushLiteral();
  return tokens;
}

function matchTokens(value, tokens) {
  let current = new Uint8Array(value.length + 1);
  let next = new Uint8Array(value.length + 1);
  current[0] = 1;

  for (const token of tokens) {
    next.fill(0);
    if (token.kind === 'literal') {
      for (let offset = 0; offset <= value.length - token.value.length; offset += 1) {
        if (current[offset] && value.startsWith(token.value, offset)) {
          next[offset + token.value.length] = 1;
        }
      }
    } else if (token.kind === 'alternatives') {
      for (let offset = 0; offset <= value.length; offset += 1) {
        if (!current[offset]) continue;
        for (const alternative of token.values) {
          if (value.startsWith(alternative, offset)) {
            next[offset + alternative.length] = 1;
          }
        }
      }
    } else if (token.kind === 'single') {
      for (let offset = 0; offset < value.length; offset += 1) {
        if (current[offset] && value[offset] !== '/') next[offset + 1] = 1;
      }
    } else if (token.kind === 'star') {
      next[0] = current[0];
      for (let offset = 1; offset <= value.length; offset += 1) {
        next[offset] = current[offset] || (
          value[offset - 1] !== '/' && next[offset - 1]
        );
      }
    } else if (token.kind === 'globstar') {
      next[0] = current[0];
      for (let offset = 1; offset <= value.length; offset += 1) {
        next[offset] = current[offset] || next[offset - 1];
      }
    } else {
      let canConsume = false;
      for (let offset = 0; offset <= value.length; offset += 1) {
        if (current[offset]) {
          next[offset] = 1;
          canConsume = true;
        }
        if (canConsume && value[offset] === '/') next[offset + 1] = 1;
      }
    }
    [current, next] = [next, current];
  }

  return current[value.length] === 1;
}

function* matchTargets(rawPath, { matchBasename, matchSuffixes }) {
  const normalized = rawPath.split(path.sep).join('/');
  yield normalized;
  if (matchSuffixes) {
    const parts = normalized.split('/').filter(Boolean);
    for (let index = 0; index < parts.length; index += 1) {
      const suffix = parts.slice(index).join('/');
      if (suffix !== normalized) yield suffix;
    }
  } else if (matchBasename) {
    const base = normalized.split('/').pop();
    if (base !== normalized) yield base;
  }
}

/**
 * Create one matcher per scan/filter operation. Glob arrays are snapshotted
 * on first use and must remain unchanged for that operation.
 */
export function createGlobMatcher({ exhaustedResult = false } = {}) {
  let remainingWork = MAX_MATCH_WORK;
  let cachedResults = 0;
  const globStates = new WeakMap();

  return function matchAnyGlob(filePath, globs, options = {}) {
    if (!Array.isArray(globs) || globs.length === 0) return false;
    const rawPath = String(filePath || '');
    if (!rawPath) return false;
    if (rawPath.length > MAX_PATH_CHARS) return exhaustedResult;

    const matchBasename = options.matchBasename !== false;
    const matchSuffixes = options.matchSuffixes === true;
    const matchBraces = options.matchBraces !== false;
    let state = globStates.get(globs);
    if (!state) {
      state = {
        invalid: globs.length > MAX_GLOB_PATTERNS,
        patterns: [],
        tokensByBraceMode: new Map(),
        results: new Map(),
      };
      if (!state.invalid) {
        for (const glob of globs) {
          let pattern;
          try {
            pattern = String(glob);
          } catch {
            state.invalid = true;
            break;
          }
          if (pattern.length > MAX_GLOB_CHARS) {
            state.invalid = true;
            break;
          }
          state.patterns.push(pattern);
        }
      }
      globStates.set(globs, state);
    }
    if (state.invalid) return exhaustedResult;

    const braceMode = matchBraces ? 'expanded' : 'literal';
    let compiled = state.tokensByBraceMode.get(braceMode);
    if (!state.tokensByBraceMode.has(braceMode)) {
      compiled = [];
      for (const pattern of state.patterns) {
        if (!pattern) continue;
        const tokens = tokenizeGlob(pattern, matchBraces);
        if (tokens === null) {
          compiled = null;
          break;
        }
        compiled.push({ pattern, tokens });
      }
      state.tokensByBraceMode.set(braceMode, compiled);
    }
    if (compiled === null) return exhaustedResult;

    const cacheKey = `${matchBasename ? 1 : 0}:${matchSuffixes ? 1 : 0}:${braceMode}:${rawPath}`;
    if (state.results.has(cacheKey)) return state.results.get(cacheKey);

    const finish = (result) => {
      if (cachedResults < MAX_RESULT_CACHE_ENTRIES) {
        state.results.set(cacheKey, result);
        cachedResults += 1;
      }
      return result;
    };
    if (remainingWork <= 0) return exhaustedResult;

    for (const { pattern, tokens } of compiled) {
      for (const target of matchTargets(rawPath, { matchBasename, matchSuffixes })) {
        const work = (pattern.length + 1) * (target.length + 1);
        if (work > remainingWork) {
          remainingWork = 0;
          return exhaustedResult;
        }
        remainingWork -= work;
        if (matchTokens(target, tokens)) return finish(true);
      }
    }
    return finish(false);
  };
}

export function matchesAnyGlob(filePath, globs, options) {
  return createGlobMatcher()(filePath, globs, options);
}
