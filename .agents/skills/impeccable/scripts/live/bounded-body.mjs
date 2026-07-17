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
    const chunks = [];
    let total = 0;
    let settled = false;

    req.on('data', (chunk) => {
      if (settled) return;
      total += chunk.length;
      if (total > maxBytes) {
        settled = true;
        reject(new RequestBodyTooLargeError(maxBytes));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks).toString('utf-8'));
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
