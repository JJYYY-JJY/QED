import { randomUUID } from 'node:crypto';

const HTTP_PROTOCOLS = new Set(['http:', 'https:']);

export function normalizePreviewOrigin(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2048) {
    throw new Error('preview origin must be a non-empty URL');
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('preview origin must be a valid URL');
  }
  if (!HTTP_PROTOCOLS.has(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('preview origin must use http or https without credentials');
  }
  if (parsed.origin === 'null') {
    throw new Error('opaque preview origins are not supported');
  }
  return parsed.origin;
}

function bearerCredential(req) {
  const header = req.headers.authorization;
  if (typeof header !== 'string') return null;
  const match = /^Bearer ([A-Za-z0-9._~-]+)$/.exec(header);
  return match ? match[1] : null;
}

function refererOrigin(req) {
  const value = req.headers.referer;
  if (typeof value !== 'string') return null;
  try {
    return normalizePreviewOrigin(value);
  } catch {
    return null;
  }
}

function requestOrigin(req) {
  try {
    return normalizePreviewOrigin(req.headers.origin);
  } catch {
    return null;
  }
}

export function createBrowserAuthorization({ issueCapability = randomUUID } = {}) {
  const sessionsByOrigin = new Map();
  const originsByCapability = new Map();

  function registerOrigin(value) {
    const origin = normalizePreviewOrigin(value);
    const existing = sessionsByOrigin.get(origin);
    if (existing) return { origin };
    const previousOrigin = sessionsByOrigin.keys().next().value || null;
    sessionsByOrigin.clear();
    originsByCapability.clear();
    const capability = issueCapability();
    if (typeof capability !== 'string' || capability.length < 32) {
      throw new Error('browser capability issuer returned an invalid credential');
    }
    sessionsByOrigin.set(origin, capability);
    originsByCapability.set(capability, origin);
    return { origin, replaced: previousOrigin !== null, previousOrigin };
  }

  function bootstrap(req) {
    if (req.headers['sec-fetch-dest'] !== 'script') {
      return { ok: false, status: 401, error: 'Unauthorized' };
    }
    const origin = requestOrigin(req) || refererOrigin(req);
    if (!origin) return { ok: false, status: 401, error: 'Unauthorized' };
    const capability = sessionsByOrigin.get(origin);
    if (!capability) return { ok: false, status: 403, error: 'Forbidden' };
    return { ok: true, origin, capability };
  }

  function authenticate(req) {
    const capability = bearerCredential(req);
    if (!capability) return { ok: false, status: 401, error: 'Unauthorized' };
    const expectedOrigin = originsByCapability.get(capability);
    if (!expectedOrigin) return { ok: false, status: 401, error: 'Unauthorized' };
    let requestOrigin;
    try {
      requestOrigin = normalizePreviewOrigin(req.headers.origin);
    } catch {
      return { ok: false, status: 403, error: 'Forbidden' };
    }
    if (requestOrigin !== expectedOrigin) {
      return { ok: false, status: 403, error: 'Forbidden' };
    }
    return { ok: true, origin: expectedOrigin };
  }

  function preflight(req) {
    let origin;
    try {
      origin = normalizePreviewOrigin(req.headers.origin);
    } catch {
      return { ok: false, status: 403, error: 'Forbidden' };
    }
    if (!sessionsByOrigin.has(origin)) {
      return { ok: false, status: 403, error: 'Forbidden' };
    }
    return { ok: true, origin };
  }

  function corsHeaders(origin) {
    return {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization, Content-Type',
      'Access-Control-Max-Age': '600',
      Vary: 'Origin',
    };
  }

  return {
    authenticate,
    bootstrap,
    corsHeaders,
    preflight,
    registerOrigin,
  };
}

export function hasAgentBearer(req, expectedToken) {
  return bearerCredential(req) === expectedToken;
}
