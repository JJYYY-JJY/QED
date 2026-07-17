import { useState, type FormEvent } from "react";

import type { CreateRunPayload, QedConfig } from "../types";

interface NewRunFormProps {
  defaultModel: string;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (payload: CreateRunPayload) => Promise<void>;
}

function defaultConfig(model: string): QedConfig {
  return {
    schema_version: 1,
    model,
    effort: "auto",
    backend: "auto",
    parallelism: { runs: 1, proof_candidates: 4, verifiers: 2, proactive_multi_agent: true },
    budgets: {
      run_seconds: 7200,
      stage_seconds: 1800,
      max_tokens: 250000,
      proof_attempts: 8,
      plan_revisions: 2,
      strategy_rewrites: 2,
      turn_retries: 2,
    },
    search: {
      enabled: true,
      allowed_roles: ["literature", "citation"],
      max_queries_per_stage: 20,
    },
    sandbox: {
      literature: "read-only",
      planner: "read-only",
      prover: "read-only",
      verifier: "read-only",
      adjudicator: "read-only",
      approval: "never",
    },
  };
}

function NumberInput({ value, min, max, onChange }: {
  value: number;
  min: number;
  max?: number;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));

  return (
    <input
      type="number"
      min={min}
      max={max}
      value={draft}
      onChange={(event) => {
        setDraft(event.target.value);
        if (Number.isFinite(event.target.valueAsNumber)) onChange(event.target.valueAsNumber);
      }}
      onBlur={() => {
        if (!Number.isFinite(Number(draft)) || draft.trim() === "") setDraft(String(value));
      }}
    />
  );
}

export function NewRunForm({ defaultModel, submitting, onCancel, onSubmit }: NewRunFormProps) {
  const [problem, setProblem] = useState("");
  const [guidance, setGuidance] = useState("");
  const [rules, setRules] = useState("");
  const [config, setConfig] = useState(() => defaultConfig(defaultModel));

  const updateBudget = (field: keyof QedConfig["budgets"], value: number) => {
    setConfig((current) => ({ ...current, budgets: { ...current.budgets, [field]: value } }));
  };
  const updateParallelism = (field: keyof QedConfig["parallelism"], value: number) => {
    setConfig((current) => ({ ...current, parallelism: { ...current.parallelism, [field]: value } }));
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const verificationRules = rules
      .split("\n")
      .map((rule) => rule.trim())
      .filter((rule, index, all) => rule.length > 0 && all.indexOf(rule) === index);
    void onSubmit({
      schema_version: 1,
      run_input: {
        schema_version: 1,
        problem: problem.trim(),
        prove_guidance: guidance.trim(),
        verification_rules: verificationRules,
      },
      config,
    });
  };

  return (
    <main className="composer-page">
      <header className="composer-header">
        <div>
          <p className="context-line">New durable research record</p>
          <h1>Define the problem</h1>
          <p>Inputs are frozen when the run starts. Later attempts add history; they do not rewrite it.</p>
        </div>
        <button type="button" className="secondary-button" onClick={onCancel}>Cancel</button>
      </header>

      <form className="research-form" onSubmit={submit}>
        <section className="form-section">
          <div className="section-heading">
            <h2>Research input</h2>
            <p>State the mathematical object and the exact claim to prove.</p>
          </div>
          <label className="field field-wide">
            <span>Problem statement</span>
            <textarea
              value={problem}
              onChange={(event) => setProblem(event.target.value)}
              placeholder="Prove that …"
              rows={9}
              required
              autoFocus
            />
          </label>
          <label className="field field-wide">
            <span>Proving guidance <small>Optional</small></span>
            <textarea
              value={guidance}
              onChange={(event) => setGuidance(event.target.value)}
              placeholder="Preferred lemmas, approaches to avoid, or known partial work"
              rows={4}
            />
          </label>
          <label className="field field-wide">
            <span>Verification rules <small>One rule per line</small></span>
            <textarea
              aria-label="Verification rules"
              value={rules}
              onChange={(event) => setRules(event.target.value)}
              placeholder={"Check the exceptional case n = 2.\nVerify every external citation."}
              rows={4}
            />
          </label>
        </section>

        <details className="config-disclosure">
          <summary>
            <span>
              <strong>Runtime and budgets</strong>
              <small>{config.model} · {config.parallelism.proof_candidates} candidates · {config.budgets.max_tokens.toLocaleString()} tokens</small>
            </span>
            <span aria-hidden="true">⌄</span>
          </summary>
          <div className="config-body">
            <div className="field-grid">
              <label className="field">
                <span>Model</span>
                <input value={config.model} onChange={(event) => setConfig({ ...config, model: event.target.value })} required />
              </label>
              <label className="field">
                <span>Effort</span>
                <input
                  list="effort-options"
                  value={config.effort}
                  onChange={(event) => setConfig({ ...config, effort: event.target.value })}
                  placeholder="auto or advertised effort"
                  required
                />
                <datalist id="effort-options"><option value="auto" /></datalist>
              </label>
              <label className="field">
                <span>Runtime backend</span>
                <select value={config.backend} onChange={(event) => setConfig({ ...config, backend: event.target.value as QedConfig["backend"] })}>
                  <option value="auto">Capability-selected</option>
                  <option value="sdk">Python SDK</option>
                  <option value="app-server">App Server</option>
                  <option value="exec">codex exec fallback</option>
                </select>
              </label>
              <label className="field">
                <span>Concurrent runs</span>
                <NumberInput value={config.parallelism.runs} min={1} max={32} onChange={(value) => updateParallelism("runs", value)} />
              </label>
              <label className="field">
                <span>Proof candidates</span>
                <NumberInput value={config.parallelism.proof_candidates} min={1} max={64} onChange={(value) => updateParallelism("proof_candidates", value)} />
              </label>
              <label className="field">
                <span>Concurrent verifiers</span>
                <NumberInput value={config.parallelism.verifiers} min={1} max={64} onChange={(value) => updateParallelism("verifiers", value)} />
              </label>
              <label className="field checkbox-field">
                <input
                  type="checkbox"
                  checked={config.parallelism.proactive_multi_agent}
                  onChange={(event) => setConfig({
                    ...config,
                    parallelism: {
                      ...config.parallelism,
                      proactive_multi_agent: event.target.checked,
                    },
                  })}
                />
                <span>Request proactive multi-agent work when the runtime supports it</span>
              </label>
              <label className="field">
                <span>Run limit · seconds</span>
                <NumberInput value={config.budgets.run_seconds} min={1} onChange={(value) => updateBudget("run_seconds", value)} />
              </label>
              <label className="field">
                <span>Stage limit · seconds</span>
                <NumberInput value={config.budgets.stage_seconds} min={1} max={config.budgets.run_seconds} onChange={(value) => updateBudget("stage_seconds", value)} />
              </label>
              <label className="field">
                <span>Token budget</span>
                <NumberInput value={config.budgets.max_tokens} min={1} onChange={(value) => updateBudget("max_tokens", value)} />
              </label>
              <label className="field">
                <span>Proof attempts</span>
                <NumberInput value={config.budgets.proof_attempts} min={1} onChange={(value) => updateBudget("proof_attempts", value)} />
              </label>
              <label className="field">
                <span>Plan revisions</span>
                <NumberInput value={config.budgets.plan_revisions} min={0} onChange={(value) => updateBudget("plan_revisions", value)} />
              </label>
              <label className="field">
                <span>Strategy rewrites</span>
                <NumberInput value={config.budgets.strategy_rewrites} min={0} onChange={(value) => updateBudget("strategy_rewrites", value)} />
              </label>
              <label className="field">
                <span>Turn retries</span>
                <NumberInput value={config.budgets.turn_retries} min={0} max={10} onChange={(value) => updateBudget("turn_retries", value)} />
              </label>
              <label className="field">
                <span>Search queries · stage</span>
                <NumberInput
                  min={1}
                  value={config.search.max_queries_per_stage}
                  onChange={(value) => setConfig({ ...config, search: { ...config.search, max_queries_per_stage: value } })}
                />
              </label>
            </div>
            <label className="check-field">
              <input
                type="checkbox"
                checked={config.search.enabled}
                onChange={(event) => setConfig({ ...config, search: { ...config.search, enabled: event.target.checked } })}
              />
              <span>
                <strong>Restricted literature search</strong>
                <small>Network is limited to literature and citation roles. Every other thread remains offline and read-only.</small>
              </span>
            </label>
            <fieldset className="policy-fieldset">
              <legend>Search roles</legend>
              {(["literature", "citation"] as const).map((role) => (
                <label className="compact-check" key={role}>
                  <input
                    type="checkbox"
                    checked={config.search.allowed_roles.includes(role)}
                    onChange={(event) => {
                      const allowedRoles = event.target.checked
                        ? [...config.search.allowed_roles, role]
                        : config.search.allowed_roles.filter((item) => item !== role);
                      setConfig({ ...config, search: { ...config.search, allowed_roles: allowedRoles } });
                    }}
                  />
                  {role === "literature" ? "Literature" : "Citation verification"}
                </label>
              ))}
            </fieldset>
            <fieldset className="policy-fieldset sandbox-fieldset">
              <legend>Sandbox policy</legend>
              <label className="field">
                <span>Literature</span>
                <select value={config.sandbox.literature} disabled><option value="read-only">Read-only</option></select>
              </label>
              <label className="field">
                <span>Planner</span>
                <select value={config.sandbox.planner} disabled><option value="read-only">Read-only</option></select>
              </label>
              <label className="field">
                <span>Prover</span>
                <select value={config.sandbox.prover} disabled><option value="read-only">Read-only</option></select>
              </label>
              <label className="field">
                <span>Verifier</span>
                <select value={config.sandbox.verifier} disabled><option value="read-only">Read-only</option></select>
              </label>
              <label className="field">
                <span>Adjudicator</span>
                <select value={config.sandbox.adjudicator} disabled><option value="read-only">Read-only</option></select>
              </label>
              <label className="field">
                <span>Approvals</span>
                <select value={config.sandbox.approval} disabled><option value="never">Never</option></select>
              </label>
            </fieldset>
          </div>
        </details>

        <footer className="form-actions">
          <span>The run begins at intake and streams durable events to this console.</span>
          <button type="submit" className="primary-button" disabled={submitting || !problem.trim()}>
            {submitting ? "Creating run…" : "Create and start"}
          </button>
        </footer>
      </form>
    </main>
  );
}
