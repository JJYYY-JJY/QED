import { useMemo, useState } from "react";

import { eventLabel, eventSummary, formatDate, titleCase } from "../format";
import type { RunEvent, ThreadRecord } from "../types";
import { StatusBadge } from "./StatusBadge";

interface ActivityTimelineProps {
  events: RunEvent[];
  threads: ThreadRecord[];
}

export function ActivityTimeline({ events, threads }: ActivityTimelineProps) {
  const [filter, setFilter] = useState("all");
  const visibleEvents = useMemo(() => {
    const filtered = filter === "all" ? events : events.filter((event) => event.stage === filter);
    return filtered.slice(-250).reverse();
  }, [events, filter]);

  return (
    <section className="activity-view">
      <header className="view-heading">
        <div>
          <h2>Activity and threads</h2>
          <p>Store-assigned event sequence and independent Codex thread lifecycle.</p>
        </div>
        <span>{events.length} durable event{events.length === 1 ? "" : "s"}</span>
      </header>

      <div className="thread-ledger" aria-label="Agent thread graph">
        <div className="section-row-heading">
          <h3>Research threads</h3>
          <span>{threads.filter((thread) => thread.parent_thread_id === null).length} fresh · {threads.filter((thread) => thread.parent_thread_id !== null).length} forked</span>
        </div>
        {threads.length === 0 ? (
          <p className="inline-empty">No Codex threads have started.</p>
        ) : (
          <ul>
            {threads.map((thread) => (
              <li key={thread.id}>
                <span className={`thread-node thread-${thread.status}`} aria-hidden="true" />
                <div>
                  <strong>{titleCase(thread.role)}</strong>
                  <span className="mono">{thread.external_thread_id ?? "External ID pending"}</span>
                </div>
                <span>{thread.parent_thread_id ? "Forked history" : "Fresh thread"}</span>
                <StatusBadge value={thread.status} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="timeline-toolbar">
        <h3>Event timeline</h3>
        <label>
          <span className="visually-hidden">Filter events by stage</span>
          <select value={filter} onChange={(event) => setFilter(event.target.value)}>
            <option value="all">All stages</option>
            <option value="literature">Literature</option>
            <option value="planning">Planning</option>
            <option value="proving">Proving</option>
            <option value="verification">Verification</option>
            <option value="adjudication">Adjudication</option>
            <option value="export">Export</option>
          </select>
        </label>
      </div>
      <ol className="event-timeline" aria-label="Run event timeline">
        {visibleEvents.map((event) => (
          <li key={event.seq}>
            <span className="event-sequence">#{event.seq}</span>
            <span className="event-pin" aria-hidden="true" />
            <div>
              <strong>{eventLabel(event.event_type)}</strong>
              <span>{eventSummary(event)}</span>
            </div>
            <span className="event-stage">{titleCase(event.stage)}</span>
            <time dateTime={event.created_at}>{formatDate(event.created_at)}</time>
          </li>
        ))}
      </ol>
      {visibleEvents.length === 0 && <p className="view-empty">No events match this stage.</p>}
      {events.length > 250 && <p className="truncation-note">Showing the latest 250 events. The durable snapshot retains all {events.length.toLocaleString()}.</p>}
    </section>
  );
}
