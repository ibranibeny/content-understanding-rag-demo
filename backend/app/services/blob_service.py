import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from io import BufferedIOBase
from tempfile import SpooledTemporaryFile
from typing import Any, Protocol, cast

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from app.core.errors import AppError
from app.domain.protocols import BlobUploadGrant, Clock, VerifiedBlobUpload
from app.services.file_validation import MAX_FILE_BYTES, TYPE_MAP, validate_office_package_stream

SAS_CLOCK_SKEW = timedelta(minutes=5)
SAS_LIFETIME = timedelta(minutes=15)
HEADER_BYTES = 16
OFFICE_SPOOL_MEMORY_BYTES = 4 * 1024 * 1024


class Download(Protocol):
    async def readall(self) -> bytes: ...

    def chunks(self) -> Any: ...


class BlobClientLike(Protocol):
    url: str

    async def get_blob_properties(self) -> Any: ...

    async def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: Any
    ) -> Download: ...


class BlobServiceClientLike(Protocol):
    async def get_user_delegation_key(self, start: datetime, expiry: datetime) -> object: ...

    def get_blob_client(self, container: str, blob: str) -> BlobClientLike: ...


UploadGrant = BlobUploadGrant
VerifiedUpload = VerifiedBlobUpload


SasFactory = Callable[..., str]
SpoolFactory = Callable[..., Any]


class AzureBlobStore:
    """Azure Blob adapter that never retains or logs generated SAS values."""

    def __init__(
        self,
        account_name: str,
        container_name: str,
        clock: Clock,
        *,
        credential: AsyncTokenCredential | None = None,
        service_client: BlobServiceClientLike | None = None,
        sas_factory: SasFactory = generate_blob_sas,
        spool_factory: SpoolFactory = SpooledTemporaryFile,
        office_concurrency: int = 2,
        own_credential: bool = False,
        own_service_client: bool = False,
    ) -> None:
        self._account_name = account_name
        self._container_name = container_name
        self._clock = clock
        self._credential = credential
        self._service_client = service_client
        self._sas_factory = sas_factory
        self._spool_factory = spool_factory
        self._office_semaphore = asyncio.Semaphore(office_concurrency)
        self._owns_credential = own_credential
        self._owns_service_client = own_service_client
        self._service_client_closed = False
        self._credential_closed = False

    def _client(self) -> BlobServiceClientLike:
        if self._service_client is None:
            if self._credential is None:
                self._credential = DefaultAzureCredential()
                self._owns_credential = True
            endpoint = f"https://{self._account_name}.blob.core.windows.net"
            self._service_client = cast(
                BlobServiceClientLike,
                BlobServiceClient(account_url=endpoint, credential=self._credential),
            )
            self._owns_service_client = True
        return self._service_client

    async def create_upload(self, blob_name: str, content_type: str) -> UploadGrant:
        del content_type
        now = self._clock.now()
        start = now - SAS_CLOCK_SKEW
        expiry = now + SAS_LIFETIME
        client = self._client()
        key = await client.get_user_delegation_key(start, expiry)
        permission = BlobSasPermissions(create=True, write=True)
        sas = self._sas_factory(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=blob_name,
            user_delegation_key=key,
            permission=permission,
            start=start,
            expiry=expiry,
            protocol="https",
        )
        blob = client.get_blob_client(self._container_name, blob_name)
        return UploadGrant(
            upload_url=f"{blob.url}?{sas}",
            expires_at=expiry,
            required_headers={"x-ms-blob-type": "BlockBlob"},
        )

    async def verify_upload(
        self,
        blob_name: str,
        expected_etag: str,
        expected_size: int,
        expected_content_type: str,
        *,
        office: bool,
    ) -> VerifiedUpload:
        blob = self._client().get_blob_client(self._container_name, blob_name)
        try:
            properties = await blob.get_blob_properties()
        except ResourceNotFoundError:
            raise AppError("upload_not_found", 404, "The uploaded file was not found.", False) from None
        except AzureError:
            raise AppError(
                "upload_verification_failed",
                503,
                "The uploaded file could not be verified.",
                True,
            ) from None
        if str(properties.etag) != expected_etag:
            raise AppError("upload_etag_mismatch", 409, "The upload ETag does not match.", False)
        if int(properties.size) != expected_size:
            raise AppError("upload_size_mismatch", 400, "The uploaded file size does not match.", False)
        content_type = properties.content_settings.content_type
        if content_type != expected_content_type:
            raise AppError(
                "upload_content_type_mismatch",
                400,
                "The uploaded content type does not match.",
                False,
            )
        length = expected_size if office else min(expected_size, HEADER_BYTES)
        try:
            if office:
                async with self._office_semaphore:
                    download = await blob.download_blob(
                        offset=0,
                        length=length,
                        etag=expected_etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                    return await self._verify_office(
                        download, expected_size, expected_content_type
                    )
            download = await blob.download_blob(
                offset=0,
                length=length,
                etag=expected_etag,
                match_condition=MatchConditions.IfNotModified,
            )
            content = await download.readall()
        except AzureError:
            raise AppError("upload_read_failed", 503, "The uploaded file could not be read.", True) from None
        if len(content) != length:
            raise AppError("upload_size_mismatch", 400, "The uploaded file size does not match.", False)
        return VerifiedUpload(
            header=content[:HEADER_BYTES],
        )

    async def _verify_office(
        self, download: Download, expected_size: int, expected_content_type: str
    ) -> VerifiedUpload:
        if expected_size > MAX_FILE_BYTES:
            raise AppError("upload_size_mismatch", 400, "The uploaded file size does not match.", False)
        required_entry = next(
            required for mime, _, required in TYPE_MAP.values() if mime == expected_content_type
        )
        assert required_entry is not None
        with self._spool_factory(max_size=OFFICE_SPOOL_MEMORY_BYTES, mode="w+b") as spool:
            total = 0
            header = bytearray()
            async for chunk in download.chunks():
                total += len(chunk)
                if total > expected_size or total > MAX_FILE_BYTES:
                    raise AppError(
                        "upload_size_mismatch", 400, "The uploaded file size does not match.", False
                    )
                if len(header) < HEADER_BYTES:
                    header.extend(chunk[: HEADER_BYTES - len(header)])
                spool.write(chunk)
            if total != expected_size:
                raise AppError(
                    "upload_size_mismatch", 400, "The uploaded file size does not match.", False
                )
            spool.seek(0)
            summary = validate_office_package_stream(
                cast(BufferedIOBase, spool), required_entry
            )
            return VerifiedUpload(header=bytes(header), office_summary=summary)

    async def aclose(self) -> None:
        if (
            self._owns_service_client
            and not self._service_client_closed
            and self._service_client is not None
        ):
            try:
                await self._service_client.close()  # type: ignore[attr-defined]
                self._service_client_closed = True
            finally:
                if (
                    self._owns_credential
                    and not self._credential_closed
                    and self._credential is not None
                ):
                    await self._credential.close()
                    self._credential_closed = True
            return
        if (
            self._owns_credential
            and not self._credential_closed
            and self._credential is not None
        ):
                await self._credential.close()
                self._credential_closed = True