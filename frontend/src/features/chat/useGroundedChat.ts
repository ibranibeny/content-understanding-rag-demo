import { useCallback, useRef, useState } from "react";
import { parseSseStream, streamChat } from "../../api/sse";
import type { ChatTurn, Citation, RetrievalSource } from "../../domain/types";

const storageKey = "cu-chat-history";
function initialHistory(): ChatTurn[] {
  try { const parsed = JSON.parse(sessionStorage.getItem(storageKey) ?? "[]") as ChatTurn[]; return parsed.slice(-6); }
  catch { return []; }
}

export function useGroundedChat() {
  const [history, setHistory] = useState<ChatTurn[]>(initialHistory);
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<RetrievalSource[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [diagnostics, setDiagnostics] = useState<{ retrieval?: number; total?: number; tokens?: number; correlationId?: string }>({});
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string>();
  const controller = useRef<AbortController | undefined>(undefined);

  const cancel = useCallback(() => { controller.current?.abort(); controller.current = undefined; setStreaming(false); }, []);
  async function ask(question: string, documentIds: string[]) {
    cancel(); setAnswer(""); setSources([]); setCitations([]); setDiagnostics({}); setError(undefined); setStreaming(true);
    const abort = new AbortController(); controller.current = abort;
    try {
      const recent = history.slice(-6);
      const stream = await streamChat({ question, documentIds, history: recent }, abort.signal);
      let nextAnswer = "";
      for await (const event of parseSseStream(stream, abort.signal)) {
        if (event.type === "retrieval") { setSources(event.sources); setDiagnostics((value) => ({ ...value, retrieval: event.latencyMs, correlationId: event.correlationId })); }
        if (event.type === "token") { nextAnswer += event.text; setAnswer(nextAnswer); }
        if (event.type === "citation") setCitations((value) => value.some((item) => item.citationId === event.citation.citationId) ? value : [...value, event.citation]);
        if (event.type === "done") setDiagnostics((value) => ({ ...value, total: event.totalLatencyMs, tokens: event.inputTokens + event.outputTokens, correlationId: event.correlationId }));
        if (event.type === "error") throw new Error(`${event.code}${event.retryable ? " — try again." : "."}`);
      }
      if (!abort.signal.aborted) {
        const next = [...recent, { role: "user", content: question }, { role: "assistant", content: nextAnswer }] as ChatTurn[];
        const retained = next.slice(-6); setHistory(retained); sessionStorage.setItem(storageKey, JSON.stringify(retained));
      }
    } catch (reason) { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "Chat failed."); }
    finally { if (controller.current === abort) controller.current = undefined; setStreaming(false); }
  }
  return { history, answer, sources, citations, diagnostics, streaming, error, ask, cancel };
}
