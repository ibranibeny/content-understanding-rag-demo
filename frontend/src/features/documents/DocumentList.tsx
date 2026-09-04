import type { DocumentSummary } from "../../domain/types";
import { StatusBadge } from "../../components/StatusBadge";

export function DocumentList({ documents, selectedId, onSelect, onRetry, onDelete }: { documents: DocumentSummary[]; selectedId?: string; onSelect: (id: string) => void; onRetry: (id: string) => void; onDelete: (id: string) => void }) {
  if (!documents.length) return <p className="empty">No documents yet. Add evidence to begin the pipeline.</p>;
  return <ul className="document-list">{documents.map((document) => <li key={document.documentId} className={selectedId === document.documentId ? "is-selected" : ""}>
    <button type="button" className="document-select" aria-pressed={selectedId === document.documentId} onClick={() => onSelect(document.documentId)}><span className="document-name">{document.fileName}</span><StatusBadge state={document.state} /></button>
    <div className="document-actions">{document.state === "failed" && document.failureRetryable && <button type="button" onClick={() => onRetry(document.documentId)}>Retry</button>}<button type="button" onClick={() => onDelete(document.documentId)}>Delete</button></div>
  </li>)}</ul>;
}
