import { formatDate, shortHash, titleCase } from "../format";
import type { CandidateRecord, Evidence, RunSnapshot, VerificationRecord } from "../types";
import { useModalDrawer } from "../useModalDrawer";
import { StatusBadge } from "./StatusBadge";

export type InspectorTarget =
  | { kind: "candidate"; value: CandidateRecord }
  | { kind: "evidence"; value: Evidence }
  | { kind: "report"; value: VerificationRecord }
  | null;

interface InspectorProps {
  snapshot: RunSnapshot;
  target: InspectorTarget;
  open: boolean;
  modal: boolean;
  onClose: () => void;
  onInspectEvidence: (evidence: Evidence) => void;
}

function MetadataRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="metadata-row">
      <span>{label}</span>
      <strong className={mono ? "mono" : undefined} title={value}>{value}</strong>
    </div>
  );
}

function HashScopeNote() {
  return (
    <p className="inspector-note">
      SHA-256 provides integrity addressing only; it is not an author signature or trusted timestamp.
    </p>
  );
}

function ProvenanceBlock({ source, sourceId, model, runtime, prompt, capturedAt }: {
  source: string;
  sourceId: string | null;
  model: string | null;
  runtime: string;
  prompt: string | null;
  capturedAt: string;
}) {
  return (
    <section className="inspector-section">
      <h3>Provenance</h3>
      <MetadataRow label="Record origin" value={source} />
      <MetadataRow label="Origin ID" value={sourceId ?? "Not recorded"} mono />
      <MetadataRow label="Model" value={model ?? "Not applicable"} />
      <MetadataRow label="Runtime" value={runtime} mono />
      <MetadataRow label="Prompt" value={prompt ?? "Not applicable"} mono />
      <MetadataRow label="Captured" value={formatDate(capturedAt)} />
    </section>
  );
}

export function Inspector({ snapshot, target, open, modal, onClose, onInspectEvidence }: InspectorProps) {
  const panelRef = useModalDrawer(open, modal, onClose);
  return (
    <>
      <button
        type="button"
        className={`inspector-scrim ${open ? "is-visible" : ""}`}
        aria-label="Dismiss inspector overlay"
        aria-hidden={!open}
        tabIndex={open ? 0 : -1}
        onClick={onClose}
      />
      <aside
        ref={panelRef}
        id="research-inspector"
        className={`inspector ${open ? "is-open" : ""}`}
        aria-label="Research inspector"
        role={modal ? "dialog" : undefined}
        aria-modal={modal && open ? true : undefined}
      >
        <header className="inspector-header">
          <div>
            <span>{target ? titleCase(target.kind) : "Run"}</span>
            <strong>Inspector</strong>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close inspector" data-drawer-initial-focus>×</button>
        </header>
        <div className="inspector-body">
          {target?.kind === "candidate" && <CandidateInspector candidate={target.value} snapshot={snapshot} />}
          {target?.kind === "evidence" && <EvidenceInspector evidence={target.value} />}
          {target?.kind === "report" && (
            <ReportInspector report={target.value} snapshot={snapshot} onInspectEvidence={onInspectEvidence} />
          )}
          {!target && <RunInspector snapshot={snapshot} />}
        </div>
      </aside>
    </>
  );
}

function RunInspector({ snapshot }: { snapshot: RunSnapshot }) {
  const { run, run_input: input } = snapshot;
  return (
    <>
      <section className="inspector-lead">
        <StatusBadge value={run.status} />
        <h2>{run.id}</h2>
        <p>{input?.problem ?? "The frozen problem input is unavailable."}</p>
      </section>
      <section className="inspector-section">
        <h3>Durable identity</h3>
        <MetadataRow label="Input SHA-256" value={shortHash(run.input_sha256, 18)} mono />
        <MetadataRow label="Config SHA-256" value={shortHash(run.config_sha256, 18)} mono />
        <MetadataRow label="Execution" value={`v${run.execution_version}`} />
        <MetadataRow label="Resumes" value={String(run.resume_count)} />
        <MetadataRow label="Runtime" value={run.runtime_version} mono />
        <HashScopeNote />
      </section>
      <section className="inspector-section">
        <h3>Frozen policy</h3>
        <MetadataRow label="Model" value={run.config.model} />
        <MetadataRow label="Effort" value={titleCase(run.config.effort)} />
        <MetadataRow label="Proof attempts" value={`${run.proof_attempt_count} / ${run.config.budgets.proof_attempts}`} />
        <MetadataRow label="Candidates" value={String(run.config.parallelism.proof_candidates)} />
        <MetadataRow label="Verifiers" value={String(run.config.parallelism.verifiers)} />
        <MetadataRow label="Approval" value={titleCase(run.config.sandbox.approval)} />
      </section>
      {input && input.verification_rules.length > 0 && (
        <section className="inspector-section">
          <h3>Verification rules</h3>
          <ul className="rule-list">{input.verification_rules.map((rule) => <li key={rule}>{rule}</li>)}</ul>
        </section>
      )}
    </>
  );
}

function CandidateInspector({ candidate, snapshot }: { candidate: CandidateRecord; snapshot: RunSnapshot }) {
  const decision = snapshot.decisions.find((item) => item.candidate_id === candidate.id);
  return (
    <>
      <section className="inspector-lead">
        <StatusBadge
          value={decision ? (decision.passed ? "pass" : "fail") : "pending"}
          label={decision ? (decision.passed ? "QED policy PASS" : "QED policy NOT PASSED") : undefined}
        />
        <h2>Candidate {candidate.attempt}</h2>
        <p>Immutable proof attempt bound to {candidate.plan_id}.</p>
      </section>
      <section className="inspector-section">
        <h3>Content identity</h3>
        <MetadataRow label="Candidate ID" value={candidate.id} mono />
        <MetadataRow label="Candidate SHA-256" value={shortHash(candidate.candidate_sha256, 18)} mono />
        <MetadataRow label="Proof SHA-256" value={shortHash(candidate.proof_sha256, 18)} mono />
        <MetadataRow label="Sealed" value={candidate.sealed_at ? formatDate(candidate.sealed_at) : "Not sealed"} />
        <MetadataRow label="Evidence refs" value={String(candidate.candidate.evidence_ids.length)} />
        <HashScopeNote />
      </section>
      {decision && (
        <section className="inspector-section">
          <h3>QED policy decision</h3>
          <MetadataRow label="Result" value={decision.passed ? "QED policy PASS" : "QED policy NOT PASSED"} />
          <MetadataRow label="Required" value={decision.required_kinds.map(titleCase).join(", ")} />
          <p className="inspector-note">
            This result reflects configured thread-isolated LLM checks and code gates. It is not peer review,
            formal or Lean verification, or a guarantee of mathematical truth.
          </p>
          {decision.reasons.length > 0 && <ul className="finding-list compact">{decision.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
        </section>
      )}
      <ProvenanceBlock
        source={candidate.provenance.source}
        sourceId={candidate.provenance.source_id}
        model={candidate.provenance.model}
        runtime={candidate.provenance.runtime_version}
        prompt={candidate.provenance.prompt_version}
        capturedAt={candidate.provenance.captured_at}
      />
    </>
  );
}

function EvidenceInspector({ evidence }: { evidence: Evidence }) {
  return (
    <>
      <section className="inspector-lead">
        <StatusBadge value={evidence.kind} tone="neutral" />
        <h2>{evidence.title}</h2>
        <p>{evidence.citation ?? "No citation recorded."}</p>
      </section>
      <section className="inspector-section">
        <h3>Recorded evidence content</h3>
        <p className="evidence-content">{evidence.content}</p>
        {evidence.source_uri && (
          <a href={evidence.source_uri} target="_blank" rel="noreferrer" className="source-link">
            Open recorded URI <span aria-hidden="true">↗</span>
          </a>
        )}
      </section>
      <section className="inspector-section">
        <h3>Content identity</h3>
        <MetadataRow label="Evidence ID" value={evidence.id} mono />
        <MetadataRow label="Content SHA-256" value={shortHash(evidence.content_sha256, 18)} mono />
        <HashScopeNote />
      </section>
      <ProvenanceBlock
        source={evidence.provenance.source}
        sourceId={evidence.provenance.source_id}
        model={evidence.provenance.model}
        runtime={evidence.provenance.runtime_version}
        prompt={evidence.provenance.prompt_version}
        capturedAt={evidence.provenance.captured_at}
      />
    </>
  );
}

function ReportInspector({ report: record, snapshot, onInspectEvidence }: {
  report: VerificationRecord;
  snapshot: RunSnapshot;
  onInspectEvidence: (evidence: Evidence) => void;
}) {
  const { report } = record;
  return (
    <>
      <section className="inspector-lead">
        <StatusBadge value={report.verdict} />
        <h2>{titleCase(report.kind)} report</h2>
        <p>
          This verifier checked the exact sealed candidate hash in a fresh conversation thread.
          Fresh means conversation-state isolation, not independent model weights.
        </p>
      </section>
      <section className="inspector-section">
        <h3>Verifier identity</h3>
        <MetadataRow label="Report SHA-256" value={shortHash(record.report_sha256, 18)} mono />
        <MetadataRow label="Candidate SHA-256" value={shortHash(report.candidate_sha256, 18)} mono />
        <MetadataRow label="Local thread" value={report.verifier_thread_id} mono />
        <MetadataRow label="Codex thread" value={report.verifier_external_thread_id ?? "Missing"} mono />
        <HashScopeNote />
      </section>
      <section className="inspector-section">
        <h3>Checks</h3>
        <ul className="check-list">
          {report.checks.map((check) => (
            <li key={check.id}>
              <div><StatusBadge value={check.status} /><strong>{check.category}</strong></div>
              <p>{check.summary}</p>
              {check.proof_spans.map((span) => <span className="proof-span" key={span}>Proof: {span}</span>)}
            </li>
          ))}
        </ul>
      </section>
      {report.findings.length > 0 && (
        <section className="inspector-section">
          <h3>Proof-linked findings</h3>
          <ul className="finding-list">
            {report.findings.map((finding) => (
              <li key={finding.id}>
                <div><StatusBadge value={finding.severity} /><strong>{finding.summary}</strong></div>
                <p>{finding.detail}</p>
                {finding.proof_span && <span className="proof-span">Proof: {finding.proof_span}</span>}
                {finding.evidence_ids.map((evidenceId) => {
                  const evidence = snapshot.evidence.find((item) => item.id === evidenceId);
                  return evidence ? <button type="button" className="text-button" key={evidenceId} onClick={() => onInspectEvidence(evidence)}>{evidence.title}</button> : null;
                })}
              </li>
            ))}
          </ul>
        </section>
      )}
      <ProvenanceBlock
        source={report.provenance.source}
        sourceId={report.provenance.source_id}
        model={report.provenance.model}
        runtime={report.provenance.runtime_version}
        prompt={report.provenance.prompt_version}
        capturedAt={report.provenance.captured_at}
      />
    </>
  );
}
