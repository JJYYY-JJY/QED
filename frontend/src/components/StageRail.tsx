import { useEffect, useRef } from "react";

import { STAGES, stagePosition, titleCase } from "../format";
import type { RunStage, ThreadRecord } from "../types";

interface StageRailProps {
  stage: RunStage;
  threads: ThreadRecord[];
}

const STAGE_ROLES: Partial<Record<RunStage, ThreadRecord["role"][]>> = {
  literature: ["literature"],
  planning: ["planner"],
  proving: ["prover"],
  verification: ["verifier"],
  adjudication: ["adjudicator"],
};

export function StageRail({ stage, threads }: StageRailProps) {
  const current = stagePosition(stage);
  const panelRef = useRef<HTMLElement>(null);
  const currentRef = useRef<HTMLLIElement>(null);

  useEffect(() => {
    const panel = panelRef.current;
    if (panel && panel.scrollWidth > panel.clientWidth) {
      currentRef.current?.scrollIntoView({ block: "nearest", inline: "center" });
    }
  }, [stage]);

  return (
    <section ref={panelRef} className="stage-panel" aria-label="Research stage and agent graph">
      <ol className="stage-rail">
        {STAGES.map((item, index) => {
          const state = index < current ? "complete" : index === current ? "current" : "pending";
          const roles = STAGE_ROLES[item] ?? [];
          const stageThreads = threads.filter((thread) => roles.includes(thread.role));
          return (
            <li
              ref={state === "current" ? currentRef : undefined}
              key={item}
              className={`stage-step stage-${state}`}
              aria-current={state === "current" ? "step" : undefined}
            >
              <span className="stage-node" aria-hidden="true">{index < current ? "✓" : index + 1}</span>
              <span className="stage-copy">
                <strong>{titleCase(item)}</strong>
                {stageThreads.length > 0 && <small>{stageThreads.length} thread{stageThreads.length === 1 ? "" : "s"}</small>}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
