from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from app.core.config import Settings
from app.core.errors import AppError, ConcurrencyConflict
from app.domain.models import SessionRecord
from app.repositories.memory_repository import MemorySessionRepository
from app.services.session_service import SessionService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
RAW_TOKEN = urlsafe_b64encode(TOKEN).rstrip(b"=").decode("ascii")
SESSION_KEY = sha256(TOKEN).hexdigest()
MIB = 1024 * 1024


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class ConflictRepository(MemorySessionRepository):
    def __init__(self, conflicts: int) -> None:
        super().__init__()
        self.conflicts = conflicts
        self.replace_attempts = 0

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]:
        self.replace_attempts += 1
        if self.replace_attempts <= self.conflicts:
            raise ConcurrencyConflict
        return await super().replace(session, etag)


class RecordingRepository(MemorySessionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.lookups: list[str] = []

    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None:
        self.lookups.append(session_key)
        return await super().get(session_key)


def make_service(
    repository: MemorySessionRepository | None = None,
    clock: MutableClock | None = None,
) -> tuple[SessionService, MemorySessionRepository, MutableClock]:
    actual_repository = repository or MemorySessionRepository()
    actual_clock = clock or MutableClock()
    service = SessionService(actual_repository, actual_clock, token_factory=lambda: TOKEN)
    return service, actual_repository, actual_clock


async def test_issue_hashes_exactly_32_random_bytes_and_expires_in_24_hours() -> None:
    service, repository, _ = make_service()

    issued = await service.issue()

    assert issued.raw_token == RAW_TOKEN
    assert issued.raw_token != issued.record.session_key
    assert issued.record.session_key == SESSION_KEY
    assert issued.record.created_at == NOW
    assert issued.record.expires_at == NOW + timedelta(hours=24)
    stored = await repository.get(SESSION_KEY)
    assert stored is not None
    assert stored[0] == issued.record
    assert RAW_TOKEN not in stored[0].model_dump_json()


async def test_issue_ignores_attempted_session_lifetime_override() -> None:
    repository = MemorySessionRepository()
    service = SessionService(
        repository,
        MutableClock(),
        settings=Settings.model_validate({"session_lifetime_hours": 1}),
        token_factory=lambda: TOKEN,
    )

    issued = await service.issue()

    assert issued.record.expires_at == NOW + timedelta(hours=24)


async def test_resolve_round_trips_cookie_and_looks_up_only_by_derived_hash() -> None:
    repository = RecordingRepository()
    service, _, _ = make_service(repository=repository)
    issued = await service.issue()

    resolved = await service.resolve(issued.raw_token)

    assert not resolved.is_new
    assert resolved.record == issued.record
    assert repository.lookups == [SESSION_KEY]
    assert RAW_TOKEN not in repository.lookups


@pytest.mark.parametrize(
    "cookie",
    [
        None,
        "",
        urlsafe_b64encode(b"short").rstrip(b"=").decode("ascii"),
        "not+url/safe",
        RAW_TOKEN + "=",
        "é" * 43,
    ],
)
async def test_invalid_or_missing_cookie_rotates(cookie: str | None) -> None:
    service, _, _ = make_service()

    resolved = await service.resolve(cookie)

    assert resolved.is_new
    assert resolved.raw_token == RAW_TOKEN


async def test_missing_valid_cookie_rotates() -> None:
    service, _, _ = make_service()
    other_cookie = urlsafe_b64encode(b"y" * 32).rstrip(b"=").decode("ascii")

    resolved = await service.resolve(other_cookie)

    assert resolved.is_new


async def test_expired_cookie_rotates() -> None:
    clock = MutableClock()
    tokens = iter((TOKEN, b"y" * 32))
    service = SessionService(
        MemorySessionRepository(), clock, token_factory=lambda: next(tokens)
    )
    issued = await service.issue()
    clock.current = issued.record.expires_at

    resolved = await service.resolve(issued.raw_token)

    assert resolved.is_new
    assert resolved.record.created_at == clock.current


async def test_repository_enforces_create_and_replace_etags() -> None:
    repository = MemorySessionRepository()
    record = SessionRecord(session_key=SESSION_KEY, created_at=NOW, expires_at=NOW + timedelta(days=1))
    created, etag = await repository.create(record)

    assert created == record
    with pytest.raises(ConcurrencyConflict):
        await repository.create(record)
    with pytest.raises(ConcurrencyConflict):
        await repository.replace(record, 'W/"wrong"')

    replaced, replacement_etag = await repository.replace(
        record.model_copy(update={"document_count": 1}), etag
    )
    assert replaced.document_count == 1
    assert replacement_etag != etag


async def test_document_count_enforces_configured_limit() -> None:
    repository = MemorySessionRepository()
    service = SessionService(
        repository,
        MutableClock(),
        settings=Settings.model_validate({"max_documents": 2}),
        token_factory=lambda: TOKEN,
    )
    issued = await service.issue()

    for _ in range(2):
        record = await service.reserve_document(issued.record.session_key, 1)

    assert record.document_count == 2
    with pytest.raises(AppError) as caught:
        await service.reserve_document(issued.record.session_key, 1)
    assert caught.value.code == "document_quota_exceeded"


async def test_document_bytes_accept_exact_limit_and_reject_one_more() -> None:
    service, _, _ = make_service()
    issued = await service.issue()

    record = await service.reserve_document(issued.record.session_key, 500 * MIB)

    assert record.total_bytes == 500 * MIB
    with pytest.raises(AppError) as caught:
        await service.reserve_document(issued.record.session_key, 1)
    assert caught.value.code == "storage_quota_exceeded"


@pytest.mark.parametrize("size", [0, -1])
@pytest.mark.parametrize("operation", ["reserve", "release"])
async def test_document_operations_reject_nonpositive_size_without_mutation(
    size: int, operation: str
) -> None:
    service, repository, _ = make_service()
    issued = await service.issue()
    if operation == "release":
        await service.reserve_document(issued.record.session_key, 100)
    before = await repository.get(issued.record.session_key)
    assert before is not None

    with pytest.raises(AppError) as caught:
        if operation == "reserve":
            await service.reserve_document(issued.record.session_key, size)
        else:
            await service.release_document(issued.record.session_key, size)

    assert caught.value.code == "invalid_document_size"
    assert caught.value.status_code == 400
    assert not caught.value.retryable
    after = await repository.get(issued.record.session_key)
    assert after is not None
    assert after[0].document_count == before[0].document_count
    assert after[0].total_bytes == before[0].total_bytes


async def test_release_document_is_strict_and_never_underflows() -> None:
    service, _, _ = make_service()
    issued = await service.issue()
    await service.reserve_document(issued.record.session_key, 100)

    released = await service.release_document(issued.record.session_key, 100)

    assert released.document_count == 0
    assert released.total_bytes == 0
    with pytest.raises(AppError) as caught:
        await service.release_document(issued.record.session_key, 1)
    assert caught.value.code == "invalid_quota_release"


async def test_question_quota_rejects_thirty_first_in_rolling_hour() -> None:
    service, _, _ = make_service()
    issued = await service.issue()

    for _ in range(30):
        record = await service.reserve_question(issued.record.session_key)

    assert len(record.question_timestamps) == 30
    with pytest.raises(AppError) as caught:
        await service.reserve_question(issued.record.session_key)
    assert caught.value.code == "question_quota_exceeded"


async def test_question_exactly_on_hour_boundary_is_pruned_and_new_one_accepted() -> None:
    service, repository, clock = make_service()
    issued = await service.issue()
    old_timestamps = tuple(NOW for _ in range(30))
    stored = await repository.get(issued.record.session_key)
    assert stored is not None
    await repository.replace(stored[0].model_copy(update={"question_timestamps": old_timestamps}), stored[1])
    clock.current = NOW + timedelta(hours=1)

    record = await service.reserve_question(issued.record.session_key)

    assert record.question_timestamps == (clock.current,)


@pytest.mark.parametrize("operation", ["document", "release", "question"])
async def test_quota_operations_reject_absent_sessions(operation: str) -> None:
    service, _, _ = make_service()

    with pytest.raises(AppError) as caught:
        if operation == "document":
            await service.reserve_document(SESSION_KEY, 1)
        elif operation == "release":
            await service.release_document(SESSION_KEY, 1)
        else:
            await service.reserve_question(SESSION_KEY)
    assert caught.value.code == "session_not_found"


async def test_quota_operations_reject_expired_sessions() -> None:
    clock = MutableClock()
    service, _, _ = make_service(clock=clock)
    issued = await service.issue()
    clock.current = issued.record.expires_at

    with pytest.raises(AppError) as caught:
        await service.reserve_question(issued.record.session_key)
    assert caught.value.code == "session_expired"


async def test_etag_conflict_is_retried() -> None:
    repository = ConflictRepository(conflicts=2)
    service, _, _ = make_service(repository=repository)
    issued = await service.issue()

    record = await service.reserve_question(issued.record.session_key)

    assert len(record.question_timestamps) == 1
    assert repository.replace_attempts == 3


async def test_etag_conflict_exhaustion_becomes_retryable_app_error() -> None:
    repository = ConflictRepository(conflicts=5)
    service, _, _ = make_service(repository=repository)
    issued = await service.issue()

    with pytest.raises(AppError) as caught:
        await service.reserve_question(issued.record.session_key)

    assert repository.replace_attempts == 5
    assert caught.value.code == "concurrency_conflict"
    assert caught.value.retryable
