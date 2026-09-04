"""Unit tests for the deployed smoke test. An httpx MockTransport stands in for the live service."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType

import httpx
import pytest

API = "https://api.test"
FRONTEND = "https://frontend.test"
DOCUMENT_ID = "11111111-1111-1111-1111-111111111111"


def _sse_body(with_citation: bool) -> bytes:
    events = [
        ("retrieval", {"sources": [], "latencyMs": 5}),
        ("token", {"text": "Contoso revenue for Q3 2026 was 4,200,000 USD "}),
    ]
    if with_citation:
        events.append(
            (
                "citation",
                {
                    "citation": {
                        "citationId": "S1",
                        "documentId": DOCUMENT_ID,
                        "fileName": "smoke-sample.pdf",
                        "sourceLocator": "page-1",
                    }
                },
            )
        )
        events.append(("token", {"text": "[S1]."}))
    events.append(("done", {"inputTokens": 12, "outputTokens": 9, "totalLatencyMs": 40}))
    lines: list[str] = []
    for event, data in events:
        payload = dict(data)
        payload["correlationId"] = "corr-1"
        lines.append(f"event: {event}")
        lines.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _make_transport(
    *,
    ready_after: int = 1,
    page_count: int = 1,
    with_citation: bool = True,
    record: list[tuple[str, str]] | None = None,
    init_payloads: list[dict[str, object]] | None = None,
) -> httpx.MockTransport:
    counters = {"status_gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append((request.method, request.url.path))
        host, path, method = request.url.host, request.url.path, request.method

        if host == "blob.test":
            assert method == "PUT"
            assert request.headers.get("x-ms-blob-type") == "BlockBlob"
            assert request.headers.get("content-type") == "application/pdf"
            return httpx.Response(201, headers={"ETag": '"0x8DSMOKETESTETAG"'})

        if method == "GET" and path == "/api/session":
            return httpx.Response(
                200,
                json={"expiresAt": "2026-09-05T00:00:00Z"},
                headers={"set-cookie": "cu_session=token-abc; Path=/"},
            )
        if method == "POST" and path == "/api/uploads/init":
            assert request.headers.get("origin") == FRONTEND
            body = json.loads(request.content)
            if init_payloads is not None:
                init_payloads.append(body)
            assert body["fileName"] and body["sizeBytes"] > 0 and body["contentType"]
            return httpx.Response(
                200,
                json={
                    "uploadUrl": "https://blob.test/uploads/smoke?sig=redacted",
                    "documentId": DOCUMENT_ID,
                    "expiresAt": "2026-09-05T00:00:00Z",
                    "requiredHeaders": {"x-ms-blob-type": "BlockBlob"},
                },
            )
        if method == "POST" and path == f"/api/uploads/{DOCUMENT_ID}/complete":
            assert request.headers.get("origin") == FRONTEND
            assert json.loads(request.content)["etag"] == '"0x8DSMOKETESTETAG"'
            return httpx.Response(200, json={"state": "queued"})
        if method == "GET" and path == f"/api/documents/{DOCUMENT_ID}":
            counters["status_gets"] += 1
            state = "ready" if counters["status_gets"] >= ready_after else "analyzing"
            return httpx.Response(200, json={"state": state, "pageCount": page_count})
        if method == "POST" and path == "/api/chat/stream":
            assert request.headers.get("origin") == FRONTEND
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_body(with_citation),
            )
        if method == "DELETE" and path == f"/api/documents/{DOCUMENT_ID}":
            assert request.headers.get("origin") == FRONTEND
            return httpx.Response(202, json={"state": "deleting"})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=transport, base_url=API)


def test_make_sample_pdf_is_valid(smoke_module: ModuleType) -> None:
    pdf = smoke_module.make_sample_pdf(("Hello world",))
    assert pdf.startswith(b"%PDF-")
    assert b"%%EOF" in pdf
    assert b"stream" in pdf


def test_make_sample_pdf_builds_three_page_tree_with_text_on_every_page(
    smoke_module: ModuleType,
) -> None:
    pdf = smoke_module.make_sample_pdf(smoke_module.SAMPLE_LINES, page_count=3)
    assert b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>" in pdf
    assert b"2 0 obj\n<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>" in pdf
    assert b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>" in pdf
    assert b"/Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3" in pdf
    assert pdf.count(b"/Type /Page /Parent 2 0 R") == 3
    assert b"/Font << /F1 3 0 R >> >> /Contents 5 0 R" in pdf
    assert pdf.count(b"Total revenue for Q3 2026 was 4,200,000 USD.") == 3
    xref_offset = int(re.search(rb"startxref\n(\d+)\n%%EOF", pdf).group(1))
    assert pdf[xref_offset:].startswith(b"xref\n")
    for object_number in range(1, 10):
        object_offset = pdf.index(f"{object_number} 0 obj\n".encode("ascii"))
        xref_lines = pdf[xref_offset:].splitlines()
        assert int(xref_lines[2 + object_number].split()[0]) == object_offset


def test_parse_sse_groups_events(smoke_module: ModuleType) -> None:
    lines = ["event: token", 'data: {"text":"hi"}', "", "event: done", 'data: {"totalLatencyMs":3}', ""]
    events = list(smoke_module.parse_sse(lines))
    assert events[0] == ("token", {"text": "hi"})
    assert events[1][0] == "done"


def test_full_flow_requires_citation_and_deletes(smoke_module: ModuleType) -> None:
    record: list[tuple[str, str]] = []
    transport = _make_transport(ready_after=2, record=record)
    config = smoke_module.SmokeConfig(api_base=API, frontend_origin=FRONTEND, poll_interval=0.0)
    pdf = smoke_module.make_sample_pdf(smoke_module.SAMPLE_LINES)
    with _client(transport) as client:
        result = smoke_module.run_smoke(client, config, pdf, sleep=lambda _s: None)
    assert result.final_state == "ready"
    assert result.citation_count == 1
    assert result.deleted is True
    assert ("POST", "/api/uploads/init") in record
    assert ("PUT", "/uploads/smoke") in record
    assert ("POST", "/api/chat/stream") in record
    assert ("DELETE", f"/api/documents/{DOCUMENT_ID}") in record
    # The status endpoint was polled until it reported ready.
    assert sum(1 for method, path in record if method == "GET" and path.endswith(DOCUMENT_ID)) == 2


def test_range_is_sent_and_expected_ready_page_count_matches(smoke_module: ModuleType) -> None:
    init_payloads: list[dict[str, object]] = []
    transport = _make_transport(page_count=2, init_payloads=init_payloads)
    config = smoke_module.SmokeConfig(
        api_base=API,
        frontend_origin=FRONTEND,
        content_range="2-3",
        expected_page_count=2,
        poll_interval=0.0,
    )
    pdf = smoke_module.make_sample_pdf(smoke_module.SAMPLE_LINES, page_count=3)
    with _client(transport) as client:
        result = smoke_module.run_smoke(client, config, pdf, sleep=lambda _s: None)
    assert result.final_state == "ready"
    assert init_payloads[0]["contentRange"] == "2-3"


def test_default_upload_init_omits_content_range(smoke_module: ModuleType) -> None:
    init_payloads: list[dict[str, object]] = []
    transport = _make_transport(init_payloads=init_payloads)
    config = smoke_module.SmokeConfig(api_base=API, frontend_origin=FRONTEND)
    with _client(transport) as client:
        smoke_module.run_smoke(
            client,
            config,
            smoke_module.make_sample_pdf(("x",)),
            sleep=lambda _s: None,
        )
    assert "contentRange" not in init_payloads[0]


def test_ready_page_count_mismatch_has_safe_useful_error(smoke_module: ModuleType) -> None:
    transport = _make_transport(page_count=3)
    config = smoke_module.SmokeConfig(
        api_base=API, frontend_origin=FRONTEND, expected_page_count=2
    )
    with _client(transport) as client, pytest.raises(
        smoke_module.SmokeError, match="expected 2 processed pages, got 3"
    ):
        smoke_module.run_smoke(
            client,
            config,
            smoke_module.make_sample_pdf(smoke_module.SAMPLE_LINES, page_count=3),
            sleep=lambda _s: None,
        )


def test_skip_live_model_skips_readiness_and_chat(smoke_module: ModuleType) -> None:
    record: list[tuple[str, str]] = []
    transport = _make_transport(record=record)
    config = smoke_module.SmokeConfig(
        api_base=API,
        frontend_origin=FRONTEND,
        skip_live_model=True,
        expected_page_count=999,
    )
    pdf = smoke_module.make_sample_pdf(("x",))
    with _client(transport) as client:
        result = smoke_module.run_smoke(client, config, pdf, sleep=lambda _s: None)
    assert result.citation_count == 0
    assert result.deleted is True
    assert ("POST", "/api/chat/stream") not in record
    assert not any(method == "GET" and path.endswith(DOCUMENT_ID) for method, path in record)


def test_missing_citation_fails(smoke_module: ModuleType) -> None:
    transport = _make_transport(ready_after=1, with_citation=False)
    config = smoke_module.SmokeConfig(api_base=API, frontend_origin=FRONTEND)
    pdf = smoke_module.make_sample_pdf(("x",))
    with _client(transport) as client, pytest.raises(smoke_module.SmokeError):
        smoke_module.run_smoke(client, config, pdf, sleep=lambda _s: None)


def test_readiness_timeout_fails_fast(smoke_module: ModuleType) -> None:
    transport = _make_transport(ready_after=999)
    config = smoke_module.SmokeConfig(
        api_base=API, frontend_origin=FRONTEND, ready_timeout=0.0, poll_interval=0.0
    )
    pdf = smoke_module.make_sample_pdf(("x",))
    with _client(transport) as client, pytest.raises(smoke_module.SmokeError):
        smoke_module.run_smoke(client, config, pdf, sleep=lambda _s: None)


def test_cli_configures_generated_ranged_pdf(smoke_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_smoke(client: httpx.Client, config: object, pdf_bytes: bytes) -> object:
        captured.update(config=config, pdf_bytes=pdf_bytes)
        return smoke_module.SmokeResult(DOCUMENT_ID, "ready", 1, "answer", True)

    monkeypatch.setattr(smoke_module, "run_smoke", fake_run_smoke)
    exit_code = smoke_module.main(
        [
            "--api-base", API,
            "--frontend-origin", FRONTEND,
            "--generated-pages", "3",
            "--content-range", "2-3",
            "--expect-pages", "2",
        ]
    )
    config = captured["config"]
    assert exit_code == 0
    assert config.content_range == "2-3"
    assert config.expected_page_count == 2
    assert b"/Count 3" in captured["pdf_bytes"]


def test_cli_accepts_composite_generated_pdf_range(
    smoke_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_smoke(client: httpx.Client, config: object, pdf_bytes: bytes) -> object:
        captured["config"] = config
        return smoke_module.SmokeResult(DOCUMENT_ID, "ready", 1, "answer", True)

    monkeypatch.setattr(smoke_module, "run_smoke", fake_run_smoke)
    exit_code = smoke_module.main(
        [
            "--api-base", API,
            "--frontend-origin", FRONTEND,
            "--generated-pages", "5",
            "--content-range", "1-3,5",
            "--expect-pages", "4",
        ]
    )
    assert exit_code == 0
    assert captured["config"].content_range == "1-3,5"


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--generated-pages", "0"],
        ["--expect-pages", "0"],
        ["--generated-pages", "2", "--expect-pages", "3"],
        ["--generated-pages", "2", "--content-range", "2-3"],
        ["--generated-pages", "3", "--content-range", "2-3", "--expect-pages", "1"],
        ["--content-range", "3-2"],
    ],
)
def test_cli_rejects_invalid_page_options(
    smoke_module: ModuleType,
    extra_args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = smoke_module.main(
        ["--api-base", API, "--frontend-origin", FRONTEND, *extra_args]
    )
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_rejects_range_options_for_non_pdf_file(
    smoke_module: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    exit_code = smoke_module.main(
        [
            "--api-base", API,
            "--frontend-origin", FRONTEND,
            "--file", str(image),
            "--content-range", "1-1",
        ]
    )
    assert exit_code == 2
    assert "PDF" in capsys.readouterr().err


def test_cli_rejects_generated_pages_for_non_pdf_file(
    smoke_module: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    exit_code = smoke_module.main(
        [
            "--api-base", API,
            "--frontend-origin", FRONTEND,
            "--file", str(image),
            "--generated-pages", "3",
        ]
    )
    assert exit_code == 2
    assert "PDF" in capsys.readouterr().err
