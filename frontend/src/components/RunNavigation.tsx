import { formatDate } from "../format";
import type { RunRecord } from "../types";
import { useModalDrawer } from "../useModalDrawer";
import { StatusBadge } from "./StatusBadge";

interface RunNavigationProps {
  runs: RunRecord[];
  selectedRunId: string | null;
  open: boolean;
  modal: boolean;
  onClose: () => void;
  onSelect: (runId: string) => void;
  onNew: () => void;
}

export function RunNavigation({
  runs,
  selectedRunId,
  open,
  modal,
  onClose,
  onSelect,
  onNew,
}: RunNavigationProps) {
  const sorted = [...runs].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const panelRef = useModalDrawer(open, modal, onClose);
  return (
    <>
      <button
        type="button"
        className={`drawer-scrim ${open ? "is-visible" : ""}`}
        aria-label="Dismiss navigation overlay"
        aria-hidden={!open}
        tabIndex={open ? 0 : -1}
        onClick={onClose}
      />
      <aside
        ref={panelRef}
        id="run-navigation"
        className={`run-navigation ${open ? "is-open" : ""}`}
        aria-label="Research runs"
        role={modal ? "dialog" : undefined}
        aria-modal={modal && open ? true : undefined}
      >
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">Q</div>
          <div>
            <strong>QED</strong>
            <span>Research console</span>
          </div>
          <button type="button" className="icon-button nav-close" onClick={onClose} aria-label="Close navigation" data-drawer-initial-focus>
            ×
          </button>
        </div>

        <button type="button" className="primary-button new-run-button" onClick={onNew}>
          <span aria-hidden="true">＋</span>
          New research run
        </button>

        <div className="run-list-heading">
          <span>Research runs</span>
          <span>{runs.length}</span>
        </div>
        <nav className="run-list">
          {sorted.length === 0 ? (
            <div className="navigation-empty">
              <p>No runs recorded.</p>
              <span>Your first problem will appear here with its durable state.</span>
            </div>
          ) : (
            sorted.map((run) => (
              <button
                type="button"
                key={run.id}
                className={`run-list-item ${selectedRunId === run.id ? "is-selected" : ""}`}
                onClick={() => onSelect(run.id)}
              >
                <span className="run-list-title">{run.id}</span>
                <span className="run-list-meta">
                  <StatusBadge value={run.status} />
                  <time dateTime={run.updated_at}>{formatDate(run.updated_at)}</time>
                </span>
              </button>
            ))
          )}
        </nav>

        <footer className="nav-footer">
          <span className="connection-dot" aria-hidden="true" />
          Typed API · durable SQLite state
        </footer>
      </aside>
    </>
  );
}
