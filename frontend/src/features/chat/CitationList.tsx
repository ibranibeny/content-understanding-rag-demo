import { useState } from "react";
import type { Citation, RetrievalSource } from "../../domain/types";

export function CitationList({ citations, sources }: { citations: Citation[]; sources: RetrievalSource[] }) {
  const [open, setOpen] = useState<string>();
  if (!citations.length) return null;
  return <section className="citations" aria-labelledby="sources-title"><h3 id="sources-title">Sources</h3>{citations.map((citation) => {
    const source = sources.find((item) => item.citationId === citation.citationId);
    return <div key={citation.citationId} className="citation"><button type="button" aria-expanded={open === citation.citationId} onClick={() => setOpen(open === citation.citationId ? undefined : citation.citationId)}><strong>{citation.citationId}</strong> {citation.fileName}</button>{open === citation.citationId && <p>{citation.sourceLocator} · score {source?.searchScore?.toFixed(3) ?? "n/a"}{source?.rerankerScore != null ? ` · rerank ${source.rerankerScore.toFixed(2)}` : ""}</p>}</div>;
  })}</section>;
}
