import type {
  Capabilities,
  CreateRunPayload,
  RunEvent,
  RunRecord,
  RunSnapshot,
} from "./types";

const API_BASE = (import.meta.env.VITE_QED_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "";

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    diagnostic_id?: string;
  };
}

export class ApiError extends Error {
  readonly code: string;
  readonly diagnosticId: string | null;
  readonly status: number;

  constructor(message: string, options: { code: string; diagnosticId?: string; status: number }) {
    super(message);
    this.name = "ApiError";
    this.code = options.code;
    this.diagnosticId = options.diagnosticId ?? null;
    this.status = options.status;
  }
}

function headers(extra?: HeadersInit): Headers {
  const result = new Headers(extra);
  result.set("Accept", "application/json");
  return result;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: headers(init?.headers),
  });
  if (!response.ok) {
    let envelope: ErrorEnvelope = {};
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      // The transport status remains useful when an upstream proxy returns non-JSON.
    }
    const fallbackMessage = response.status === 401
      ? "This browser session is not authorized. Reconnect through the configured same-origin session proxy."
      : `Request failed (${response.status}).`;
    throw new ApiError(response.status === 401 ? fallbackMessage : (envelope.error?.message ?? fallbackMessage), {
      code: envelope.error?.code ?? "request_failed",
      diagnosticId: envelope.error?.diagnostic_id,
      status: response.status,
    });
  }
  return (await response.json()) as T;
}

export const api = {
  capabilities: () => request<Capabilities>("/api/v1/capabilities"),
  listRuns: async () => {
    const response = await request<{ items: RunRecord[] }>("/api/v1/runs?limit=100");
    return response.items;
  },
  snapshot: (runId: string) => request<RunSnapshot>(`/api/v1/runs/${encodeURIComponent(runId)}/snapshot`),
  createRun: (payload: CreateRunPayload) =>
    request<RunRecord>("/api/v1/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  command: (runId: string, command: "start" | "cancel" | "resume") =>
    request<{ accepted: true; status: string }>(
      `/api/v1/runs/${encodeURIComponent(runId)}/commands/${command}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          schema_version: 1,
          idempotency_key: `${command}-${crypto.randomUUID()}`,
        }),
      },
    ),
};

function parseEventBlock(block: string): RunEvent | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  const envelope = JSON.parse(data) as {
    run_id: string;
    sequence: number;
    occurred_at: string;
    kind: string;
    stage_id: RunEvent["stage"];
    payload: RunEvent["payload"];
  };
  return {
    schema_version: 1,
    run_id: envelope.run_id,
    seq: envelope.sequence,
    created_at: envelope.occurred_at,
    event_type: envelope.kind,
    stage: envelope.stage_id,
    payload: envelope.payload,
    payload_sha256: "",
  };
}

export async function streamRunEvents(
  runId: string,
  afterSequence: number,
  signal: AbortSignal,
  onEvent: (event: RunEvent) => void,
): Promise<number> {
  const streamHeaders = headers({ Accept: "text/event-stream" });
  if (afterSequence > 0) streamHeaders.set("Last-Event-ID", String(afterSequence));
  const response = await fetch(`${API_BASE}/api/v1/runs/${encodeURIComponent(runId)}/events`, {
    credentials: "same-origin",
    headers: streamHeaders,
    signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError(`Event stream failed (${response.status}).`, {
      code: "event_stream_failed",
      status: response.status,
    });
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let cursor = afterSequence;
  let buffer = "";
  while (!signal.aborted) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value.replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const event = parseEventBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event && event.seq > cursor) {
        cursor = event.seq;
        onEvent(event);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  return cursor;
}
