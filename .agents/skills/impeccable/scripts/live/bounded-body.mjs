export class RequestBodyTooLargeError extends Error {
  constructor(maxBytes) {
    super(`request body exceeds ${maxBytes} bytes`);
    this.name = 'RequestBodyTooLargeError';
    this.code = 'REQUEST_BODY_TOO_LARGE';
  }
}

export function readBoundedBody(req, { maxBytes }) {
  if (!Number.isInteger(maxBytes) || maxBytes < 1) {
    throw new Error('maxBytes must be a positive integer');
  }
  return new Promise((resolve, reject) => {
    let body = null;
    let total = 0;
    let settled = false;

    req.on('data', (chunk) => {
      if (settled) return;
      const nextTotal = total + chunk.length;
      if (nextTotal > maxBytes) {
        settled = true;
        body = null;
        reject(new RequestBodyTooLargeError(maxBytes));
        return;
      }
      if (body === null || body.length < nextTotal) {
        let nextCapacity = body?.length || Math.min(1024, maxBytes);
        while (nextCapacity < nextTotal) {
          nextCapacity = Math.min(
            maxBytes,
            Math.max(nextTotal, nextCapacity * 2),
          );
        }
        const grown = Buffer.allocUnsafe(nextCapacity);
        if (body !== null) body.copy(grown, 0, 0, total);
        body = grown;
      }
      body.set(chunk, total);
      total = nextTotal;
    });
    req.on('end', () => {
      if (settled) return;
      settled = true;
      const text = body ? body.toString('utf-8', 0, total) : '';
      body = null;
      resolve(text);
    });
    req.on('error', (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    });
  });
}

export async function readBoundedJsonBody(req, options) {
  const body = await readBoundedBody(req, options);
  try {
    return body.length === 0 ? {} : JSON.parse(body);
  } catch {
    const error = new Error('Invalid JSON');
    error.code = 'INVALID_JSON';
    throw error;
  }
}
