import hmac
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from app.core.config import SESSION_LIFETIME_HOURS, Settings
from app.core.errors import AppError, ConcurrencyConflict
from app.domain.models import SessionRecord, SessionResponse
from app.domain.protocols import Clock, SessionRepository

TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    raw_token: str | None
    record: SessionRecord
    is_new: bool


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        clock: Clock | None = None,
        *,
        settings: Settings | None = None,
        token_factory: Callable[[], bytes] | None = None,
        concurrency_attempts: int = 5,
    ) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()
        self._settings = settings or Settings()
        self._token_factory = token_factory or (lambda: secrets.token_bytes(TOKEN_BYTES))
        self._concurrency_attempts = concurrency_attempts

    @property
    def settings(self) -> Settings:
        return self._settings

    async def issue(self) -> ResolvedSession:
        for _ in range(self._concurrency_attempts):
            token = self._token_factory()
            if len(token) != TOKEN_BYTES:
                raise ValueError("session token factory must return exactly 32 bytes")
            raw_token = urlsafe_b64encode(token).rstrip(b"=").decode("ascii")
            now = self._clock.now()
            record = SessionRecord(
                session_key=sha256(token).hexdigest(),
                created_at=now,
                expires_at=now + timedelta(hours=SESSION_LIFETIME_HOURS),
            )
            try:
                await self._repository.create(record)
            except ConcurrencyConflict:
                continue
            return ResolvedSession(raw_token=raw_token, record=record, is_new=True)
        raise self._concurrency_error()

    async def resolve(self, raw_cookie: str | None) -> ResolvedSession:
        token = self._decode_token(raw_cookie)
        if token is None:
            return await self.issue()

        session_key = sha256(token).hexdigest()
        stored = await self._repository.get(session_key)
        if stored is None:
            return await self.issue()
        record, _ = stored
        if not hmac.compare_digest(record.session_key, session_key):
            return await self.issue()
        if record.expires_at <= self._clock.now():
            return await self.issue()
        return ResolvedSession(raw_token=None, record=record, is_new=False)

    async def reserve_document(self, session_key: str, size: int) -> SessionRecord:
        if size <= 0:
            raise AppError("invalid_document_size", 400, "Document size must be positive.", False)

        def reserve(record: SessionRecord, _: datetime) -> SessionRecord:
            if record.document_count >= self._settings.max_documents:
                raise AppError(
                    "document_quota_exceeded", 409, "The session document limit was reached.", False
                )
            if record.total_bytes + size > self._settings.max_session_bytes:
                raise AppError(
                    "storage_quota_exceeded", 409, "The session storage limit was reached.", False
                )
            return record.model_copy(
                update={
                    "document_count": record.document_count + 1,
                    "total_bytes": record.total_bytes + size,
                }
            )

        return await self._update(session_key, reserve)

    async def release_document(self, session_key: str, size: int) -> SessionRecord:
        if size <= 0:
            raise AppError("invalid_document_size", 400, "Document size must be positive.", False)

        def release(record: SessionRecord, _: datetime) -> SessionRecord:
            if record.document_count == 0 or size > record.total_bytes:
                raise AppError(
                    "invalid_quota_release", 409, "The document reservation cannot be released.", False
                )
            return record.model_copy(
                update={
                    "document_count": record.document_count - 1,
                    "total_bytes": record.total_bytes - size,
                }
            )

        return await self._update(session_key, release)

    async def reserve_question(self, session_key: str) -> SessionRecord:
        def reserve(record: SessionRecord, now: datetime) -> SessionRecord:
            cutoff = now - timedelta(hours=1)
            recent = tuple(timestamp for timestamp in record.question_timestamps if timestamp > cutoff)
            if len(recent) >= self._settings.max_questions_per_hour:
                raise AppError(
                    "question_quota_exceeded", 429, "The hourly question limit was reached.", False
                )
            return record.model_copy(update={"question_timestamps": (*recent, now)})

        return await self._update(session_key, reserve)

    def response_for(self, record: SessionRecord) -> SessionResponse:
        cutoff = self._clock.now() - timedelta(hours=1)
        questions_used = sum(timestamp > cutoff for timestamp in record.question_timestamps)
        return SessionResponse(
            expires_at=record.expires_at,
            documents_used=record.document_count,
            document_limit=self._settings.max_documents,
            bytes_used=record.total_bytes,
            byte_limit=self._settings.max_session_bytes,
            questions_used=questions_used,
            question_limit=self._settings.max_questions_per_hour,
        )

    async def _update(
        self,
        session_key: str,
        operation: Callable[[SessionRecord, datetime], SessionRecord],
    ) -> SessionRecord:
        for _ in range(self._concurrency_attempts):
            stored = await self._repository.get(session_key)
            if stored is None:
                raise AppError("session_not_found", 401, "The session was not found.", False)
            record, etag = stored
            now = self._clock.now()
            if record.expires_at <= now:
                raise AppError("session_expired", 401, "The session has expired.", False)
            updated = operation(record, now)
            try:
                replaced, _ = await self._repository.replace(updated, etag)
            except ConcurrencyConflict:
                continue
            return replaced
        raise self._concurrency_error()

    @staticmethod
    def _decode_token(raw_cookie: str | None) -> bytes | None:
        if raw_cookie is None or TOKEN_PATTERN.fullmatch(raw_cookie) is None:
            return None
        try:
            token = urlsafe_b64decode(raw_cookie + "=")
        except (ValueError, UnicodeEncodeError):
            return None
        if len(token) != TOKEN_BYTES:
            return None
        canonical = urlsafe_b64encode(token).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(canonical, raw_cookie):
            return None
        return token

    @staticmethod
    def _concurrency_error() -> AppError:
        return AppError(
            "concurrency_conflict",
            503,
            "The session changed concurrently. Retry the request.",
            True,
        )