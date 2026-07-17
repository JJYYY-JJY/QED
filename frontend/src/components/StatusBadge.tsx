import { titleCase } from "../format";

interface StatusBadgeProps {
  value: string;
  tone?: "neutral" | "progress" | "success" | "warning" | "danger";
}

const SUCCESS = new Set(["completed", "complete", "pass", "sealed", "accept"]);
const PROGRESS = new Set(["running", "active", "cancelling"]);
const WARNING = new Set(["paused", "uncertain", "created"]);
const DANGER = new Set(["failed", "fail", "cancelled", "critical", "major", "abandon"]);

function inferredTone(value: string): NonNullable<StatusBadgeProps["tone"]> {
  if (SUCCESS.has(value)) return "success";
  if (PROGRESS.has(value)) return "progress";
  if (WARNING.has(value)) return "warning";
  if (DANGER.has(value)) return "danger";
  return "neutral";
}

export function StatusBadge({ value, tone = inferredTone(value) }: StatusBadgeProps) {
  return <span className={`status-badge status-${tone}`}>{titleCase(value)}</span>;
}
