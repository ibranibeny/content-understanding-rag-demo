export function ExtractionViewer({ extraction }: { extraction: unknown }) {
  if (extraction == null) return <p className="empty">Extraction appears when processing completes.</p>;
  const text = typeof extraction === "string" ? extraction : JSON.stringify(extraction, null, 2);
  return <pre className="extraction" tabIndex={0} aria-label="Extracted document content">{text}</pre>;
}
