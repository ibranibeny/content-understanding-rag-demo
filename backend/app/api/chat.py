import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, Protocol, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from app.api.dependencies import require_expected_origin, resolve_session_cookie
from app.domain.models import ChatRequest
from app.services.rag_service import ErrorEvent, RagEvent
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/chat", tags=["chat"])


class RagStreamer(Protocol):
    def stream(self, **kwargs: object) -> AsyncIterator[RagEvent]: ...


def get_session_service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


def get_rag_service(request: Request) -> RagStreamer:
    return cast(RagStreamer, request.app.state.rag_service)


def _sse(event: RagEvent, correlation_id: str) -> str:
    payload = event.model_dump(mode="json", by_alias=True, exclude={"type"})
    payload["correlationId"] = correlation_id
    import json

    return f"event: {event.type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post("/stream", dependencies=[Depends(require_expected_origin)])
async def stream_chat(
    body: ChatRequest,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    rag: Annotated[RagStreamer, Depends(get_rag_service)],
) -> StreamingResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    await sessions.reserve_question(session_key)
    correlation_id = request.state.correlation_id

    async def generate() -> AsyncIterator[str]:
        iterator = rag.stream(
            question=body.question,
            session_key=session_key,
            document_ids=body.document_ids,
        )
        close = iterator.aclose if isinstance(iterator, AsyncGenerator) else None
        try:
            async for event in iterator:
                if await request.is_disconnected():
                    if close is not None:
                        await close()
                    return
                yield _sse(event, correlation_id)
        except asyncio.CancelledError:
            if close is not None:
                await close()
            raise
        except (RuntimeError, ValueError, httpx.HTTPError):
            yield _sse(ErrorEvent(code="rag_unavailable", retryable=True), correlation_id)
        finally:
            if close is not None:
                await close()

    stream_response = StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Correlation-ID": correlation_id,
        },
    )
    for value in response.headers.getlist("set-cookie"):
        stream_response.headers.append("set-cookie", value)
    return stream_response