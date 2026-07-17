import { formatDate, shortHash, titleCase } from "../format";
import type { CandidateRecord, Evidence, RunSnapshot, VerificationRecord } from "../types";
import { StatusBadge } from "./StatusBadge";

interface CandidateWorkspaceProps {
  snapshot: RunSnapshot;
  selectedCandidateId: string | null;
  onSelectCandidate: (candidateId: string) => void;
  onInspectCandidate: (candidate: CandidateRecord) => void;
  onInspectReport: (report: VerificationRecord) => void;
  onInspectEvidence: (evidence: Evidence) => void;
}

export function CandidateWorkspace({
  snapshot,
  selectedCandidateId,
  onSelectCandidate,
  onInspectCandidate,
  onInspectReport,
  onInspectEvidence,
}: CandidateWorkspaceProps) {
  const candidates = [...snapshot.candidates].sort((a, b) => a.attempt - b.attempt);
  const candidate = candidates.find((item) => item.id === selectedCandidateId) ?? candidates[0];

  if (!candidate) {
    const latestPlan = snapshot.plans.at(-1);
    return (
      <section className="proof-empty">
        <div className="empty-glyph" aria-hidden="true">∎</div>
        <h2>No sealed proof candidate yet</h2>
        <p>
          QED will preserve each completed attempt here, then attach fresh structural,
          detailed, and citation reports to the frozen proof.
        </p>
        {latestPlan && (
          <div className="pending-plan">
            <strong>Current proof strategy</strong>
            <p>{latestPlan.strategy}</p>
            <span>{latestPlan.steps.length} planned step{latestPlan.steps.length === 1 ? "" : "s"}</span>
          </div>
        )}
      </section>
    );
  }

  const reports = snapshot.verifications.filter((item) => item.candidate_id === candidate.id);
  const decision = snapshot.decisions.find((item) => item.candidate_id === candidate.id);
  const plan = snapshot.plans.find((item) => item.id === candidate.plan_id);
  const evidence = snapshot.evidence.filter((item) => candidate.candidate.evidence_ids.includes(item.id));

  return (
    <div className="candidate-workspace">
      {candidates.length > 1 && (
        <div className="candidate-switcher" aria-label="Proof candidates">
          {candidates.map((item) => {
            const itemDecision = snapshot.decisions.find((entry) => entry.candidate_id === item.id);
            return (
              <button
                type="button"
                key={item.id}
                className={item.id === candidate.id ? "is-selected" : ""}
                onClick={() => onSelectCandidate(item.id)}
              >
                <span>Candidate {item.attempt}</span>
                {itemDecision ? <StatusBadge value={itemDecision.passed ? "pass" : "fail"} /> : <span className="candidate-state">Awaiting decision</span>}
              </button>
            );
          })}
        </div>
      )}

      <header className="candidate-header">
        <div>
          <div className="candidate-title-line">
            <h2>Candidate {candidate.attempt}</h2>
            <StatusBadge value={candidate.sealed_at ? "sealed" : "draft"} />
          </div>
          <p>
            Attempt {candidate.attempt} · plan {shortHash(candidate.plan_id, 18)} · sealed {candidate.sealed_at ? formatDate(candidate.sealed_at) : "pending"}
          </p>
        </div>
        <button type="button" className="text-button" onClick={() => onInspectCandidate(candidate)}>
          Provenance and hashes
        </button>
      </header>

      <section className={`decision-strip ${decision?.passed ? "decision-pass" : decision ? "decision-fail" : "decision-pending"}`}>
        <div>
          <span className="decision-mark" aria-hidden="true">{decision?.passed ? "✓" : decision ? "×" : "·"}</span>
          <div>
            <strong>{decision ? (decision.passed ? "PASS" : "NOT PASSED") : "Decision pending"}</strong>
            <span>
              {decision
                ? `${decision.report_ids.length} independent reports · code-computed from frozen inputs`
                : "QED has not recorded the code-computed decision."}
            </span>
          </div>
        </div>
        {decision && decision.reasons.length > 0 && <span>{decision.reasons.length} blocking reason{decision.reasons.length === 1 ? "" : "s"}</span>}
      </section>

      <section className="verification-bar" aria-label="Independent verification reports">
        <div className="verification-heading">
          <h3>Independent verification</h3>
          <span>{reports.length} independent report{reports.length === 1 ? "" : "s"}</span>
        </div>
        <div className="report-buttons">
          {reports.length === 0 ? (
            <p className="inline-empty">Fresh verifier threads have not reported yet.</p>
          ) : (
            reports.map((record) => (
              <button type="button" key={record.id} onClick={() => onInspectReport(record)}>
                <span className={`report-dot report-${record.report.verdict}`} aria-hidden="true" />
                <span>
                  <strong>{titleCase(record.kind)}</strong>
                  <small>{titleCase(record.report.verdict)} · {record.report.checks.length} checks</small>
                </span>
                <span aria-hidden="true">›</span>
              </button>
            ))
          )}
        </div>
      </section>

      {plan && (
        <details className="proof-plan">
          <summary>
            <span>
              <strong>Frozen proof plan</strong>
              <small>{plan.strategy}</small>
            </span>
            <span>{plan.steps.length} step{plan.steps.length === 1 ? "" : "s"}</span>
          </summary>
          <ol>
            {plan.steps.map((step) => (
              <li key={step.id}>
                <span className="plan-step-index">{step.id}</span>
                <div>
                  <strong>{step.statement}</strong>
                  <p>{step.rationale}</p>
                  {step.key_step && <span className="key-step">Key step</span>}
                </div>
              </li>
            ))}
          </ol>
        </details>
      )}

      <article className="proof-document">
        <header>
          <div>
            <h3>Sealed proof</h3>
            <span className="mono">sha256:{shortHash(candidate.proof_sha256, 16)}</span>
          </div>
          <span>{candidate.candidate.proof.length.toLocaleString()} characters</span>
        </header>
        <div className="proof-text">{candidate.candidate.proof}</div>
      </article>

      {candidate.candidate.deviations.length > 0 && (
        <section className="deviation-section">
          <h3>Plan deviations</h3>
          <ul>{candidate.candidate.deviations.map((item) => <li key={item}>{item}</li>)}</ul>
        </section>
      )}

      <section className="cited-evidence">
        <div className="section-row-heading">
          <h3>Cited evidence</h3>
          <span>{evidence.length} source{evidence.length === 1 ? "" : "s"}</span>
        </div>
        {evidence.length === 0 ? (
          <p className="inline-empty">This candidate does not claim external evidence.</p>
        ) : (
          <ul>
            {evidence.map((item) => (
              <li key={item.id}>
                <button type="button" onClick={() => onInspectEvidence(item)}>
                  <span className="evidence-kind">{titleCase(item.kind)}</span>
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.citation ?? item.provenance.source}</small>
                  </span>
                  <span className="mono">{shortHash(item.content_sha256)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
