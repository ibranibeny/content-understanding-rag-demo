import type { DocumentState } from "../domain/types";

export function StatusBadge({ state }: { state: DocumentState }) {
  const label = state === "result_cleanup_pending" ? "Service retrying" : state.replaceAll("_", " ");
  return <span className={`status status--${state}`} aria-label={`Status: ${label}`}>{label}</span>;
}
