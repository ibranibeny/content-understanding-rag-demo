import { describe, expect, test } from "vitest";

import {
  MAX_CONTENT_PAGES,
  PageRangeError,
  normalizeAdvancedRange,
  normalizeSimpleRange,
} from "./pageRange";

function expectReason(action: () => unknown, reason: string): void {
  try {
    action();
    throw new Error("Expected PageRangeError");
  } catch (error) {
    expect(error).toBeInstanceOf(PageRangeError);
    expect((error as PageRangeError).reason).toBe(reason);
  }
}

describe("normalizeAdvancedRange", () => {
  test("exports the backend page limit", () => {
    expect(MAX_CONTENT_PAGES).toBe(300);
  });

  test.each([
    ["1", "1"],
    [" 1 - 3 , 5 , 9 - 12 ", "1-3,5,9-12"],
    ["1-300", "1-300"],
    ["7-7", "7"],
    ["5, 1-2", "5,1-2"],
    ["0007-0007", "7"],
  ])("normalizes %j to %j while preserving token order", (value, expected) => {
    expect(normalizeAdvancedRange(value)).toBe(expected);
  });

  test.each([
    ["", "empty"],
    ["   ", "empty"],
    ["0", "page_below_one"],
    ["3-1", "range_reversed"],
    ["1-", "invalid_syntax"],
    ["-5", "invalid_syntax"],
    ["a", "invalid_syntax"],
    ["١-٣", "invalid_syntax"],
    ["１-３", "invalid_syntax"],
    ["1,,2", "invalid_syntax"],
    ["1-3,3", "duplicate_or_overlap"],
    ["1-5,2-4", "duplicate_or_overlap"],
    ["1-301", "too_many_pages"],
  ])("rejects %j with stable reason %j", (value, reason) => {
    expectReason(() => normalizeAdvancedRange(value), reason);
  });

  test("allows exactly 300 unique pages and rejects 301 discrete pages", () => {
    expect(normalizeAdvancedRange(Array.from({ length: 300 }, (_, index) => String(index + 1)).join(","))).toBe(
      Array.from({ length: 300 }, (_, index) => String(index + 1)).join(","),
    );
    expectReason(
      () => normalizeAdvancedRange(Array.from({ length: 301 }, (_, index) => String(index + 1)).join(",")),
      "too_many_pages",
    );
  });

  test("rejects huge ranges before expanding them", () => {
    expectReason(() => normalizeAdvancedRange("1-999999999999"), "too_many_pages");
  });

  test("rejects enormous numerals with backend-compatible syntax reason", () => {
    expectReason(() => normalizeAdvancedRange("9".repeat(5_000)), "invalid_syntax");
  });

  test("preserves backend-supported numerals up to the grammar bound", () => {
    const page = "9".repeat(100);
    expect(normalizeAdvancedRange(page)).toBe(page);
  });

  test("reports overlap before aggregate size just like the backend", () => {
    expectReason(() => normalizeAdvancedRange("1-300,1"), "duplicate_or_overlap");
  });
});

describe("normalizeSimpleRange", () => {
  test("normalizes a finite inclusive range", () => {
    expect(normalizeSimpleRange(" 301 ", " 600 ")).toBe("301-600");
    expect(normalizeSimpleRange("7", "7")).toBe("7");
  });

  test.each([
    ["", "2"],
    ["1", ""],
    ["   ", "   "],
  ])("rejects a missing bound", (start, end) => {
    expectReason(() => normalizeSimpleRange(start, end), "missing_bound");
  });
});
