import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { api, ApiError, streamRunEvents } from "./api";
import { ActivityTimeline } from "./components/ActivityTimeline";
import { ArtifactList } from "./components/ArtifactList";
import { CandidateWorkspace } from "./components/CandidateWorkspace";
import { EvidenceLedger } from "./components/EvidenceLedger";
import { Inspector, type InspectorTarget } from "./components/Inspector";
import { NewRunForm } from "./components/NewRunForm";
import { RunNavigation } from "./components/RunNavigation";
import { StageRail } from "./components/StageRail";
import { StatusBadge } from "./components/StatusBadge";
import { formatDate, formatDuration, formatNumber, runStatusLabel, shortHash, titleCase, totalTokens } from "./format";
import type { Capabilities, CreateRunPayload, RunRecord, RunSnapshot } from "./types";
import { useMediaQuery } from "./useModalDrawer";
import "./styles.css";

type WorkspaceTab = "proofs" | "evidence" | "activity" | "artifacts";

const TERMINAL = new Set(["completed", "cancelled", "failed"]);
const WORKSPACE_TABS: WorkspaceTab[] = ["proofs", "evidence", "activity", "artifacts"];

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.diagnosticId ? `${error.message} Diagnostic: ${error.diagnosticId}` : error.message;
  }
  return error instanceof Error ? error.message : "The request could not be completed.";
}

export default function App() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [tab, setTab] = useState<WorkspaceTab>("proofs");
  const [composerOpen, setComposerOpen] = useState(false);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorTarget, setInspectorTarget] = useState<InspectorTarget>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [commandPending, setCommandPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [initializationError, setInitializationError] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"offline" | "live" | "reconnecting">("offline");
  const navigationModal = useMediaQuery("(max-width: 860px)");
  const inspectorModal = useMediaQuery("(max-width: 1180px)");
  const snapshotRefreshTimer = useRef<number | null>(null);
  const streamSequence = useRef(0);
  const selectedRunIdRef = useRef<string | null>(null);

  const replaceRun = useCallback((run: RunRecord) => {
    setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
  }, []);

  const selectRunId = useCallback((runId: string | null) => {
    selectedRunIdRef.current = runId;
    setSelectedRunId(runId);
  }, []);

  const refreshSnapshot = useCallback(async (runId: string) => {
    const next = await api.snapshot(runId);
    if (selectedRunIdRef.current !== runId || next.run.id !== runId) return null;
    streamSequence.current = next.events.at(-1)?.seq ?? 0;
    setSnapshot(next);
    if (TERMINAL.has(next.run.status)) setStreamState("offline");
    replaceRun(next.run);
    setSelectedCandidateId((current) => {
      if (current && next.candidates.some((candidate) => candidate.id === current)) return current;
      return next.candidates[0]?.id ?? null;
    });
    return next;
  }, [replaceRun]);

  const retryInitialization = useCallback(async () => {
    setLoading(true);
    setInitializationError(null);
    try {
      const [serverCapabilities, records] = await Promise.all([api.capabilities(), api.listRuns()]);
      setCapabilities(serverCapabilities);
      setRuns(records);
      const current = selectedRunIdRef.current;
      selectRunId(records.some((run) => run.id === current) ? current : records[0]?.id ?? null);
    } catch (caught) {
      setInitializationError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [selectRunId]);

  useEffect(() => {
    let active = true;
    void Promise.all([api.capabilities(), api.listRuns()])
      .then(([serverCapabilities, records]) => {
        if (!active) return;
        setCapabilities(serverCapabilities);
        setRuns(records);
        selectRunId(records[0]?.id ?? null);
      })
      .catch((caught: unknown) => {
        if (active) setInitializationError(errorMessage(caught));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [selectRunId]);

  useEffect(() => {
    if (!selectedRunId || composerOpen) return;
    let active = true;
    const loadSnapshot = async () => {
      setSnapshotError(null);
      try {
        await refreshSnapshot(selectedRunId);
      } catch (caught) {
        if (active) setSnapshotError(errorMessage(caught));
      }
    };
    void loadSnapshot();
    return () => { active = false; };
  }, [composerOpen, refreshSnapshot, selectedRunId]);

  useEffect(() => {
    const streamRunId = snapshot?.run.id;
    if (!streamRunId || streamRunId !== selectedRunId || TERMINAL.has(snapshot.run.status)) return;
    const controller = new AbortController();
    let cursor = streamSequence.current;
    let stopped = false;
    const follow = async () => {
      while (!controller.signal.aborted && !stopped && selectedRunIdRef.current === streamRunId) {
        try {
          setStreamState("live");
          cursor = await streamRunEvents(streamRunId, cursor, controller.signal, (event) => {
            if (controller.signal.aborted || selectedRunIdRef.current !== streamRunId) return;
            streamSequence.current = event.seq;
            setSnapshot((current) => {
              if (!current || current.run.id !== event.run_id || (current.events.at(-1)?.seq ?? 0) >= event.seq) return current;
              return { ...current, events: [...current.events, event] };
            });
            if (snapshotRefreshTimer.current === null) {
              snapshotRefreshTimer.current = window.setTimeout(() => {
                snapshotRefreshTimer.current = null;
                void refreshSnapshot(streamRunId).catch((caught: unknown) => {
                  if (selectedRunIdRef.current === streamRunId) setError(errorMessage(caught));
                });
              }, 300);
            }
          });
          if (controller.signal.aborted || selectedRunIdRef.current !== streamRunId) return;
          const latest = await refreshSnapshot(streamRunId);
          if (!latest || controller.signal.aborted || selectedRunIdRef.current !== streamRunId) return;
          if (TERMINAL.has(latest.run.status)) stopped = true;
          else await new Promise((resolve) => window.setTimeout(resolve, 800));
        } catch {
          if (controller.signal.aborted || selectedRunIdRef.current !== streamRunId) return;
          setStreamState("reconnecting");
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
      }
    };
    void follow();
    return () => {
      controller.abort();
      if (snapshotRefreshTimer.current !== null) {
        window.clearTimeout(snapshotRefreshTimer.current);
        snapshotRefreshTimer.current = null;
      }
    };
  }, [refreshSnapshot, selectedRunId, snapshot?.run.id, snapshot?.run.status]);

  const selectRun = (runId: string) => {
    selectRunId(runId);
    setComposerOpen(false);
    setSnapshot(null);
    setInspectorTarget(null);
    setSelectedCandidateId(null);
    setError(null);
    setSnapshotError(null);
    setStreamState("offline");
    setTab("proofs");
    setNavigationOpen(false);
  };

  const createRun = async (payload: CreateRunPayload) => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createRun(payload);
      replaceRun(created);
      setSnapshot(null);
      setInspectorTarget(null);
      selectRunId(created.id);
      setComposerOpen(false);
      setTab("proofs");
      await api.command(created.id, "start");
      await refreshSnapshot(created.id);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const runCommand = async (command: "start" | "cancel" | "resume") => {
    if (!selectedRunId) return;
    setCommandPending(true);
    setError(null);
    try {
      await api.command(selectedRunId, command);
      window.setTimeout(() => { void refreshSnapshot(selectedRunId); }, 250);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setCommandPending(false);
    }
  };

  const inspect = (target: InspectorTarget) => {
    setInspectorTarget(target);
    setInspectorOpen(true);
  };

  if (loading) return <LoadingShell />;
  if (initializationError) {
    return (
      <LoadFailure
        title="Research console unavailable"
        message={initializationError}
        actionLabel="Retry loading console"
        onRetry={() => { void retryInitialization(); }}
      />
    );
  }

  const selectedRun = runs.find((run) => run.id === selectedRunId) ?? null;
  const showComposer = composerOpen;

  return (
    <div className="app-shell">
      <RunNavigation
        runs={runs}
        selectedRunId={selectedRunId}
        open={navigationOpen}
        modal={navigationModal}
        onClose={() => setNavigationOpen(false)}
        onSelect={selectRun}
        onNew={() => { setComposerOpen(true); setNavigationOpen(false); }}
      />

      <div className={`app-content ${showComposer || !snapshot ? "without-inspector" : ""}`}>
        {error && (
          <div className="error-banner" role="alert">
            <span aria-hidden="true">!</span>
            <p>{error}</p>
            <button type="button" onClick={() => setError(null)} aria-label="Dismiss error">×</button>
          </div>
        )}

        {showComposer ? (
          <NewRunForm
            defaultModel={capabilities?.default_model ?? "gpt-5.6-sol"}
            submitting={submitting}
            onCancel={() => setComposerOpen(false)}
            onSubmit={createRun}
          />
        ) : snapshot && selectedRun ? (
          <RunWorkspace
            snapshot={snapshot}
            tab={tab}
            streamState={streamState}
            selectedCandidateId={selectedCandidateId}
            commandPending={commandPending}
            navigationOpen={navigationOpen}
            inspectorOpen={inspectorOpen}
            onMenu={() => setNavigationOpen(true)}
            onTab={setTab}
            onCommand={(command) => { void runCommand(command); }}
            onInspect={() => { setInspectorTarget(null); setInspectorOpen(true); }}
            onSelectCandidate={setSelectedCandidateId}
            onInspectCandidate={(candidate) => inspect({ kind: "candidate", value: candidate })}
            onInspectReport={(report) => inspect({ kind: "report", value: report })}
            onInspectEvidence={(evidence) => inspect({ kind: "evidence", value: evidence })}
          />
        ) : snapshotError && selectedRunId ? (
          <RunLoadFailure
            message={snapshotError}
            onMenu={() => setNavigationOpen(true)}
            onRetry={() => {
              setSnapshotError(null);
              void refreshSnapshot(selectedRunId).catch((caught: unknown) => setSnapshotError(errorMessage(caught)));
            }}
          />
        ) : selectedRunId ? (
          <SnapshotSkeleton navigationOpen={navigationOpen} onMenu={() => setNavigationOpen(true)} />
        ) : (
          <EmptyWorkspace navigationOpen={navigationOpen} onMenu={() => setNavigationOpen(true)} onNew={() => setComposerOpen(true)} />
        )}
      </div>

      {snapshot && !showComposer && (
        <Inspector
          snapshot={snapshot}
          target={inspectorTarget}
          open={inspectorOpen}
          modal={inspectorModal}
          onClose={() => setInspectorOpen(false)}
          onInspectEvidence={(evidence) => inspect({ kind: "evidence", value: evidence })}
        />
      )}
    </div>
  );
}

interface RunWorkspaceProps {
  snapshot: RunSnapshot;
  tab: WorkspaceTab;
  streamState: "offline" | "live" | "reconnecting";
  selectedCandidateId: string | null;
  commandPending: boolean;
  navigationOpen: boolean;
  inspectorOpen: boolean;
  onMenu: () => void;
  onTab: (tab: WorkspaceTab) => void;
  onCommand: (command: "start" | "cancel" | "resume") => void;
  onInspect: () => void;
  onSelectCandidate: (candidateId: string) => void;
  onInspectCandidate: Parameters<typeof CandidateWorkspace>[0]["onInspectCandidate"];
  onInspectReport: Parameters<typeof CandidateWorkspace>[0]["onInspectReport"];
  onInspectEvidence: Parameters<typeof CandidateWorkspace>[0]["onInspectEvidence"];
}

function RunWorkspace(props: RunWorkspaceProps) {
  const { snapshot, tab, streamState } = props;
  const { run } = snapshot;
  const [confirmCancelFor, setConfirmCancelFor] = useState<string | null>(null);
  const confirmCancelRef = useRef<HTMLButtonElement>(null);
  const elapsed = (new Date(run.updated_at).getTime() - new Date(run.created_at).getTime()) / 1000;
  const tokens = useMemo(() => totalTokens(snapshot.events), [snapshot.events]);
  const command = run.status === "created" ? "start" : run.resumable ? "resume" : run.status === "running" ? "cancel" : null;
  const cancelConfirmationKey = `${run.id}:${run.execution_version}`;
  const confirmCancel = command === "cancel" && confirmCancelFor === cancelConfirmationKey;

  useEffect(() => {
    if (confirmCancel) confirmCancelRef.current?.focus();
  }, [confirmCancel]);

  const navigateTabs = (event: KeyboardEvent<HTMLButtonElement>, current: WorkspaceTab) => {
    const index = WORKSPACE_TABS.indexOf(current);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % WORKSPACE_TABS.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + WORKSPACE_TABS.length) % WORKSPACE_TABS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = WORKSPACE_TABS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = WORKSPACE_TABS[nextIndex];
    props.onTab(next);
    document.getElementById(`workspace-tab-${next}`)?.focus();
  };

  return (
    <main className="run-workspace">
      <header className="run-header">
        <button
          type="button"
          className="icon-button mobile-menu"
          onClick={props.onMenu}
          aria-label="Open run navigation"
          aria-controls="run-navigation"
          aria-expanded={props.navigationOpen}
        >☰</button>
        <div className="run-identity">
          <span className="run-breadcrumb">QED / Runs / <span className="mono">{shortHash(run.id, 24)}</span></span>
          <div>
            <h1>{run.id}</h1>
            <StatusBadge value={run.status} label={runStatusLabel(run.status)} />
            {run.stage === "export" && run.status !== "completed" && (
              <StatusBadge value="running" label="Export intent" tone="progress" />
            )}
            {streamState !== "offline" && (
              <span className={`stream-state stream-${streamState}`}>
                <span aria-hidden="true" /> {streamState === "live" ? "Live events" : "Reconnecting"}
              </span>
            )}
          </div>
        </div>
        <div className="run-actions">
          <button
            type="button"
            className="secondary-button inspector-trigger"
            onClick={props.onInspect}
            aria-controls="research-inspector"
            aria-expanded={props.inspectorOpen}
          >Inspect run</button>
          {command === "cancel" && confirmCancel ? (
            <div className="cancel-confirmation" role="group" aria-label="Confirm run cancellation">
              <span>Stop this attempt? Durable state remains available for a later resume.</span>
              <button type="button" className="secondary-button" onClick={() => setConfirmCancelFor(null)}>Keep running</button>
              <button
                ref={confirmCancelRef}
                type="button"
                className="danger-button"
                disabled={props.commandPending}
                onClick={() => { setConfirmCancelFor(null); props.onCommand("cancel"); }}
              >
                {props.commandPending ? "Sending…" : "Confirm cancel"}
              </button>
            </div>
          ) : command && (
            <button
              type="button"
              className={command === "cancel" ? "danger-button" : "primary-button"}
              disabled={props.commandPending}
              onClick={() => command === "cancel" ? setConfirmCancelFor(cancelConfirmationKey) : props.onCommand(command)}
            >
              {props.commandPending ? "Sending…" : titleCase(command)}
            </button>
          )}
        </div>
      </header>

      <section className="problem-context">
        <div>
          <span>Frozen problem</span>
          <p>{snapshot.run_input?.problem ?? "Problem input unavailable"}</p>
        </div>
        <button
          type="button"
          className="icon-button mobile-inspector"
          onClick={props.onInspect}
          aria-label="Open run inspector"
          aria-controls="research-inspector"
          aria-expanded={props.inspectorOpen}
        >ⓘ</button>
      </section>

      <StageRail stage={run.stage} threads={snapshot.threads} />

      <section className="metrics-row" aria-label="Run resource metrics">
        <div><span>Stage</span><strong>{titleCase(run.stage)}</strong></div>
        <div><span>Elapsed</span><strong>{formatDuration(elapsed)}</strong><small>of {formatDuration(run.config.budgets.run_seconds)}</small></div>
        <div><span>Tokens</span><strong>{formatNumber(tokens)}</strong><small>of {formatNumber(run.config.budgets.max_tokens)}</small></div>
        <div><span>Attempts</span><strong>{run.proof_attempt_count}</strong><small>of {run.config.budgets.proof_attempts}</small></div>
        <div><span>Last durable event</span><strong>#{snapshot.events.at(-1)?.seq ?? 0}</strong><small>{formatDate(run.updated_at)}</small></div>
      </section>

      <nav className="workspace-tabs" aria-label="Run workspace" role="tablist">
        {WORKSPACE_TABS.map((item) => (
          <button
            type="button"
            role="tab"
            id={`workspace-tab-${item}`}
            aria-label={item === "proofs" ? "Proofs" : titleCase(item)}
            aria-selected={tab === item}
            aria-controls={`workspace-panel-${item}`}
            tabIndex={tab === item ? 0 : -1}
            className={tab === item ? "is-selected" : ""}
            key={item}
            onClick={() => props.onTab(item)}
            onKeyDown={(event) => navigateTabs(event, item)}
          >
            {item === "proofs" ? "Proofs" : titleCase(item)}
            <span>{item === "proofs" ? snapshot.candidates.length : item === "evidence" ? snapshot.evidence.length : item === "activity" ? snapshot.events.length : snapshot.artifacts.length}</span>
          </button>
        ))}
      </nav>

      <div
        className="workspace-view"
        role="tabpanel"
        id={`workspace-panel-${tab}`}
        aria-labelledby={`workspace-tab-${tab}`}
        tabIndex={0}
      >
        {tab === "proofs" && (
          <CandidateWorkspace
            snapshot={snapshot}
            selectedCandidateId={props.selectedCandidateId}
            onSelectCandidate={props.onSelectCandidate}
            onInspectCandidate={props.onInspectCandidate}
            onInspectReport={props.onInspectReport}
            onInspectEvidence={props.onInspectEvidence}
          />
        )}
        {tab === "evidence" && <EvidenceLedger evidence={snapshot.evidence} onInspect={props.onInspectEvidence} />}
        {tab === "activity" && <ActivityTimeline events={snapshot.events} threads={snapshot.threads} />}
        {tab === "artifacts" && <ArtifactList artifacts={snapshot.artifacts} />}
      </div>
    </main>
  );
}

function LoadingShell() {
  return (
    <div className="loading-shell" role="status" aria-live="polite" aria-label="Loading research console">
      <div className="loading-nav" />
      <div className="loading-main">
        <div /><div /><div />
      </div>
    </div>
  );
}

function LoadFailure({ title, message, actionLabel, onRetry }: {
  title: string;
  message: string;
  actionLabel: string;
  onRetry: () => void;
}) {
  return (
    <main className="load-failure" role="alert">
      <div className="empty-record-mark" aria-hidden="true">QED</div>
      <h1>{title}</h1>
      <p>{message}</p>
      <button type="button" className="primary-button" onClick={onRetry}>{actionLabel}</button>
    </main>
  );
}

function RunLoadFailure({ message, onMenu, onRetry }: { message: string; onMenu: () => void; onRetry: () => void }) {
  return (
    <main className="snapshot-failure" role="alert">
      <button type="button" className="icon-button mobile-menu" onClick={onMenu} aria-label="Open run navigation">☰</button>
      <div>
        <h1>Run snapshot unavailable</h1>
        <p>{message}</p>
        <button type="button" className="primary-button" onClick={onRetry}>Retry loading run</button>
      </div>
    </main>
  );
}

function SnapshotSkeleton({ navigationOpen, onMenu }: { navigationOpen: boolean; onMenu: () => void }) {
  return (
    <main className="snapshot-skeleton">
      <button type="button" className="icon-button mobile-menu" onClick={onMenu} aria-label="Open run navigation" aria-controls="run-navigation" aria-expanded={navigationOpen}>☰</button>
      <div className="skeleton-line wide" /><div className="skeleton-line" /><div className="skeleton-panel" />
    </main>
  );
}

function EmptyWorkspace({ navigationOpen, onMenu, onNew }: { navigationOpen: boolean; onMenu: () => void; onNew: () => void }) {
  return (
    <main className="empty-workspace">
      <button type="button" className="icon-button mobile-menu" onClick={onMenu} aria-label="Open run navigation" aria-controls="run-navigation" aria-expanded={navigationOpen}>☰</button>
      <div className="empty-record-mark" aria-hidden="true">QED</div>
      <h1>Begin with a mathematical problem</h1>
      <p>Create a durable record that keeps literature, proof attempts, thread-isolated checks, and the final code decision together.</p>
      <button type="button" className="primary-button" onClick={onNew}>Start a research run</button>
    </main>
  );
}
