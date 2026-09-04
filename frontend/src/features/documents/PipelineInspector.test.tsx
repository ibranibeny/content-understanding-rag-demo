import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { DocumentSummary } from "../../domain/types";
import { PipelineInspector } from "./PipelineInspector";

function document(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    documentId: "doc-1",
    fileName: "annual-report.pdf",
    state: "ready",
    failureRetryable: false,
    retryCount: 0,
    createdAt: "2026-09-04T09:00:00Z",
    updatedAt: "2026-09-04T09:01:00Z",
    expiresAt: "2026-09-04T10:00:00Z",
    ...overrides,
  };
}

describe("PipelineInspector", () => {
  test("shows the canonical requested page range", () => {
    render(<PipelineInspector document={document({ contentRange: "1-3,5" })} />);

    expect(screen.getByText("Pages requested")).toBeVisible();
    expect(screen.getByText("1-3,5")).toBeVisible();
  });

  test.each([undefined, null])("shows ALL when the requested page range is %s", (contentRange) => {
    render(<PipelineInspector document={document({ contentRange })} />);

    expect(screen.getByText("Pages requested")).toBeVisible();
    expect(screen.getByText("ALL")).toBeVisible();
  });

  test("marks the five-card metrics group for a balanced layout", () => {
    render(<PipelineInspector document={document()} />);

    const metrics = screen.getByText("Pages requested").parentElement?.parentElement;

    expect(metrics).toHaveClass("metrics", "metrics--five");
    expect(metrics).toHaveTextContent("Pages requested");
    expect(metrics).toHaveTextContent("Pages");
    expect(metrics).toHaveTextContent("Chunks");
    expect(metrics).toHaveTextContent("Vector");
    expect(metrics).toHaveTextContent("Tokens");
    expect(metrics?.children).toHaveLength(5);
  });
});
