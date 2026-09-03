import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient

from app.api.chat import stream_chat
from app.core.config import Settings
from app.domain.models import SessionRecord
from app.main import create_app
from app.repositories.memory_repository import MemoryApplicationRepository
from app.services.session_service import SessionService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
SESSION = sha256(TOKEN).hexdigest()
COOKIE = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg"
ORIGIN = {"Origin": "http://testserver"}


class Clock:
    def now(self) -> datetime:
        return NOW


class Rag:
    def __init__(self) -> None:
        self.cancelled = False

    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        del kwargs
        from app.services.rag_service import CitationEvent, DoneEvent, RetrievalEvent, TokenEvent

        try:
            yield RetrievalEvent(sources=(), latency_ms=1)
            yield TokenEvent(text="Answer [S1]")
            yield CitationEvent(
                citation={
                    "citationId": "S1",
                    "documentId": UUID(int=1),
                    "fileName": "safe.pdf",
                    "sourceLocator": "page 1",
                }
            )
            yield DoneEvent(total_latency_ms=2)
        finally:
            self.cancelled = True


def client_for(*, questions: int = 0, rag: Rag | None = None) -> tuple[TestClient, Rag]:
    repository = MemoryApplicationRepository()
    timestamps = tuple(NOW - timedelta(minutes=index) for index in range(questions))
    asyncio.run(
        repository.sessions.create(
            SessionRecord(
                session_key=SESSION,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                question_timestamps=timestamps,
            )
        )
    )
    sessions = SessionService(
        repository.sessions,
        Clock(),
        settings=Settings(app_mode="test"),
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    actual_rag = rag or Rag()
    app = create_app(
        settings=Settings(app_mode="test"),
        session_service=sessions,
        rag_service=actual_rag,
        enable_outbox_dispatcher=False,
    )
    client = TestClient(app)
    client.cookies.set("cu_session", COOKIE)
    return client, actual_rag


def parse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return events


def test_streamed_event_order_and_headers_are_strict() -> None:
    client, _ = client_for()
    response = client.post("/api/chat/stream", headers=ORIGIN, json={"question": "What?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert [event for event, _ in parse_events(response.text)] == [
        "retrieval", "token", "citation", "done"
    ]


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Origin": "https://evil.example"}, 403, "invalid_origin"),
        ({}, 403, "invalid_origin"),
    ],
)
def test_chat_requires_exact_origin(headers: dict[str, str], status: int, code: str) -> None:
    client, _ = client_for()
    response = client.post("/api/chat/stream", headers=headers, json={"question": "What?"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_chat_enforces_question_length_and_hourly_quota() -> None:
    client, _ = client_for()
    too_long = client.post(
        "/api/chat/stream", headers=ORIGIN, json={"question": "x" * 4001}
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "invalid_request"

    limited, _ = client_for(questions=30)
    quota = limited.post("/api/chat/stream", headers=ORIGIN, json={"question": "What?"})
    assert quota.status_code == 429
    assert quota.json()["error"]["code"] == "question_quota_exceeded"


async def test_disconnect_cancels_event_generation() -> None:
    client, rag = await asyncio.to_thread(client_for)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/stream",
            "headers": [(b"cookie", f"cu_session={COOKIE}".encode())],
            "app": client.app,
            "state": {"correlation_id": str(UUID(int=2))},
        }
    )
    disconnected = False

    async def is_disconnected() -> bool:
        nonlocal disconnected
        value = disconnected
        disconnected = True
        return value

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    response = await stream_chat(
        body=type("Body", (), {"question": "What?", "document_ids": ()})(),
        request=request,
        response=Response(),
        sessions=client.app.state.session_service,
        rag=rag,
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    assert len(chunks) == 1
    assert rag.cancelled is True