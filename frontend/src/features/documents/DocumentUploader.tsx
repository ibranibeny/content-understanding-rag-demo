import { useId, useRef, useState } from "react";

import { normalizeAdvancedRange, normalizeSimpleRange } from "./pageRange";

const allowed = new Set(["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "image/png", "image/jpeg"]);
const maxBytes = 100 * 1024 * 1024;
const rangeErrorMessage = "Enter a valid finite range of up to 300 pages.";

type PageMode = "all" | "simple" | "advanced";

interface DocumentUploaderProps {
  busy: boolean;
  progress: number | null;
  onUpload: (file: File, contentRange?: string) => void;
  onError: (message: string) => void;
}

export function DocumentUploader({ busy, progress, onUpload, onError }: DocumentUploaderProps) {
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const [pendingFile, setPendingFile] = useState<File>();
  const [mode, setMode] = useState<PageMode>("all");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [advanced, setAdvanced] = useState("");
  const [rangeError, setRangeError] = useState<string>();
  const hintId = `${id}-range-hint`;
  const errorId = `${id}-range-error`;

  function resetPending() {
    setPendingFile(undefined);
    setMode("all");
    setStart("");
    setEnd("");
    setAdvanced("");
    setRangeError(undefined);
    if (input.current) input.current.value = "";
  }

  function choose(file?: File) {
    if (!file) return;
    if (!allowed.has(file.type)) {
      if (input.current) input.current.value = "";
      return onError("Choose a PDF, DOCX, PPTX, PNG, or JPEG file.");
    }
    if (file.size > maxBytes) {
      if (input.current) input.current.value = "";
      return onError("Files must be 100 MB or smaller.");
    }
    if (file.type !== "application/pdf") {
      onUpload(file, undefined);
      if (input.current) input.current.value = "";
      return;
    }
    setPendingFile(file);
    setRangeError(undefined);
  }

  function changeMode(nextMode: PageMode) {
    setMode(nextMode);
    setRangeError(undefined);
  }

  function submit() {
    if (!pendingFile) return;
    if (mode === "all") {
      onUpload(pendingFile, undefined);
      resetPending();
      return;
    }
    try {
      const contentRange = mode === "simple"
        ? normalizeSimpleRange(start, end)
        : normalizeAdvancedRange(advanced);
      setRangeError(undefined);
      onUpload(pendingFile, contentRange);
      resetPending();
    } catch {
      setRangeError(rangeErrorMessage);
    }
  }

  const describedBy = rangeError ? `${hintId} ${errorId}` : hintId;

  return <section className="uploader" aria-labelledby="upload-title">
    <div><h2 id="upload-title">Add evidence</h2><p>PDF, Office, PNG or JPEG · 100 MB max</p></div>
    <input ref={input} className="sr-only" id="document-file" aria-label="Choose document file" type="file" accept=".pdf,.docx,.pptx,.png,.jpg,.jpeg" disabled={busy} onChange={(event) => choose(event.target.files?.[0])} />
    {!pendingFile && <button type="button" className="button button--primary" disabled={busy} onClick={() => input.current?.click()}>{busy ? "Uploading…" : "Choose document"}</button>}
    {pendingFile && <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <div className="uploader__selected">
        <span><span className="eyebrow">Selected PDF</span><strong>{pendingFile.name}</strong></span>
        <button type="button" className="uploader__secondary" disabled={busy} onClick={resetPending}>Choose another file</button>
      </div>
      <fieldset className="uploader__scope" disabled={busy}>
        <legend>Page scope</legend>
        <label><input type="radio" name={`${id}-page-mode`} checked={mode === "all"} onChange={() => changeMode("all")} /> All pages</label>
        <label><input type="radio" name={`${id}-page-mode`} checked={mode === "simple"} onChange={() => changeMode("simple")} /> Start / End</label>
        <label><input type="radio" name={`${id}-page-mode`} checked={mode === "advanced"} onChange={() => changeMode("advanced")} /> Advanced</label>
      </fieldset>
      {mode === "all" && <p className="uploader__warning">PDFs over 300 pages must use a range.</p>}
      {mode === "simple" && <div className="uploader__range">
        <label>Start page<input type="number" min={1} value={start} disabled={busy} aria-invalid={rangeError ? "true" : undefined} aria-describedby={describedBy} onChange={(event) => { setStart(event.target.value); setRangeError(undefined); }} /></label>
        <label>End page<input type="number" min={1} value={end} disabled={busy} aria-invalid={rangeError ? "true" : undefined} aria-describedby={describedBy} onChange={(event) => { setEnd(event.target.value); setRangeError(undefined); }} /></label>
      </div>}
      {mode === "advanced" && <label className="uploader__advanced">Pages and ranges<input type="text" inputMode="numeric" value={advanced} disabled={busy} placeholder="1-3,5,9-12" aria-invalid={rangeError ? "true" : undefined} aria-describedby={describedBy} onChange={(event) => { setAdvanced(event.target.value); setRangeError(undefined); }} /></label>}
      {mode !== "all" && <p id={hintId} className="uploader__hint">Use 1-based pages; select no more than 300 unique pages. Example: 1-3,5.</p>}
      {rangeError && <p id={errorId} className="uploader__error" role="alert">{rangeError}</p>}
      <button type="submit" className="button button--primary uploader__submit" disabled={busy}>Upload and process</button>
    </form>}
    {progress != null && <div className="progress"><span style={{ width: `${progress}%` }} /><output aria-live="polite">{progress}%</output></div>}
  </section>;
}
