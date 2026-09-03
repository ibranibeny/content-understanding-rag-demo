import { fireEvent, render, screen } from "@testing-library/react";

import { App } from "./App";

const session = {
  expiresAt: "2026-09-04T10:00:00Z",
  documentsUsed: 0,
  documentLimit: 5,
  bytesUsed: 0,
  byteLimit: 524_288_000,
  questionsUsed: 0,
  questionLimit: 30,
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/api/session")) return new Response(JSON.stringify(session));
      if (path.endsWith("/api/documents")) return new Response("[]");
      return new Response("{}", { status: 404 });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

test("renders the accessible technical console shell and safety disclosures", async () => {
  render(<App />);

  expect(screen.getByRole("heading", { name: /document intelligence console/i })).toBeVisible();
  expect(screen.getByRole("complementary", { name: /documents/i })).toBeVisible();
  expect(screen.getByRole("main", { name: /pipeline inspector/i })).toBeVisible();
  expect(screen.getByRole("region", { name: /grounded chat/i })).toBeVisible();
  expect(screen.getByText(/do not upload confidential information/i)).toBeVisible();
  expect(screen.getByText(/southeast asia/i)).toBeVisible();
  expect(screen.getByText(/global processing/i)).toBeVisible();
  expect(await screen.findByText(/0 of 5 documents/i)).toBeVisible();
});

test("mobile tabs expose labeled keyboard-operable controls", async () => {
  render(<App />);
  const chat = screen.getByRole("tab", { name: /chat/i });
  fireEvent.keyDown(chat, { key: "Enter" });
  expect(chat).toHaveAttribute("aria-selected", "true");
  expect(await screen.findByText(/0 of 5 documents/i)).toBeVisible();
});
