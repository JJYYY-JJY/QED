import type { JsonValue, RunEvent, RunStage } from "./types";

export const STAGES: RunStage[] = [
  "intake",
  "literature",
  "planning",
  "proving",
  "verification",
  "adjudication",
  "export",
  "complete",
];

export function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortHash(value: string, length = 10): string {
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { notation: value >= 10_000 ? "compact" : "standard" }).format(value);
}

export function stagePosition(stage: RunStage): number {
  return STAGES.indexOf(stage);
}

const EVENT_LABELS: Record<string, string> = {
  "run.created": "Run created",
  "run.status_changed": "Run status changed",
  "run.stage_changed": "Stage advanced",
  "run.cancel_requested": "Cancellation requested",
  "run.cancelled": "Run cancelled",
  "run.resumed": "Run resumed",
  "execution.acquired": "Execution acquired",
  "execution.released": "Execution released",
  "evidence.created": "Evidence recorded",
  "evidence.batch_created": "Evidence batch sealed",
  "plan.created": "Proof plan recorded",
  "proof.attempt_reserved": "Proof attempt reserved",
  "candidate.created": "Candidate created",
  "candidate.sealed": "Candidate sealed",
  "verification.created": "Verifier report recorded",
  "adjudication.created": "Adjudication recorded",
  "candidate.decision_recorded": "Code decision recorded",
  "artifact.created": "Export artifact recorded",
  "runtime.turn_started": "Codex turn started",
  "runtime.turn_retry": "Codex turn retried",
  "runtime.token_usage": "Token usage updated",
  "runtime.item_completed": "Runtime item completed",
  "thread.created": "Research thread started",
  "thread.status_changed": "Research thread finished",
};

export function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? titleCase(eventType.replaceAll(".", " "));
}

export function eventSummary(event: RunEvent): string {
  const payload = event.payload;
  const preferred = [
    "candidate_id",
    "evidence_id",
    "plan_id",
    "verification_id",
    "artifact_id",
    "thread_id",
    "outcome",
    "to",
  ];
  for (const key of preferred) {
    const value = payload[key];
    if (typeof value === "string") return `${titleCase(key)} ${shortHash(value, 18)}`;
  }
  return titleCase(event.stage);
}

export function totalTokens(events: RunEvent[]): number {
  const perTurn = new Map<string, number>();
  for (const event of events) {
    if (event.event_type !== "runtime.token_usage") continue;
    const threadId = event.payload.thread_id;
    const turnId = event.payload.turn_id;
    const usage = event.payload.usage;
    if (typeof threadId !== "string" || typeof turnId !== "string" || !isRecord(usage)) continue;
    const input = asNumber(usage.input_tokens);
    const output = asNumber(usage.output_tokens);
    perTurn.set(JSON.stringify([threadId, turnId]), input + output);
  }
  return [...perTurn.values()].reduce((sum, value) => sum + value, 0);
}

function isRecord(value: JsonValue | undefined): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asNumber(value: JsonValue | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
