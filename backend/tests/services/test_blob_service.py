import asyncio
from datetime import UTC, datetime, timedelta
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import AzureError, ResourceNotFoundError

from app.core.errors import AppError
from app.services.blob_service import AzureBlobStore

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