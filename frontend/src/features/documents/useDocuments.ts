import { useCallback, useEffect, useState } from "react";
import { api, ApiError, uploadBlob } from "../../api/client";
import { activeStates, type DocumentSummary, type SessionInfo } from "../../domain/types";

export function useDocuments(cancelChat: () => void) {
  const [session, setSession] = useState<SessionInfo>();
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [selected, setSelected] = useState<DocumentSummary>();
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string>();

  const refresh = useCallback(async () => {
    try {
      const [nextSession, nextDocuments] = await Promise.all([api.session(), api.documents()]);
      setSession(nextSession); setDocuments(nextDocuments);
      setSelectedId((current) => current && nextDocuments.some((item) => item.documentId === current) ? current : nextDocuments[0]?.documentId);
      setError(undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The console could not load."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  const hasActive = documents.some((item) => activeStates.has(item.state));
  useEffect(() => {
    if (!hasActive) return;
    let delay = 1000;
    let timer = window.setTimeout(tick, delay);
    function tick() {
      if (document.visibilityState === "hidden") { timer = window.setTimeout(tick, delay); return; }
      void refresh().finally(() => { delay = Math.min(delay * 2, 10_000); timer = window.setTimeout(tick, delay); });
    }
    return () => window.clearTimeout(timer);
  }, [hasActive, refresh]);

  useEffect(() => {
    if (!selectedId) { setSelected(undefined); return; }
    let live = true;
    void api.document(selectedId).then((value) => live && setSelected(value)).catch((reason: unknown) => live && setError(reason instanceof Error ? reason.message : "Document details could not load."));
    return () => { live = false; };
  }, [selectedId, documents]);

  async function upload(file: File, contentRange?: string) {
    void contentRange;
    setUploading(true); setProgress(0); setError(undefined);
    try {
      const initialized = await api.initUpload(file);
      const etag = await uploadBlob(file, initialized, setProgress);
      const completed = await api.completeUpload(initialized.documentId, etag);
      setSelectedId(completed.documentId);
      await refresh();
    } catch (reason) { setError(reason instanceof ApiError || reason instanceof Error ? reason.message : "Upload failed."); }
    finally { setUploading(false); setProgress(null); }
  }

  async function retry(id: string) { try { await api.retry(id); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Retry failed."); } }
  async function remove(id: string) {
    cancelChat();
    if (!window.confirm("Delete this document and its indexed evidence?")) return;
    try { await api.remove(id); if (selectedId === id) setSelectedId(undefined); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Delete failed."); }
  }

  return { session, documents, selected, selectedId, setSelectedId, loading, uploading, progress, error, setError, refresh, upload, retry, remove };
}
