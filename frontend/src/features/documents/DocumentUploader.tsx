import { useRef } from "react";

const allowed = new Set(["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "image/png", "image/jpeg"]);
const maxBytes = 100 * 1024 * 1024;

export function DocumentUploader({ busy, progress, onUpload, onError }: { busy: boolean; progress: number | null; onUpload: (file: File) => void; onError: (message: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  function choose(file?: File) {
    if (!file) return;
    if (!allowed.has(file.type)) return onError("Choose a PDF, DOCX, PPTX, PNG, or JPEG file.");
    if (file.size > maxBytes) return onError("Files must be 100 MB or smaller.");
    onUpload(file);
  }
  return <section className="uploader" aria-labelledby="upload-title">
    <div><h2 id="upload-title">Add evidence</h2><p>PDF, Office, PNG or JPEG · 100 MB max</p></div>
    <input ref={input} className="sr-only" id="document-file" type="file" accept=".pdf,.docx,.pptx,.png,.jpg,.jpeg" onChange={(e) => choose(e.target.files?.[0])} />
    <button type="button" className="button button--primary" disabled={busy} onClick={() => input.current?.click()}>{busy ? "Uploading…" : "Choose document"}</button>
    {progress != null && <div className="progress"><span style={{ width: `${progress}%` }} /><output aria-live="polite">{progress}%</output></div>}
  </section>;
}
