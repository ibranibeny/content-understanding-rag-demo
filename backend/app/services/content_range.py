import re

MAX_CONTENT_PAGES = 300
_MAX_NUMERAL_DIGITS = 100
_TOKEN_PATTERN = re.compile(
    rf"^(\d{{1,{_MAX_NUMERAL_DIGITS}}})(?:\s*-\s*(\d{{1,{_MAX_NUMERAL_DIGITS}}}))?$"
)


class InvalidContentRange(ValueError):
    """Raised when a requested content range is invalid."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def normalize_content_range(raw: str) -> str:
    """Validate and canonically format a finite, 1-based page selection."""
    if not raw.strip():
        raise InvalidContentRange("empty")

    selected_pages: set[int] = set()
    normalized_tokens: list[str] = []

    for raw_token in raw.split(","):
        token = raw_token.strip()
        match = _TOKEN_PATTERN.fullmatch(token)
        if match is None:
            raise InvalidContentRange("invalid_syntax")

        try:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) is not None else start
        except ValueError as error:
            raise InvalidContentRange("invalid_syntax") from error
        if start < 1 or end < 1:
            raise InvalidContentRange("page_below_one")
        if start > end:
            raise InvalidContentRange("range_reversed")

        page_count = end - start + 1
        if page_count > MAX_CONTENT_PAGES:
            raise InvalidContentRange("too_many_pages")

        pages = range(start, end + 1)
        if any(page in selected_pages for page in pages):
            raise InvalidContentRange("duplicate_or_overlap")
        if len(selected_pages) + page_count > MAX_CONTENT_PAGES:
            raise InvalidContentRange("too_many_pages")
        selected_pages.update(pages)

        normalized_tokens.append(str(start) if start == end else f"{start}-{end}")

    return ",".join(normalized_tokens)
