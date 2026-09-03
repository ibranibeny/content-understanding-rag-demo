import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.domain.protocols import ChunkSearch
from app.main import ApplicationDependencies, ProductionDependencies, create_production_dependencies
from app.services.deletion_service import DeletionService

CLEANUP_PAGE_SIZE = 100
CLEANUP_SCAN_LIMIT = 1_000


@dataclass(frozen=True, slots=True)
class CleanupResult:
    deleted: int
    pending: int
    skipped: int
    purged: int


async def run_cleanup_once(
    dependencies: ApplicationDependencies,
    now: datetime,
    limit: int = CLEANUP_SCAN_LIMIT,
) -> CleanupResult:
    service = DeletionService(
        dependencies.application_repository.documents,
        dependencies.blob_store,
        dependencies.chunk_search,
    )
    swept = await service.sweep_pending(now, limit)
    purged = await service.purge_deleted(now, limit)
    return CleanupResult(
        deleted=swept.deleted,
        pending=swept.pending,
        skipped=swept.skipped,
        purged=purged,
    )


def _production_dependencies(
    settings: Settings, chunk_search: ChunkSearch | None = None
) -> ProductionDependencies:
    if chunk_search is None:
        raise RuntimeError(
            "ChunkSearch is not configured; inject the Task 7 Azure AI Search adapter"
        )
    dependencies = create_production_dependencies(settings, chunk_search)
    if not isinstance(dependencies, ProductionDependencies):
        raise TypeError("production dependency factory returned an invalid bundle")
    return dependencies


async def async_main(
    *,
    dependency_factory: Callable[[Settings], ProductionDependencies] = (
        _production_dependencies
    ),
) -> int:
    dependencies: ProductionDependencies | None = None
    totals = CleanupResult(0, 0, 0, 0)
    try:
        settings = Settings()
        dependencies = dependency_factory(settings)
        while True:
            result = await run_cleanup_once(dependencies, datetime.now(UTC))
            totals = CleanupResult(
                totals.deleted + result.deleted,
                totals.pending + result.pending,
                totals.skipped + result.skipped,
                totals.purged + result.purged,
            )
            if result.deleted + result.purged == 0:
                break
        print(
            f"cleanup deleted={totals.deleted} pending={totals.pending} "
            f"skipped={totals.skipped} purged={totals.purged}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - command boundary maps systemic failures to exit status
        print(f"cleanup failed exception={type(exc).__name__}")
        return 1
    finally:
        if dependencies is not None:
            await dependencies.aclose()


def run() -> None:
    raise SystemExit(asyncio.run(async_main()))


__all__ = [
    "CLEANUP_PAGE_SIZE",
    "CLEANUP_SCAN_LIMIT",
    "CleanupResult",
    "async_main",
    "run",
    "run_cleanup_once",
]