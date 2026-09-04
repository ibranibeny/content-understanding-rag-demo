import { MetricCard } from "../../components/MetricCard";
import type { DocumentState, DocumentSummary } from "../../domain/types";
import { ExtractionViewer } from "./ExtractionViewer";

const stages: { key: DocumentState; label: string }[] = [
  { key: "queued", label: "Queued" }, { key: "analyzing", label: "Analyze" },
  { key: "classified", label: "Classify" }, { key: "extracted", label: "Extract" },
  { key: "chunking", label: "Chunk" }, { key: "embedding", label: "Embed" },
  { key: "indexing", label: "Index" }, { key: "ready", label: "Ready" },
];

export function PipelineInspector({ document, loading }: { document?: DocumentSummary; loading?: boolean }) {
  if (loading) return <p role="status" className="empty">Loading document…</p>;
  if (!document) return <div className="inspector-empty"><span className="signal-mark">CU</span><h2>Select a document</h2><p>Pipeline stages, extracted JSON and indexing metrics appear here.</p></div>;
  const current = stages.findIndex((stage) => stage.key === document.state);
  return <>
    <header className="panel-heading"><div><span className="eyebrow">Pipeline inspector</span><h2>{document.title || document.fileName}</h2></div><span className="mono">{document.documentType || "pending type"}</span></header>
    <ol className="pipeline" aria-label="Processing pipeline">{stages.map((stage, index) => <li key={stage.key} className={index <= current ? "is-complete" : ""} aria-current={stage.key === document.state ? "step" : undefined}><span>{String(index + 1).padStart(2, "0")}</span>{stage.label}</li>)}</ol>
    {document.state === "result_cleanup_pending" && <p className="notice">Content Understanding cleanup is retrying before indexing.</p>}
    <div className="metrics metrics--five"><MetricCard label="Pages requested" value={document.contentRange || "ALL"} /><MetricCard label="Pages" value={document.pageCount ?? "—"} /><MetricCard label="Chunks" value={document.chunkCount ?? "—"} /><MetricCard label="Vector" value={document.state === "ready" ? "3,072d" : "—"} /><MetricCard label="Tokens" value={document.tokenCount ?? "—"} /></div>
    <section aria-labelledby="extraction-title"><div className="section-heading"><h3 id="extraction-title">Extracted JSON / Markdown</h3><span className="mono">safe text view</span></div><ExtractionViewer extraction={document.extraction} /></section>
  </>;
}
