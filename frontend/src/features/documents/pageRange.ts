export const MAX_CONTENT_PAGES = 300;

const MAX_NUMERAL_DIGITS = 100;
const TOKEN_PATTERN = new RegExp(
  `^(\\d{1,${MAX_NUMERAL_DIGITS}})(?:\\s*-\\s*(\\d{1,${MAX_NUMERAL_DIGITS}}))?$`,
);

export class PageRangeError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    super(reason);
    this.name = "PageRangeError";
    this.reason = reason;
  }
}

export function normalizeAdvancedRange(value: string): string {
  if (!value.trim()) {
    throw new PageRangeError("empty");
  }

  const selectedPages = new Set<bigint>();
  const normalizedTokens: string[] = [];

  for (const rawToken of value.split(",")) {
    const match = TOKEN_PATTERN.exec(rawToken.trim());
    if (match === null) {
      throw new PageRangeError("invalid_syntax");
    }

    const start = BigInt(match[1]);
    const end = match[2] === undefined ? start : BigInt(match[2]);
    if (start < 1n || end < 1n) {
      throw new PageRangeError("page_below_one");
    }
    if (start > end) {
      throw new PageRangeError("range_reversed");
    }

    const pageCount = end - start + 1n;
    if (pageCount > BigInt(MAX_CONTENT_PAGES)) {
      throw new PageRangeError("too_many_pages");
    }

    for (let page = start; page <= end; page += 1n) {
      if (selectedPages.has(page)) {
        throw new PageRangeError("duplicate_or_overlap");
      }
    }
    if (BigInt(selectedPages.size) + pageCount > BigInt(MAX_CONTENT_PAGES)) {
      throw new PageRangeError("too_many_pages");
    }
    for (let page = start; page <= end; page += 1n) {
      selectedPages.add(page);
    }

    normalizedTokens.push(start === end ? String(start) : `${start}-${end}`);
  }

  return normalizedTokens.join(",");
}

export function normalizeSimpleRange(start: string, end: string): string {
  if (!start.trim() || !end.trim()) {
    throw new PageRangeError("missing_bound");
  }
  return normalizeAdvancedRange(`${start}-${end}`);
}
