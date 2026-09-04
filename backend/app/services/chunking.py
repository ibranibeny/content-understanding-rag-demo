from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from uuid import UUID

import tiktoken

DEFAULT_MAX_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 120
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LOCATOR = re.compile(
    r"^\s*<!--\s*(PageNumber|SlideNumber|ImageNumber)\s*=\s*(\d+)\s*-->\s*$",
    re.IGNORECASE,
)
_LEXICAL_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+")


class _Encoding(Protocol):
    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


class _LocalEncoding:
    """Deterministic no-download approximation used only when the cached BPE is unavailable."""

    def __init__(self) -> None:
        self._values: dict[int, str] = {}

    def encode(self, text: str) -> list[int]:
        tokens: list[int] = []
        for ordinal, match in enumerate(_LEXICAL_TOKEN.finditer(text)):
            token = int.from_bytes(
                hashlib.sha256(f"{ordinal}:{match.group(0)}".encode()).digest()[:8], "big"
            )
            self._values[token] = match.group(0)
            tokens.append(token)
        return tokens

    def decode(self, tokens: list[int]) -> str:
        parts = [self._values[token] for token in tokens]
        text = " ".join(parts)
        return re.sub(r"\s+([,.;:!?%\)\]\}])", r"\1", text)


def _load_tiktoken() -> _Encoding:
    # get_encoding loads cl100k_base from tiktoken's installed/cache data only in this project.
    return tiktoken.get_encoding("cl100k_base")


@lru_cache(maxsize=1)
def _encoding() -> _Encoding:
    try:
        return _load_tiktoken()
    except (FileNotFoundError, ImportError, KeyError, OSError, RuntimeError, ValueError):
        return _LocalEncoding()


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    chunk_id: str
    ordinal: int
    section_path: str | None
    page_number: int | None
    source_locator: str
    content: str


@dataclass(frozen=True, slots=True)
class _Region:
    section_path: str | None
    page_number: int | None
    source_locator: str
    text: str


def chunk_markdown(
    markdown: str,
    *,
    document_id: UUID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[ChunkDraft]:
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    chunks: list[ChunkDraft] = []
    for region in _regions(markdown):
        for content in _chunk_region(region.text, max_tokens, overlap_tokens):
            ordinal = len(chunks)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            digest = hashlib.sha256(
                f"{document_id}:{ordinal}:{content_hash}".encode()
            ).digest()
            chunk_id = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            chunks.append(
                ChunkDraft(
                    chunk_id=chunk_id,
                    ordinal=ordinal,
                    section_path=region.section_path,
                    page_number=region.page_number,
                    source_locator=region.source_locator,
                    content=content,
                )
            )
    return chunks


def _regions(markdown: str) -> list[_Region]:
    headings: list[str] = []
    locator = "section 1"
    page_number: int | None = None
    buffer: list[str] = []
    regions: list[_Region] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            regions.append(
                _Region(" > ".join(headings) or None, page_number, locator, text)
            )
        buffer.clear()

    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        heading = _HEADING.match(raw_line)
        marker = _LOCATOR.match(raw_line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings[level - 1 :] = [heading.group(2).strip()]
            locator = f"section {' > '.join(headings)}"
            page_number = None
        elif marker:
            flush()
            kind, number_text = marker.groups()
            number = int(number_text)
            normalized = kind.lower().removesuffix("number")
            locator = f"{normalized} {number}"
            page_number = number if normalized == "page" else None
        else:
            buffer.append(raw_line)
    flush()
    return regions


def _chunk_region(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    units: list[tuple[str, bool]] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if count_tokens(paragraph) <= max_tokens:
            units.append((paragraph, False))
            continue
        sentences = [value.strip() for value in _SENTENCE_END.split(paragraph) if value.strip()]
        for sentence in sentences:
            if count_tokens(sentence) <= max_tokens:
                units.append((sentence, False))
            else:
                units.extend(
                    (window, True)
                    for window in _token_windows(sentence, max_tokens, overlap_tokens)
                )

    chunks: list[str] = []
    current: list[str] = []
    previous_has_overlap = False
    for unit, has_overlap in units:
        candidate = "\n\n".join((*current, unit))
        if current and count_tokens(candidate) > max_tokens:
            content = "\n\n".join(current)
            chunks.append(content)
            prefix = "" if has_overlap or previous_has_overlap else _overlap_text(content, overlap_tokens)
            current = [prefix, unit] if prefix and count_tokens(f"{prefix}\n\n{unit}") <= max_tokens else [unit]
        else:
            current.append(unit)
        previous_has_overlap = has_overlap
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _token_windows(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    encoding = _encoding()
    tokens = encoding.encode(text)
    step = max_tokens - overlap_tokens
    return [
        encoding.decode(tokens[start : start + max_tokens]).strip()
        for start in range(0, len(tokens), step)
        if tokens[start : start + max_tokens]
    ]


def _overlap_text(text: str, overlap_tokens: int) -> str:
    if overlap_tokens == 0:
        return ""
    encoding = _encoding()
    return encoding.decode(encoding.encode(text)[-overlap_tokens:]).strip()
