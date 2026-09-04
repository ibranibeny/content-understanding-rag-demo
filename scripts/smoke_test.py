#!/usr/bin/env python
"""Deployed end-to-end smoke test for the Content Understanding RAG console.

Exercises the public contract exactly as a browser would: create an anonymous session, request an
upload SAS, PUT a small PDF directly to Blob storage, complete the upload, wait for ingestion to
reach ``ready``, ask a grounded question over Server-Sent Events, require at least one citation, and
delete the document. ``--skip-live-model`` runs a preliminary check that validates session/upload
plumbing without waiting on Content Understanding or gpt-5. No keys or secrets are used or printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

INSUFFICIENT_EVIDENCE = "i don't have enough evidence"
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
SAMPLE_LINES: tuple[str, ...] = (
    "Contoso Ltd - Quarterly Business Review",
    "Prepared by Alex Taylor on 15 August 2026.",
    "Total revenue for Q3 2026 was 4,200,000 USD.",
    "Operating expenses for the quarter were 1,750,000 USD.",
    "The board approved a dividend of 0.35 USD per share.",
)
SAMPLE_QUESTION = "What was Contoso's total revenue for Q3 2026?"


class SmokeError(RuntimeError):
    """A smoke-test assertion or transport failure with an operator-safe message."""


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    api_base: str
    frontend_origin: str
    file_name: str = "smoke-sample.pdf"
    content_type: str = "application/pdf"
    question: str = SAMPLE_QUESTION
    expect_substring: str | None = None
    content_range: str | None = None
    expected_page_count: int | None = None
    skip_live_model: bool = False
    ready_timeout: float = 300.0
    poll_interval: float = 5.0


@dataclass(frozen=True, slots=True)
class SmokeResult:
    document_id: str
    final_state: str
    citation_count: int
    answer: str
    deleted: bool


def make_sample_pdf(lines: Sequence[str], page_count: int = 1) -> bytes:
    """Build a valid text-bearing PDF with the requested pages and a correct xref table."""
    if page_count < 1:
        raise ValueError("page_count must be at least 1")

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            ops.append("T*")
        ops.append(f"({escape(line)}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("ascii")
    page_numbers = [4 + index * 2 for index in range(page_count)]
    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{number} 0 R".encode("ascii") for number in page_numbers)
            + f"] /Count {page_count} >>".encode("ascii")
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page_number in page_numbers:
        content_number = page_number + 1
        bodies.extend(
            [
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
                    + f"{content_number} 0 R >>".encode("ascii")
                ),
                (
                    b"<< /Length "
                    + str(len(stream)).encode("ascii")
                    + b" >>\nstream\n"
                    + stream
                    + b"\nendstream"
                ),
            ]
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(out)
    size = len(bodies) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += b"trailer\n" + f"<< /Size {size} /Root 1 0 R >>\n".encode("ascii")
    out += b"startxref\n" + f"{xref_offset}\n".encode("ascii") + b"%%EOF\n"
    return bytes(out)


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse a Server-Sent Events stream into (event, data) pairs."""
    event = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event, _decode_sse_data(data_lines)
            event, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip(" "))
    if data_lines:
        yield event, _decode_sse_data(data_lines)


def _decode_sse_data(data_lines: Sequence[str]) -> dict[str, Any]:
    try:
        decoded = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _require(response: httpx.Response, expected: int, label: str) -> None:
    if response.status_code != expected:
        raise SmokeError(f"{label} returned HTTP {response.status_code}, expected {expected}")


def run_smoke(
    client: httpx.Client,
    config: SmokeConfig,
    pdf_bytes: bytes,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeResult:
    """Run the full (or preliminary) smoke journey against an already-deployed environment."""
    origin = {"Origin": config.frontend_origin}

    _require(client.get("/api/session"), 200, "session")

    init_payload: dict[str, str | int] = {
        "fileName": config.file_name,
        "contentType": config.content_type,
        "sizeBytes": len(pdf_bytes),
    }
    if config.content_range is not None:
        init_payload["contentRange"] = config.content_range
    init = client.post(
        "/api/uploads/init",
        headers=origin,
        json=init_payload,
    )
    _require(init, 200, "upload init")
    init_body = init.json()
    document_id = init_body.get("documentId")
    upload_url = init_body.get("uploadUrl")
    if not document_id or not upload_url:
        raise SmokeError("upload init response is missing documentId or uploadUrl")
    required_headers = dict(init_body.get("requiredHeaders") or {})

    put = client.put(
        upload_url,
        content=pdf_bytes,
        headers={**required_headers, "Content-Type": config.content_type},
    )
    if put.status_code not in (200, 201):
        raise SmokeError(f"direct SAS blob upload failed: HTTP {put.status_code}")
    etag = put.headers.get("ETag") or put.headers.get("etag")
    if not etag:
        raise SmokeError("direct SAS blob upload did not return an ETag")

    complete = client.post(
        f"/api/uploads/{document_id}/complete", headers=origin, json={"etag": etag}
    )
    _require(complete, 200, "upload complete")
    state = str(complete.json().get("state", ""))

    if config.skip_live_model:
        deleted = _delete_document(client, document_id, origin)
        return SmokeResult(str(document_id), state, 0, "", deleted)

    state = _poll_until_ready(client, str(document_id), config, sleep=sleep, monotonic=monotonic)
    citation_count, answer = _ask_grounded_question(client, str(document_id), config, origin)
    if citation_count < 1:
        raise SmokeError("the grounded answer contained no citations")
    if INSUFFICIENT_EVIDENCE in answer.lower():
        raise SmokeError("the model reported insufficient evidence for a known question")
    if config.expect_substring and config.expect_substring.lower() not in answer.lower():
        raise SmokeError(f"expected substring not found in answer: {config.expect_substring!r}")

    deleted = _delete_document(client, str(document_id), origin)
    return SmokeResult(str(document_id), state, citation_count, answer, deleted)


def _poll_until_ready(
    client: httpx.Client,
    document_id: str,
    config: SmokeConfig,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> str:
    deadline = monotonic() + config.ready_timeout
    last_state = "unknown"
    while True:
        response = client.get(f"/api/documents/{document_id}")
        _require(response, 200, "document status")
        body = response.json()
        last_state = str(body.get("state", "unknown"))
        if last_state == "ready":
            if config.expected_page_count is not None:
                actual_page_count = body.get("pageCount")
                if actual_page_count != config.expected_page_count:
                    raise SmokeError(
                        f"expected {config.expected_page_count} processed pages, "
                        f"got {actual_page_count!r}"
                    )
            return last_state
        if last_state in {"failed", "deleting", "deleted"}:
            raise SmokeError(f"document reached terminal state {last_state!r} before ready")
        if monotonic() >= deadline:
            raise SmokeError(
                f"timed out after {config.ready_timeout:.0f}s waiting for ready "
                f"(last state: {last_state})"
            )
        sleep(config.poll_interval)


def _ask_grounded_question(
    client: httpx.Client,
    document_id: str,
    config: SmokeConfig,
    origin: dict[str, str],
) -> tuple[int, str]:
    citations = 0
    tokens: list[str] = []
    with client.stream(
        "POST",
        "/api/chat/stream",
        headers={**origin, "Accept": "text/event-stream"},
        json={"question": config.question, "documentIds": [document_id]},
    ) as response:
        if response.status_code != 200:
            raise SmokeError(f"chat stream returned HTTP {response.status_code}")
        for event, data in parse_sse(response.iter_lines()):
            if event == "citation":
                citations += 1
            elif event == "token":
                tokens.append(str(data.get("text", "")))
            elif event == "error":
                raise SmokeError(f"chat stream returned an error event: {data.get('code')}")
    return citations, "".join(tokens)


def _delete_document(client: httpx.Client, document_id: str, origin: dict[str, str]) -> bool:
    response = client.delete(f"/api/documents/{document_id}", headers=origin)
    if response.status_code not in (200, 202):
        raise SmokeError(f"delete returned HTTP {response.status_code}")
    return True


def _content_type_for(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    content_type = CONTENT_TYPES.get(suffix)
    if content_type is None:
        raise SmokeError(f"unsupported file extension for smoke upload: {suffix!r}")
    return content_type


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deployed Content Understanding RAG smoke test")
    parser.add_argument("--api-base", default=None, help="API base URL, for example https://ca-api-...")
    parser.add_argument("--frontend-origin", default=None, help="Allowed browser origin (Origin header)")
    parser.add_argument("--file", default=None, help="Optional path to a supplied document to upload")
    parser.add_argument("--question", default=None, help="Grounded question to ask")
    parser.add_argument("--expect", default=None, help="Optional substring the answer must contain")
    parser.add_argument("--content-range", default=None, help="PDF page range to process, for example 2-3")
    parser.add_argument("--expect-pages", type=int, default=None, help="Expected processed page count")
    parser.add_argument("--generated-pages", type=int, default=1, help="Pages in the generated sample PDF")
    parser.add_argument("--timeout", type=float, default=300.0, help="Readiness timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Readiness poll interval")
    parser.add_argument(
        "--skip-live-model",
        action="store_true",
        help="Preliminary run: validate session/upload only; skip readiness, RAG, and citations.",
    )
    return parser.parse_args(argv)


def _validate_page_options(args: argparse.Namespace, file_name: str | None) -> str | None:
    if args.generated_pages < 1:
        return "--generated-pages must be at least 1"
    if args.expect_pages is not None and args.expect_pages < 1:
        return "--expect-pages must be at least 1"
    if args.content_range is not None:
        selected_pages: set[int] = set()
        for token in args.content_range.split(","):
            match = re.fullmatch(r"([1-9]\d*)(?:-([1-9]\d*))?", token)
            if match is None:
                return "--content-range must contain PDF pages or ascending ranges"
            range_start = int(match.group(1))
            range_end = int(match.group(2) or match.group(1))
            pages = set(range(range_start, range_end + 1))
            if range_start > range_end or selected_pages.intersection(pages):
                return "--content-range must contain non-overlapping ascending PDF ranges"
            selected_pages.update(pages)
        if file_name is None and max(selected_pages) > args.generated_pages:
            return "--content-range cannot exceed --generated-pages"
        if args.expect_pages is not None and args.expect_pages != len(selected_pages):
            return "--expect-pages must equal the number of pages in --content-range"
    elif file_name is None and args.expect_pages is not None and args.expect_pages > args.generated_pages:
        return "--expect-pages cannot exceed --generated-pages"
    if (
        file_name is not None
        and Path(file_name).suffix.lower() != ".pdf"
        and (
            args.content_range is not None
            or args.expect_pages is not None
            or args.generated_pages != 1
        )
    ):
        return "page range, count, and generation options require a PDF supplied file"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    page_options_error = _validate_page_options(args, args.file)
    if page_options_error is not None:
        print(f"error: {page_options_error}", file=sys.stderr)
        return 2
    api_base = args.api_base or os.environ.get("API_BASE_URL") or os.environ.get("API_URL")
    frontend_origin = (
        args.frontend_origin or os.environ.get("FRONTEND_ORIGIN") or os.environ.get("FRONTEND_URL")
    )
    if not api_base or not frontend_origin:
        print(
            "error: provide --api-base and --frontend-origin (or API_URL/FRONTEND_URL env vars).",
            file=sys.stderr,
        )
        return 2

    if args.file:
        pdf_bytes = Path(args.file).read_bytes()
        file_name = Path(args.file).name
        content_type = _content_type_for(file_name)
        question = args.question or SAMPLE_QUESTION
    else:
        pdf_bytes = make_sample_pdf(SAMPLE_LINES, page_count=args.generated_pages)
        file_name = "smoke-sample.pdf"
        content_type = "application/pdf"
        question = args.question or SAMPLE_QUESTION

    config = SmokeConfig(
        api_base=api_base.rstrip("/"),
        frontend_origin=frontend_origin.rstrip("/"),
        file_name=file_name,
        content_type=content_type,
        question=question,
        expect_substring=args.expect,
        content_range=args.content_range,
        expected_page_count=args.expect_pages,
        skip_live_model=args.skip_live_model,
        ready_timeout=args.timeout,
        poll_interval=args.poll_interval,
    )

    timeout = httpx.Timeout(60.0, read=max(120.0, config.ready_timeout))
    with httpx.Client(base_url=config.api_base, timeout=timeout, follow_redirects=False) as client:
        try:
            result = run_smoke(client, config, pdf_bytes)
        except SmokeError as exc:
            print(f"SMOKE FAIL: {exc}", file=sys.stderr)
            return 1

    mode = "preliminary (skip-live-model)" if config.skip_live_model else "full"
    print(
        f"SMOKE PASS [{mode}] document={result.document_id} state={result.final_state} "
        f"citations={result.citation_count} deleted={result.deleted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
