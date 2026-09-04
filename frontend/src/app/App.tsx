import { useState } from "react";
import { ErrorNotice } from "../components/ErrorNotice";
import { GroundedChat } from "../features/chat/GroundedChat";
import { useGroundedChat } from "../features/chat/useGroundedChat";
import { DocumentList } from "../features/documents/DocumentList";
import { DocumentUploader } from "../features/documents/DocumentUploader";
import { PipelineInspector } from "../features/documents/PipelineInspector";
import { useDocuments } from "../features/documents/useDocuments";

type Pane = "documents" | "inspector" | "chat";

export function App() {
  const [pane, setPane] = useState<Pane>("documents");
  const chat = useGroundedChat();
  const documents = useDocuments(chat.cancel);
  const expires = documents.session ? new Date(documents.session.expiresAt).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Loading…";

  function activate(next: Pane) { setPane(next); }
  return <div className="app-shell">
    <header className="console-header">
      <div className="brand"><span className="brand-mark" aria-hidden="true">DI</span><div><h1>Document Intelligence Console</h1><p>Content Understanding → Azure AI Search → GPT-5</p></div></div>
      <div className="header-facts" aria-label="Service information"><p><strong><span aria-hidden="true">● </span>API connected</strong>service health</p><p><strong>{expires}</strong>session expiry</p><p><strong>Southeast Asia → East US 2</strong>data processing</p><p><strong>GPT-5 · global processing</strong>model route</p></div>
    </header>
    <div className="safety-strip" role="note">Workshop environment · Do not upload confidential information · Files are not malware-scanned · Session data expires after 24 hours</div>
    <nav className="mobile-tabs" role="tablist" aria-label="Console panes">{(["documents", "inspector", "chat"] as Pane[]).map((item) => <button key={item} role="tab" aria-selected={pane === item} onClick={() => activate(item)} onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && activate(item)}>{item[0].toUpperCase() + item.slice(1)}</button>)}</nav>
    <div className="workspace">
      <aside className="documents-pane" aria-label="Documents" data-mobile-hidden={pane !== "documents"}>
        <DocumentUploader busy={documents.uploading} progress={documents.progress} onUpload={(file) => void documents.upload(file)} onError={documents.setError} />
        <div className="quota">{documents.session ? <><strong>{documents.session.documentsUsed} of {documents.session.documentLimit} documents</strong><br />{Math.round(documents.session.bytesUsed / 1_048_576)} of {Math.round(documents.session.byteLimit / 1_048_576)} MB · {documents.session.questionsUsed}/{documents.session.questionLimit} questions</> : "Loading session quota…"}</div>
        {documents.error && <ErrorNotice message={documents.error} onRetry={() => void documents.refresh()} />}
        {documents.loading ? <p role="status" className="empty">Loading documents…</p> : <DocumentList documents={documents.documents} selectedId={documents.selectedId} onSelect={(id) => { documents.setSelectedId(id); setPane("inspector"); }} onRetry={(id) => void documents.retry(id)} onDelete={(id) => void documents.remove(id)} />}
      </aside>
      <main className="inspector-pane" aria-label="Pipeline inspector" data-mobile-hidden={pane !== "inspector"}><PipelineInspector document={documents.selected} loading={documents.loading} /></main>
      <section className="chat-pane" aria-label="Grounded chat" data-mobile-hidden={pane !== "chat"}><GroundedChat chat={chat} selectedId={documents.selectedId} /></section>
    </div>
  </div>;
}
