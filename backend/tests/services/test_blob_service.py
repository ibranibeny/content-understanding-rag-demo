import asyncio
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from app.core.errors import AppError
from app.services.blob_service import AzureBlobStore, DocumentLeaseBusy, DocumentLeaseLost

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class Download:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def readall(self) -> bytes:
        return self.data

    async def chunks(self):  # type: ignore[no-untyped-def]
        for offset in range(0, len(self.data), 3):
            await asyncio.sleep(0)
            yield self.data[offset : offset + 3]


class ChunkOnlyDownload(Download):
    async def readall(self) -> bytes:
        raise AssertionError("Office validation must not call readall")


class BlobClient:
    def __init__(self, data: bytes = b"%PDF-1.7") -> None:
        self.url = "https://acct.blob.core.windows.net/uploads/path/file.pdf"
        self.data = data
        self.downloads: list[tuple[int | None, int | None, dict[str, object]]] = []
        self.properties = SimpleNamespace(
            etag='"etag"', size=len(data), content_settings=SimpleNamespace(content_type="application/pdf")
        )

    async def get_blob_properties(self) -> Any:
        return self.properties

    async def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: object
    ) -> Download:
        self.downloads.append((offset, length, kwargs))
        end = None if length is None else (offset or 0) + length
        return Download(self.data[offset or 0 : end])


class ChunkOnlyBlobClient(BlobClient):
    async def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: object
    ) -> Download:
        self.downloads.append((offset, length, kwargs))
        end = None if length is None else (offset or 0) + length
        return ChunkOnlyDownload(self.data[offset or 0 : end])


class FailingBlobClient(BlobClient):
    def __init__(
        self,
        *,
        property_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.property_error = property_error
        self.read_error = read_error

    async def get_blob_properties(self) -> Any:
        if self.property_error is not None:
            raise self.property_error
        return await super().get_blob_properties()

    async def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: object
    ) -> Download:
        if self.read_error is not None:
            raise self.read_error
        return await super().download_blob(offset, length, **kwargs)


class BlobService:
    def __init__(self, blob: BlobClient) -> None:
        self.blob = blob
        self.key_times: tuple[datetime, datetime] | None = None
        self.requested: tuple[str, str] | None = None

    async def get_user_delegation_key(self, start: datetime, expiry: datetime) -> object:
        self.key_times = (start, expiry)
        return object()

    def get_blob_client(self, container: str, blob: str) -> BlobClient:
        self.requested = (container, blob)
        return self.blob

    async def close(self) -> None:
        self.close_calls = getattr(self, "close_calls", 0) + 1


async def test_sas_is_https_one_blob_write_create_only_and_short_lived() -> None:
    captured: dict[str, Any] = {}

    def sas_factory(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "sp=cw&spr=https&sig=secret"

    client = BlobService(BlobClient())
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=client, sas_factory=sas_factory
    )

    grant = await store.create_upload("uploads/session/doc/safe.pdf", "application/pdf")

    assert client.requested == ("uploads", "uploads/session/doc/safe.pdf")
    assert client.key_times == (NOW - timedelta(minutes=5), NOW + timedelta(minutes=15))
    assert captured["start"] == NOW - timedelta(minutes=5)
    assert captured["expiry"] == NOW + timedelta(minutes=15)
    assert captured["protocol"] == "https"
    permission = captured["permission"]
    assert permission.create and permission.write
    assert not permission.read and not permission.add and not permission.delete and not permission.tag
    assert str(permission) == "cw"
    assert grant.upload_url.endswith("?sp=cw&spr=https&sig=secret")
    assert grant.expires_at == NOW + timedelta(minutes=15)
    assert grant.required_headers == {"x-ms-blob-type": "BlockBlob"}
    assert "secret" not in repr(store)


async def test_verify_reads_bounded_header_for_non_office_blob() -> None:
    blob = BlobClient(b"%PDF-1.7" + b"x" * 100)
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=BlobService(blob))

    verified = await store.verify_upload(
        "path/file.pdf", '"etag"', len(blob.data), "application/pdf", office=False
    )

    assert verified.header == blob.data[:16]
    assert verified.package is None
    assert blob.downloads[0][:2] == (0, 16)
    assert blob.downloads[0][2]["etag"] == '"etag"'
    assert str(blob.downloads[0][2]["match_condition"]).endswith("IfNotModified")


async def test_verify_streams_office_package_without_retaining_bytes() -> None:
    from tests.services.test_file_validation import DOCX

    blob = ChunkOnlyBlobClient(DOCX)
    blob.properties.content_settings.content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=BlobService(blob))

    verified = await store.verify_upload(
        "path/file.docx",
        '"etag"',
        len(blob.data),
        blob.properties.content_settings.content_type,
        office=True,
    )

    assert verified.package is None
    assert verified.office_summary is not None
    assert "word/document.xml" in verified.office_summary.entry_names
    assert blob.downloads[0][:2] == (0, len(blob.data))
    assert blob.downloads[0][2]["etag"] == '"etag"'


async def test_office_spool_uses_bounded_threshold_and_closes() -> None:
    from tests.services.test_file_validation import DOCX

    created: list[object] = []

    def spool_factory(*, max_size: int, mode: str):  # type: ignore[no-untyped-def]
        spool = SpooledTemporaryFile(max_size=max_size, mode=mode)  # noqa: SIM115
        created.append(spool)
        assert max_size == 4 * 1024 * 1024
        return spool

    blob = ChunkOnlyBlobClient(DOCX)
    blob.properties.content_settings.content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=BlobService(blob), spool_factory=spool_factory
    )
    await store.verify_upload(
        "a.docx", '"etag"', len(DOCX), blob.properties.content_settings.content_type, office=True
    )
    assert created and created[0].closed  # type: ignore[union-attr]


async def test_office_validation_respects_concurrency_limit() -> None:
    from tests.services.test_file_validation import DOCX

    active = 0
    peak = 0

    class MeasuringDownload(ChunkOnlyDownload):
        async def chunks(self):  # type: ignore[no-untyped-def]
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                yield self.data
            finally:
                active -= 1

    class MeasuringBlob(ChunkOnlyBlobClient):
        async def download_blob(self, *args: object, **kwargs: object) -> Download:
            return MeasuringDownload(self.data)

    blob = MeasuringBlob(DOCX)
    blob.properties.content_settings.content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=BlobService(blob), office_concurrency=2
    )
    await asyncio.gather(*[
        store.verify_upload(
            "a.docx", '"etag"', len(DOCX), blob.properties.content_settings.content_type, office=True
        )
        for _ in range(5)
    ])
    assert peak == 2


async def test_office_semaphore_limits_download_start() -> None:
    from tests.services.test_file_validation import DOCX

    active = 0
    peak = 0

    class MeasuringBlob(ChunkOnlyBlobClient):
        async def download_blob(self, *args: object, **kwargs: object) -> Download:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return ChunkOnlyDownload(self.data)
            finally:
                active -= 1

    blob = MeasuringBlob(DOCX)
    blob.properties.content_settings.content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=BlobService(blob), office_concurrency=2
    )
    await asyncio.gather(*[
        store.verify_upload(
            "a.docx", '"etag"', len(DOCX), blob.properties.content_settings.content_type, office=True
        )
        for _ in range(5)
    ])
    assert peak == 2


async def test_office_cancellation_closes_spool() -> None:
    created: list[object] = []

    def spool_factory(*, max_size: int, mode: str):  # type: ignore[no-untyped-def]
        spool = SpooledTemporaryFile(max_size=max_size, mode=mode)  # noqa: SIM115
        created.append(spool)
        return spool

    class BlockingDownload(ChunkOnlyDownload):
        async def chunks(self):  # type: ignore[no-untyped-def]
            yield b"PK\x03\x04"
            await asyncio.Future()

    class BlockingBlob(ChunkOnlyBlobClient):
        async def download_blob(self, *args: object, **kwargs: object) -> Download:
            return BlockingDownload(self.data)

    blob = BlockingBlob(b"PK\x03\x04")
    blob.properties.content_settings.content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=BlobService(blob), spool_factory=spool_factory
    )
    task = asyncio.create_task(store.verify_upload(
        "a.docx", '"etag"', 4, blob.properties.content_settings.content_type, office=True
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created and created[0].closed  # type: ignore[union-attr]


async def test_injected_resources_are_not_closed_without_ownership_transfer() -> None:
    service = BlobService(BlobClient())
    credential = SimpleNamespace(close_calls=0)

    async def close_credential() -> None:
        credential.close_calls += 1

    credential.close = close_credential
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=service, credential=credential
    )
    await store.aclose()
    await store.aclose()
    assert getattr(service, "close_calls", 0) == 0
    assert credential.close_calls == 0


async def test_owned_resources_close_exactly_once() -> None:
    service = BlobService(BlobClient())
    credential = SimpleNamespace(close_calls=0)

    async def close_credential() -> None:
        credential.close_calls += 1

    credential.close = close_credential
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=service, credential=credential,
        own_service_client=True, own_credential=True,
    )
    await store.aclose()
    await store.aclose()
    assert service.close_calls == 1
    assert credential.close_calls == 1


async def test_close_failure_still_closes_credential_and_can_retry_client() -> None:
    class FailsOnceService(BlobService):
        async def close(self) -> None:
            self.close_calls = getattr(self, "close_calls", 0) + 1
            if self.close_calls == 1:
                raise RuntimeError("close failed")

    service = FailsOnceService(BlobClient())
    credential = SimpleNamespace(close_calls=0)

    async def close_credential() -> None:
        credential.close_calls += 1

    credential.close = close_credential
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=service, credential=credential,
        own_service_client=True, own_credential=True,
    )
    with pytest.raises(RuntimeError):
        await store.aclose()
    assert credential.close_calls == 1
    await store.aclose()
    assert service.close_calls == 2
    assert credential.close_calls == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("etag", '"other"', "upload_etag_mismatch"),
        ("size", 999, "upload_size_mismatch"),
        ("content_type", "image/png", "upload_content_type_mismatch"),
    ],
)
async def test_verify_rejects_property_mismatches(field: str, value: object, code: str) -> None:
    blob = BlobClient()
    setattr(blob.properties, field, value) if field != "content_type" else setattr(
        blob.properties.content_settings, "content_type", value
    )
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=BlobService(blob))

    with pytest.raises(AppError) as caught:
        await store.verify_upload("file.pdf", '"etag"', len(blob.data), "application/pdf", office=False)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("blob", "code", "status", "retryable"),
    [
        (
            FailingBlobClient(property_error=ResourceNotFoundError("missing")),
            "upload_not_found",
            404,
            False,
        ),
        (
            FailingBlobClient(property_error=AzureError("properties")),
            "upload_verification_failed",
            503,
            True,
        ),
        (FailingBlobClient(read_error=AzureError("changed")), "upload_read_failed", 503, True),
    ],
)
async def test_verify_maps_blob_failures_to_stable_errors(
    blob: BlobClient, code: str, status: int, retryable: bool
) -> None:
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=BlobService(blob))
    with pytest.raises(AppError) as caught:
        await store.verify_upload("file.pdf", '"etag"', len(blob.data), "application/pdf", office=False)
    assert (caught.value.code, caught.value.status_code, caught.value.retryable) == (
        code,
        status,
        retryable,
    )


async def test_verify_rejects_short_conditional_read() -> None:
    blob = BlobClient(b"%PDF-1.7" + b"x" * 100)

    async def short_download(
        offset: int | None = None, length: int | None = None, **kwargs: object
    ) -> Download:
        del offset, length, kwargs
        return Download(b"%PDF-")

    blob.download_blob = short_download  # type: ignore[method-assign]
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=BlobService(blob))
    with pytest.raises(AppError) as caught:
        await store.verify_upload("file.pdf", '"etag"', len(blob.data), "application/pdf", office=False)
    assert caught.value.code == "upload_size_mismatch"


class LeaseBlob(BlobClient):
    def __init__(self) -> None:
        super().__init__(b"")
        self.upload_calls: list[tuple[bytes, bool]] = []
        self.delete_calls = 0

    async def upload_blob(self, data: bytes, *, overwrite: bool) -> None:
        self.upload_calls.append((data, overwrite))

    async def delete_blob(self, **kwargs: object) -> None:
        del kwargs
        self.delete_calls += 1


class LeaseClient:
    def __init__(self) -> None:
        self.acquire_calls: list[int] = []
        self.renew_calls = 0
        self.release_calls = 0
        self.acquire_error: Exception | None = None
        self.renew_error: Exception | None = None
        self.release_started = asyncio.Event()
        self.release_continue = asyncio.Event()
        self.block_release = False

    async def acquire(self, *, lease_duration: int) -> None:
        self.acquire_calls.append(lease_duration)
        if self.acquire_error:
            raise self.acquire_error

    async def renew(self) -> None:
        self.renew_calls += 1
        if self.renew_error:
            raise self.renew_error

    async def release(self) -> None:
        self.release_calls += 1
        self.release_started.set()
        if self.block_release:
            await self.release_continue.wait()


class PrefixService(BlobService):
    def __init__(self, control: LeaseBlob, names: list[str]) -> None:
        super().__init__(control)
        self.control = control
        self.names = names
        self.requested_names: list[str] = []

    def get_blob_client(self, container: str, blob: str) -> LeaseBlob:
        del container
        self.requested_names.append(blob)
        return self.control

    def get_container_client(self, container: str):  # type: ignore[no-untyped-def]
        del container
        service = self

        class Container:
            def list_blobs(self, *, name_starts_with: str):  # type: ignore[no-untyped-def]
                async def values():  # type: ignore[no-untyped-def]
                    for name in service.names:
                        if name.startswith(name_starts_with):
                            yield SimpleNamespace(name=name)
                return values()

        return Container()


async def test_control_lease_uses_server_derived_zero_byte_blob_and_renews() -> None:
    control = LeaseBlob()
    lease_client = LeaseClient()
    service = PrefixService(control, [])
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=service,
        lease_factory=lambda blob: lease_client, lease_renew_interval=0.01,
    )

    async with store.acquire_document_lease("a" * 64, UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")) as lease:
        await asyncio.sleep(0.03)
        lease.ensure_valid()

    assert service.requested_names[0] == (
        "control/" + "a" * 64 + "/9f4b8484-9f6b-44f2-b4d4-e5e7687c80df.lock"
    )
    assert control.upload_calls == [(b"", False)]
    assert lease_client.acquire_calls == [60]
    assert lease_client.renew_calls >= 1
    assert lease_client.release_calls == 1


async def test_control_blob_already_existing_is_safe_and_busy_lease_is_typed() -> None:
    control = LeaseBlob()

    async def exists(data: bytes, *, overwrite: bool) -> None:
        del data, overwrite
        raise ResourceExistsError("exists")

    control.upload_blob = exists  # type: ignore[method-assign]
    lease_client = LeaseClient()
    lease_client.acquire_error = ResourceModifiedError("busy")
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=PrefixService(control, []),
        lease_factory=lambda blob: lease_client,
    )
    with pytest.raises(DocumentLeaseBusy):
        async with store.acquire_document_lease("a" * 64, UUID(int=1)):
            raise AssertionError("unreachable")


async def test_renewal_loss_is_observable_and_still_releases() -> None:
    control = LeaseBlob()
    lease_client = LeaseClient()
    lease_client.renew_error = ResourceModifiedError("lost private lease")
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=PrefixService(control, []),
        lease_factory=lambda blob: lease_client, lease_renew_interval=0.01,
    )
    with pytest.raises(DocumentLeaseLost):
        async with store.acquire_document_lease("a" * 64, UUID(int=1)) as lease:
            await asyncio.sleep(0.02)
            lease.ensure_valid()
    assert lease_client.release_calls == 1


async def test_cancellation_during_body_waits_for_release() -> None:
    control = LeaseBlob()
    lease_client = LeaseClient()
    lease_client.block_release = True
    store = AzureBlobStore(
        "acct", "uploads", Clock(), service_client=PrefixService(control, []),
        lease_factory=lambda blob: lease_client,
    )

    async def work() -> None:
        async with store.acquire_document_lease("a" * 64, UUID(int=1)):
            await asyncio.Future()

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    task.cancel()
    await lease_client.release_started.wait()
    assert not task.done()
    lease_client.release_continue.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease_client.release_calls == 1


async def test_document_artifacts_use_only_server_prefixes_and_missing_is_safe() -> None:
    document_id = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
    session = "a" * 64
    names = [
        f"uploads/{session}/{document_id}/private.pdf",
        f"derived/{session}/{document_id}/content.md",
        f"uploads/{'b' * 64}/{document_id}/other.pdf",
    ]
    blob = LeaseBlob()
    service = PrefixService(blob, names)
    store = AzureBlobStore("acct", "uploads", Clock(), service_client=service)

    assert await store.document_artifacts_exist(session, document_id)
    await store.delete_document_artifacts(session, document_id)

    assert service.requested_names[-2:] == names[:2]
    assert blob.delete_calls == 2