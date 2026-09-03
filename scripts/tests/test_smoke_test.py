"""Unit tests for the deployed smoke test. An httpx MockTransport stands in for the live service."""

from __future__ import annotations

import json
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
    with_citation: bool = True,
    record: list[tuple[str, str]] | None = None,
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
            return httpx.Response(200, json={"state": state})
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


def test_skip_live_model_skips_readiness_and_chat(smoke_module: ModuleType) -> None:
    record: list[tuple[str, str]] = []
    transport = _make_transport(record=record)
    config = smoke_module.SmokeConfig(api_base=API, frontend_origin=FRONTEND, skip_live_model=True)
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
