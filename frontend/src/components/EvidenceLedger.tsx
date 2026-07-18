import { shortHash, titleCase } from "../format";
import type { Evidence } from "../types";
import { StatusBadge } from "./StatusBadge";

interface EvidenceLedgerProps {
  evidence: Evidence[];
  onInspect: (evidence: Evidence) => void;
}

export function EvidenceLedger({ evidence, onInspect }: EvidenceLedgerProps) {
  return (
    <section className="ledger-view">
      <header className="view-heading">
        <div>
          <h2>Evidence ledger</h2>
          <p>
            Content-addressed evidence records retained before proof and citation verification.
            A content hash records bytes; it does not establish source authenticity or citation support.
          </p>
        </div>
        <span>{evidence.length} record{evidence.length === 1 ? "" : "s"}</span>
      </header>
      {evidence.length === 0 ? (
        <div className="view-empty">
          <h3>No evidence recorded</h3>
          <p>Literature and citation threads will place submitted evidence content, identifiers, and hashes here.</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Evidence record</th>
                <th>Kind</th>
                <th>Citation</th>
                <th>Content SHA-256</th>
                <th><span className="visually-hidden">Inspect</span></th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((item) => (
                <tr key={item.id}>
                  <td data-label="Evidence record">
                    <strong>{item.title}</strong>
                    <small>{item.id}</small>
                  </td>
                  <td data-label="Kind"><StatusBadge value={titleCase(item.kind)} tone="neutral" /></td>
                  <td data-label="Citation">{item.citation ?? "No citation recorded"}</td>
                  <td data-label="Content SHA-256"><span className="mono">{shortHash(item.content_sha256, 12)}</span></td>
                  <td>
                    <button type="button" className="text-button" onClick={() => onInspect(item)}>Inspect</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
