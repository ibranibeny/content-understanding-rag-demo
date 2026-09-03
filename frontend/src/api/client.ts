import type { ApiErrorBody, DocumentSummary, SessionInfo, UploadInit } from "../domain/types";

export class ApiError extends Error {
  constructor(public readonly code: string, message: string, public readonly retryable = false) { super(message); }
}

async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    try { body = await response.json() as ApiErrorBody; } catch { /* safe fallback */ }
    throw new ApiError(body?.error.code ?? "request_failed", body?.error.message ?? "The request failed.", body?.error.retryable);
  }
  return response.json() as Promise<T>;
}

export const api = {
  session: () => json<SessionInfo>("/api/session"),
  documents: () => json<DocumentSummary[]>("/api/documents"),
  document: (id: string) => json<DocumentSummary>(`/api/documents/${id}`),
  initUpload: (file: File) => json<UploadInit>("/api/uploads/init", { method: "POST", body: JSON.stringify({ fileName: file.name, contentType: file.type, sizeBytes: file.size }) }),
  completeUpload: (id: string, etag: string) => json<DocumentSummary>(`/api/uploads/${id}/complete`, { method: "POST", body: JSON.stringify({ etag }) }),
  retry: (id: string) => json<DocumentSummary>(`/api/documents/${id}/retry`, { method: "POST", body: "{}" }),
  remove: (id: string) => json<DocumentSummary>(`/api/documents/${id}`, { method: "DELETE" }),
};

export function uploadBlob(file: File, upload: UploadInit, onProgress: (percent: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", upload.uploadUrl);
    Object.entries(upload.requiredHeaders).forEach(([name, value]) => request.setRequestHeader(name, value));
    request.upload.onprogress = (event) => event.lengthComputable && onProgress(Math.round(event.loaded / event.total * 100));
    request.onerror = () => reject(new ApiError("blob_upload_failed", "The direct upload failed.", true));
    request.onload = () => {
      if (request.status < 200 || request.status >= 300) return reject(new ApiError("blob_upload_failed", "The direct upload failed.", true));
      const etag = request.getResponseHeader("ETag");
      if (!etag) return reject(new ApiError("missing_etag", "Storage did not return an upload ETag."));
      onProgress(100);
      resolve(etag);
    };
    request.send(file);
  });
}
