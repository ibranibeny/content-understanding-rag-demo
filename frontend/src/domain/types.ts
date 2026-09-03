export type DocumentState =
  | "awaiting_upload" | "queued" | "analyzing" | "classified" | "extracted"
  | "result_cleanup_pending" | "chunking" | "embedding" | "indexing"
  | "ready" | "deleting" | "deleted" | "failed";

export interface SessionInfo {
  expiresAt: string;
  documentsUsed: number;
  documentLimit: number;
  bytesUsed: number;
  byteLimit: number;
  questionsUsed: number;
  questionLimit: number;
}

export interface DocumentSummary {
  documentId: string;
  fileName: string;
  state: DocumentState;
  documentType?: string | null;
  title?: string | null;
  pageCount?: number | null;
  chunkCount?: number | null;
  tokenCount?: number | null;
  extraction?: unknown;
  failureCode?: string | null;
  failureRetryable: boolean;
  retryCount: number;
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
}

export interface UploadInit {
  uploadUrl: string;
  documentId: string;
  expiresAt: string;
  requiredHeaders: Record<string, string>;
}

export interface ApiErrorBody {
  error: { code: string; message: string; retryable: boolean; correlationId: string };
}

export interface ChatTurn { role: "user" | "assistant"; content: string }
export interface RetrievalSource {
  citationId: string;
  documentId: string;
  fileName: string;
  sourceLocator: string;
  searchScore?: number | null;
  rerankerScore?: number | null;
}
export interface Citation { citationId: string; documentId: string; fileName: string; sourceLocator: string }
export type ChatEvent =
  | { type: "retrieval"; sources: RetrievalSource[]; latencyMs: number; correlationId?: string }
  | { type: "token"; text: string; correlationId?: string }
  | { type: "citation"; citation: Citation; correlationId?: string }
  | { type: "done"; inputTokens: number; outputTokens: number; totalLatencyMs: number; correlationId?: string }
  | { type: "error"; code: string; retryable: boolean; correlationId?: string };

export const activeStates = new Set<DocumentState>([
  "awaiting_upload", "queued", "analyzing", "classified", "extracted",
  "result_cleanup_pending", "chunking", "embedding", "indexing", "deleting",
]);
