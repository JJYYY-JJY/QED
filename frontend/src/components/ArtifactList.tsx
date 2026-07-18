import { formatDate, shortHash, titleCase } from "../format";
import type { ArtifactRecord } from "../types";

interface ArtifactListProps {
  artifacts: ArtifactRecord[];
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ArtifactList({ artifacts }: ArtifactListProps) {
  return (
    <section className="artifacts-view">
      <header className="view-heading">
        <div>
          <h2>Reproducible artifacts</h2>
          <p>Proof, verifier report, and manifest files registered in the durable run record.</p>
        </div>
        <span>{artifacts.length} file{artifacts.length === 1 ? "" : "s"}</span>
      </header>
      {artifacts.length === 0 ? (
        <div className="view-empty">
          <h3>Export has not completed</h3>
          <p>Completed exports register content hashes and relative paths after the bundle is written atomically.</p>
        </div>
      ) : (
        <ul className="artifact-list">
          {artifacts.map((artifact) => (
            <li key={artifact.id}>
              <span className="artifact-icon" aria-hidden="true">{artifact.kind === "manifest" ? "{}" : "¶"}</span>
              <div>
                <strong>{titleCase(artifact.kind)}</strong>
                <span className="mono">{artifact.relative_path ?? artifact.id}</span>
              </div>
              <div>
                <span>{artifact.media_type}</span>
                <small>{formatBytes(artifact.size_bytes)} · {formatDate(artifact.created_at)}</small>
              </div>
              <span className="mono">sha256:{shortHash(artifact.sha256, 14)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
