from app.core.errors import ConcurrencyConflict
from app.domain.models import SessionRecord


class MemorySessionRepository:
    """Process-local session storage with opaque optimistic-concurrency versions."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[SessionRecord, int]] = {}

    @staticmethod
    def _etag(version: int) -> str:
        return f'W/"{version}"'

    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None:
        stored = self._sessions.get(session_key)
        if stored is None:
            return None
        record, version = stored
        return record, self._etag(version)

    async def create(self, session: SessionRecord) -> tuple[SessionRecord, str]:
        if session.session_key in self._sessions:
            raise ConcurrencyConflict
        self._sessions[session.session_key] = (session, 1)
        return session, self._etag(1)

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]:
        stored = self._sessions.get(session.session_key)
        if stored is None:
            raise ConcurrencyConflict
        _, version = stored
        if etag != self._etag(version):
            raise ConcurrencyConflict
        next_version = version + 1
        self._sessions[session.session_key] = (session, next_version)
        return session, self._etag(next_version)