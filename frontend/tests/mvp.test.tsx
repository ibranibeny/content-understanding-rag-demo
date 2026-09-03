import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { uploadBlob } from "../src/api/client";
import { parseSseStream } from "../src/api/sse";
import { ExtractionViewer } from "../src/features/documents/ExtractionViewer";

test("direct upload reports progress and sends only returned headers", async () => {
  class FakeXhr {
    static latest: FakeXhr;
    upload: { onprogress?: (event: ProgressEvent) => void } = {};
    onerror?: () => void;
    onload?: () => void;
    status = 201;
    headers: Record<string, string> = {};
    method = "";
    url = "";
    constructor() { FakeXhr.latest = this; }
    open(method: string, url: string) { this.method = method; this.url = url; }
    setRequestHeader(name: string, value: string) { this.headers[name] = value; }
    getResponseHeader(name: string) { return name === "ETag" ? '"etag-1"' : null; }
    send() { this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent); this.onload?.(); }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  const progress: number[] = [];
  const etag = await uploadBlob(new File(["test"], "a.pdf", { type: "application/pdf" }), {
    uploadUrl: "https://blob.example/upload?sig=secret", documentId: "doc", expiresAt: "2026-09-04T00:00:00Z", requiredHeaders: { "x-ms-blob-type": "BlockBlob" },
  }, (value) => progress.push(value));
  expect(etag).toBe('"etag-1"');
  expect(progress).toEqual([50, 100]);
  expect(FakeXhr.latest.headers).toEqual({ "x-ms-blob-type": "BlockBlob" });
  vi.unstubAllGlobals();
});

test("parses fragmented named SSE events in order", async () => {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode("event: retr"));
      controller.enqueue(encoder.encode('ieval\ndata: {"sources":[]}\n\nevent: token\ndata: {"text":"Hi"}\n\n'));
      controller.enqueue(encoder.encode('event: done\ndata: {"totalLatencyMs":12}\n\n'));
      controller.close();
    },
  });

  const events = [];
  for await (const event of parseSseStream(stream)) events.push(event.type);
  expect(events).toEqual(["retrieval", "token", "done"]);
});

test("aborting a stream cancels its reader", async () => {
  const cancel = vi.fn();
  const stream = new ReadableStream<Uint8Array>({ cancel });
  const controller = new AbortController();
  const consuming = (async () => {
    for await (const _event of parseSseStream(stream, controller.signal)) void _event;
  })();
  controller.abort();
  await consuming;
  expect(cancel).toHaveBeenCalled();
});

test("renders extraction as inert text rather than HTML", () => {
  render(<ExtractionViewer extraction={{ summary: "<img src=x onerror=alert(1)>" }} />);
  expect(screen.getByText(/<img src=x/)).toBeVisible();
  expect(document.querySelector("img")).toBeNull();
});

test("citation controls reveal grounded source metadata", async () => {
  const citation = { citationId: "S1", documentId: "doc-1", fileName: "invoice.pdf", sourceLocator: "page 2" };
  const { CitationList } = await import("../src/features/chat/CitationList");
  render(<CitationList citations={[citation]} sources={[{ ...citation, searchScore: 0.91, rerankerScore: 3.4 }]} />);
  fireEvent.click(screen.getByRole("button", { name: /s1 invoice.pdf/i }));
  await waitFor(() => expect(screen.getByText(/page 2 · score 0.910/i)).toBeVisible());
});