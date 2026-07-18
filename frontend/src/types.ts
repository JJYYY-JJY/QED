export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type RunStatus =
  | "created"
  | "running"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "completed";

export type RunStage =
  | "intake"
  | "literature"
  | "planning"
  | "proving"
  | "verification"
  | "adjudication"
  | "export"
  | "complete";

export interface Provenance {
  source: string;
  source_id: string | null;
  model: string | null;
  runtime_version: string;
  prompt_version: string | null;
  captured_at: string;
}

export interface ParallelismPolicy {
  runs: number;
  proof_candidates: number;
  verifiers: number;
  proactive_multi_agent: boolean;
}

export interface BudgetPolicy {
  run_seconds: number;
  stage_seconds: number;
  max_tokens: number;
  proof_attempts: number;
  plan_revisions: number;
  strategy_rewrites: number;
  turn_retries: number;
}

export interface SearchPolicy {
  enabled: boolean;
  allowed_roles: ("literature" | "citation")[];
  max_queries_per_stage: number;
}

export interface QedConfig {
  schema_version: 1;
  model: string;
  effort: string;
  backend: "auto" | "sdk" | "app-server" | "exec";
  parallelism: ParallelismPolicy;
  budgets: BudgetPolicy;
  search: SearchPolicy;
  sandbox: {
    literature: "read-only";
    planner: "read-only";
    prover: "read-only";
    verifier: "read-only";
    adjudicator: "read-only";
    approval: "never";
  };
}

export interface RunInput {
  schema_version: 1;
  problem: string;
  prove_guidance: string;
  verification_rules: string[];
}

export interface RunRecord {
  id: string;
  schema_version: number;
  status: RunStatus;
  stage: RunStage;
  config: QedConfig;
  config_sha256: string;
  input_sha256: string;
  provenance: Provenance;
  provenance_sha256: string;
  runtime_version: string;
  cancellation_requested: boolean;
  resumable: boolean;
  resume_count: number;
  execution_version: number;
  proof_attempt_count: number;
  plan_revision_count: number;
  strategy_rewrite_count: number;
  created_at: string;
  updated_at: string;
}

export interface RunEvent {
  schema_version: 1;
  run_id: string;
  seq: number;
  event_type: string;
  stage: RunStage;
  payload: Record<string, JsonValue>;
  payload_sha256: string;
  created_at: string;
}

export interface ThreadRecord {
  id: string;
  run_id: string;
  role: "literature" | "planner" | "prover" | "verifier" | "adjudicator";
  parent_thread_id: string | null;
  external_thread_id: string | null;
  model: string;
  status: "active" | "completed" | "failed" | "cancelled";
  schema_version: number;
  provenance: Provenance;
  provenance_sha256: string;
  created_at: string;
  updated_at: string;
}

export interface Evidence {
  schema_version: 1 | 2;
  id: string;
  kind: "paper" | "theorem" | "computation" | "human_guidance" | "source" | "note";
  title: string;
  content: string;
  content_sha256: string;
  provenance: Provenance;
  source_uri: string | null;
  citation: string | null;
  source_trust: "legacy_untrusted" | "model_reported" | "runtime_observed" | "server_captured";
  content_trust: "legacy_untrusted" | "model_reported" | "runtime_observed" | "server_captured";
  observation_ids: string[];
  source_uri_sha256: string | null;
}

export interface PlanStep {
  id: string;
  statement: string;
  rationale: string;
  success_criteria: string[];
  dependencies: string[];
  evidence_ids: string[];
  key_step: boolean;
}

export interface Plan {
  schema_version: 1;
  id: string;
  problem_sha256: string;
  strategy: string;
  steps: PlanStep[];
  provenance: Provenance;
  created_at: string;
}

export interface ProofCandidate {
  schema_version: 1;
  id: string;
  run_id: string;
  plan_id: string;
  attempt: number;
  proof: string;
  proof_sha256: string;
  evidence_ids: string[];
  deviations: string[];
  provenance: Provenance;
  created_at: string;
}

export interface CandidateRecord {
  id: string;
  run_id: string;
  thread_id: string | null;
  plan_id: string;
  attempt: number;
  schema_version: number;
  candidate: ProofCandidate;
  candidate_sha256: string;
  proof_sha256: string;
  provenance: Provenance;
  provenance_sha256: string;
  sealed_at: string | null;
  created_at: string;
  updated_at: string;
}

export type CheckStatus = "pass" | "fail" | "uncertain";

export interface VerificationCheck {
  id: string;
  category: string;
  status: CheckStatus;
  summary: string;
  proof_spans: string[];
  evidence_ids: string[];
  rule_ids: string[];
  citation_support: CitationSupport[];
}

export interface CitationSupport {
  evidence_id: string;
  proof_span: string;
  evidence_excerpt: string;
  source_locator: string;
}

export interface Finding {
  id: string;
  check_id: string;
  severity: "info" | "minor" | "major" | "critical";
  summary: string;
  detail: string;
  proof_span: string | null;
  evidence_ids: string[];
}

export interface VerificationReport {
  schema_version: 1 | 2 | 3;
  id: string;
  candidate_id: string;
  candidate_sha256: string;
  kind: "structural" | "detailed" | "citation" | "mutation";
  checks: VerificationCheck[];
  findings: Finding[];
  verifier_thread_id: string;
  verifier_external_thread_id: string | null;
  provenance: Provenance;
  created_at: string;
  verdict: CheckStatus;
}

export interface VerificationRecord {
  id: string;
  run_id: string;
  candidate_id: string;
  thread_id: string;
  kind: VerificationReport["kind"];
  schema_version: number;
  report: VerificationReport;
  report_sha256: string;
  candidate_sha256: string;
  provenance: Provenance;
  provenance_sha256: string;
  created_at: string;
}

export interface CandidateDecision {
  schema_version: 1 | 2 | 3;
  candidate_id: string;
  candidate_sha256: string;
  passed: boolean;
  required_kinds: ("structural" | "detailed" | "citation")[];
  required_rule_ids: string[];
  rule_coverage: RuleCoverage[];
  report_ids: string[];
  reasons: string[];
}

export interface RuleCoverage {
  rule_id: string;
  report_id: string;
  check_id: string;
  status: CheckStatus;
}

export interface Adjudication {
  schema_version: 1;
  id: string;
  candidate_id: string;
  report_ids: string[];
  outcome: "accept" | "revise_proof" | "revise_plan" | "rewrite" | "abandon";
  rationale: string;
  provenance: Provenance;
  created_at: string;
}

export interface ArtifactRecord {
  id: string;
  run_id: string;
  kind: string;
  relative_path: string | null;
  media_type: string;
  sha256: string;
  size_bytes: number;
  schema_version: number;
  provenance: Provenance;
  provenance_sha256: string;
  created_at: string;
}

export interface StageOutputRecord {
  id: string;
  run_id: string;
  stage: RunStage;
  kind: string;
  schema_version: number;
  content: JsonValue;
  content_sha256: string;
  provenance: Provenance;
  provenance_sha256: string;
  created_at: string;
}

export interface ExecutionLease {
  id: string;
  run_id: string;
  worker_id: string;
  version: number;
  runtime_version: string | null;
  runtime_resolution_sha256: string | null;
  lease_expires_at: string;
  released_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunSnapshot {
  run: RunRecord;
  run_input: RunInput | null;
  events: RunEvent[];
  stage_outputs: StageOutputRecord[];
  threads: ThreadRecord[];
  candidates: CandidateRecord[];
  verifications: VerificationRecord[];
  artifacts: ArtifactRecord[];
  execution_segments: ExecutionLease[];
  evidence: Evidence[];
  plans: Plan[];
  adjudications: Adjudication[];
  decisions: CandidateDecision[];
}

export interface Capabilities {
  schema_version?: 1;
  api_version?: "v1";
  default_model: string;
  commands?: ("start" | "cancel" | "resume")[];
  event_transport?: "sse";
  authentication_required: boolean;
}

export interface CreateRunPayload {
  schema_version: 1;
  run_input: RunInput;
  config: QedConfig;
}
