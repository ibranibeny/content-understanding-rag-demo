import { afterEach, describe, expect, test, vi } from "vitest";

import { api } from "./client";

const upload = {
  uploadUrl: "https://blob.example/upload",
  documentId: "doc-1",
  expiresAt: "2026-09-04T10:00:00Z",
  requiredHeaders: {},
};

function stubFetch() {
  const fetch = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
    async () => new Response(JSON.stringify(upload)),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => vi.unstubAllGlobals());

describe("api.initUpload", () => {
  test("includes the canonical content range with the exact file metadata", async () => {
    const fetch = stubFetch();
    const file = new File([new Uint8Array(17)], "annual report.pdf", { type: "application/pdf" });

    await api.initUpload(file, "1-3,5");

    const [, init] = fetch.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      fileName: "annual report.pdf",
      contentType: "application/pdf",
      sizeBytes: 17,
      contentRange: "1-3,5",
    });
  });

  test("omits contentRange when no range is provided", async () => {
    const fetch = stubFetch();
    const file = new File([new Uint8Array(4)], "all-pages.pdf", { type: "application/pdf" });

    await api.initUpload(file);

    const [, init] = fetch.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      fileName: "all-pages.pdf",
      contentType: "application/pdf",
      sizeBytes: 4,
    });
  });
});
