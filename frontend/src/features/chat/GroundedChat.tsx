import { useState, type FormEvent } from "react";
import { ErrorNotice } from "../../components/ErrorNotice";
import type { ReturnTypeOfChat } from "./chatTypes";
import { CitationList } from "./CitationList";

export function GroundedChat({ chat, selectedId }: { chat: ReturnTypeOfChat; selectedId?: string }) {
  const [question, setQuestion] = useState("");
  function submit(event: FormEvent) { event.preventDefault(); const value = question.trim(); if (!value || chat.streaming) return; setQuestion(""); void chat.ask(value, selectedId ? [selectedId] : []); }
  return <>
    <header className="chat-header"><span className="eyebrow">GPT-5 · grounded</span><h2 id="chat-heading">Ask the evidence</h2><p>Answers use ready documents and validated citations only.</p></header>
    <div className="chat-log" aria-live="polite">
      {!chat.history.length && !chat.answer && <p className="empty">Ask a precise question about the selected document.</p>}
      {chat.history.map((turn, index) => <div className={`message message--${turn.role}`} key={`${turn.role}-${index}`}>{turn.content}</div>)}
      {chat.answer && <div className="message message--assistant">{chat.answer}</div>}
      {chat.streaming && !chat.answer && <p role="status">Retrieving evidence…</p>}
      {chat.error && <ErrorNotice message={chat.error} />}
      <CitationList citations={chat.citations} sources={chat.sources} />
      {(chat.diagnostics.retrieval != null || chat.diagnostics.total != null) && <div className="diagnostics"><span>retrieve {chat.diagnostics.retrieval ?? "—"}ms</span><span>total {chat.diagnostics.total ?? "—"}ms</span><span>{chat.diagnostics.tokens ?? "—"} tokens</span>{chat.diagnostics.correlationId && <span title={chat.diagnostics.correlationId}>trace {chat.diagnostics.correlationId.slice(0, 8)}</span>}</div>}
    </div>
    <form className="chat-form" onSubmit={submit}><label htmlFor="chat-question">Question</label><textarea id="chat-question" maxLength={4000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What obligations are due next?" /><div className="chat-actions"><span className="mono">{question.length}/4000</span>{chat.streaming ? <button type="button" onClick={chat.cancel}>Stop</button> : <button type="submit" className="button--primary" disabled={!question.trim()}>Ask</button>}</div></form>
  </>;
}
