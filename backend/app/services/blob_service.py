import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BufferedIOBase
from tempfile import SpooledTemporaryFile
from typing import Any, Protocol, cast
from uuid import UUID

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobLeaseClient, BlobServiceClient

from app.core.errors import AppError, TransientArtifactError
from app.domain.protocols import BlobUploadGrant, Clock, DocumentLeaseContext, VerifiedBlobUpload
from app.services.file_validation import MAX_FILE_BYTES, TYPE_MAP, validate_office_package_stream

SAS_CLOCK_SKEW = timedelta(minutes=5)
SAS_LIFETIME = timedelta(minutes=15)
HEADER_BYTES = 16
OFFICE_SPOOL_MEMORY_BYTES = 4 * 1024 * 1024
LEASE_DURATION_SECONDS = 60
LEASE_RENEW_INTERVAL_SECONDS = 30.0
LEASE_ACQUIRE_ATTEMPTS = 5
LEASE_RETRY_BASE_SECONDS = 0.1
LEASE_RETRY_CAP_SECONDS = 2.0
SECURE_RANDOM = secrets.SystemRandom()


class DocumentLeaseBusy(Exception):
    """The document control blob is leased by another operation."""


class DocumentLeaseLost(Exception):
    """The document lease was lost and fenced work must stop."""


class LeaseClientLike(Protocol):
    async def acquire(self, *, lease_duration: int) -> object: ...

    async def renew(self) -> object: ...

    async def release(self) -> object: ...


class RenewableDocumentLease:
    def __init__(self, lease_client: LeaseClientLike) -> None:
        self._lease_client = lease_client
        self._lost = False

    def mark_lost(self) -> None:
        self._lost = True

    def ensure_valid(self) -> None:
        if self._lost:
            raise DocumentLeaseLost


class Download(Protocol):
    async def readall(self) -> bytes: ...

    def chunks(self) -> Any: ...


class BlobClientLike(Protocol):
    url: str

    async def get_blob_properties(self) -> Any: ...

    async def download_blob(
        self, offset: int | None = None, length: int | None = None, **kwargs: Any
    ) -> Download: ...

    async def upload_blob(self, data: bytes, *, overwrite: bool) -> object: ...

    async def delete_blob(self, **kwargs: Any) -> object: ...


class ContainerClientLike(Protocol):
    def list_blobs(self, *, name_starts_with: str) -> AsyncIterator[Any]: ...


class BlobServiceClientLike(Protocol):
    async def get_user_delegation_key(self, start: datetime, expiry: datetime) -> object: ...

    def get_blob_client(self, container: str, blob: str) -> BlobClientLike: ...

    def get_container_client(self, container: str) -> ContainerClientLike: ...


class BlobSasSigner(Protocol):
    async def sign(
        self,
        service_client: BlobServiceClientLike,
        sas_factory: "SasFactory",
        *,
        account_name: str,
        container_name: str,
        blob_name: str,
        permission: BlobSasPermissions,
        start: datetime,
        expiry: datetime,
    ) -> str: ...


UploadGrant = BlobUploadGrant
VerifiedUpload = VerifiedBlobUpload


SasFactory = Callable[..., str]
SpoolFactory = Callable[..., Any]
LeaseFactory = Callable[[BlobClientLike], LeaseClientLike]
Sleep = Callable[[float], object]
Random = Callable[[], float]


class UserDelegationBlobSasSigner:
    async def sign(
        self,
        service_client: BlobServiceClientLike,
        sas_factory: SasFactory,
        *,
        account_name: str,
        container_name: str,
        blob_name: str,
        permission: BlobSasPermissions,
        start: datetime,
        expiry: datetime,
    ) -> str:
        key = await service_client.get_user_delegation_key(start, expiry)
        return sas_factory(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            user_delegation_key=key,
            permission=permission,
            start=start,
            expiry=expiry,
            protocol="https",
        )


class LocalBlobSasSigner:
    """Azurite-only account-key signer; the key is never represented or exposed."""

    def __init__(self, account_key: str) -> None:
        if not account_key:
            raise ValueError("local storage account key is required")
        self._account_key = account_key

    async def sign(
        self,
        service_client: BlobServiceClientLike,
        sas_factory: SasFactory,
        *,
        account_name: str,
        container_name: str,
        blob_name: str,
        permission: BlobSasPermissions,
        start: datetime,
        expiry: datetime,
    ) -> str:
        del service_client
        return sas_factory(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=self._account_key,
            permission=permission,
            start=start,
            expiry=expiry,
            protocol="https,http",
        )


def _azure_lease_factory(blob: BlobClientLike) -> LeaseClientLike:
    return cast(LeaseClientLike, BlobLeaseClient(cast(Any, blob)))


class AzureBlobStore:
    """Azure Blob adapter that never retains or logs generated SAS values."""

    def __init__(
        self,
        account_name: str,
        uploads_container: str,
        clock: Clock,
        *,
        derived_container: str = "derived",
        control_container: str = "control",
        credential: AsyncTokenCredential | None = None,
        service_client: BlobServiceClientLike | None = None,
        sas_factory: SasFactory = generate_blob_sas,
        sas_signer: BlobSasSigner | None = None,
        spool_factory: SpoolFactory = SpooledTemporaryFile,
        office_concurrency: int = 2,
        lease_factory: LeaseFactory = _azure_lease_factory,
        lease_renew_interval: float = LEASE_RENEW_INTERVAL_SECONDS,
        lease_attempts: int = LEASE_ACQUIRE_ATTEMPTS,
        lease_retry_base: float = LEASE_RETRY_BASE_SECONDS,
        lease_retry_cap: float = LEASE_RETRY_CAP_SECONDS,
        lease_sleep: Callable[[float], Any] = asyncio.sleep,
        lease_random: Random = SECURE_RANDOM.random,
        own_credential: bool = False,
        own_service_client: bool = False,
    ) -> None:
        self._account_name = account_name
        self._uploads_container = uploads_container
        self._derived_container = derived_container
        self._control_container = control_container
        self._clock = clock
        self._credential = credential
        self._service_client = service_client
        self._sas_factory = sas_factory
        self._sas_signer = sas_signer or UserDelegationBlobSasSigner()
        self._spool_factory = spool_factory
        self._office_semaphore = asyncio.Semaphore(office_concurrency)
        self._lease_factory = lease_factory
        self._lease_renew_interval = lease_renew_interval
        self._lease_attempts = lease_attempts
        self._lease_retry_base = lease_retry_base
        self._lease_retry_cap = lease_retry_cap
        self._lease_sleep = lease_sleep
        self._lease_random = lease_random
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
        permission = BlobSasPermissions(create=True, write=True)
        sas = await self._sas_signer.sign(
            client,
            self._sas_factory,
            account_name=self._account_name,
            container_name=self._uploads_container,
            blob_name=blob_name,
            permission=permission,
            start=start,
            expiry=expiry,
        )
        blob = client.get_blob_client(self._uploads_container, blob_name)
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
        blob = self._client().get_blob_client(self._uploads_container, blob_name)
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

    async def create_read_url(self, blob_name: str, expires_at: datetime) -> str:
        start = self._clock.now() - SAS_CLOCK_SKEW
        client = self._client()
        key = await client.get_user_delegation_key(start, expires_at)
        sas = self._sas_factory(
            account_name=self._account_name,
            container_name=self._uploads_container,
            blob_name=blob_name,
            user_delegation_key=key,
            permission=BlobSasPermissions(read=True),
            start=start,
            expiry=expires_at,
            protocol="https",
        )
        blob = client.get_blob_client(self._uploads_container, blob_name)
        return f"{blob.url}?{sas}"

    async def read_prefix(self, blob_name: str, length: int) -> bytes:
        download = await self._client().get_blob_client(
            self._uploads_container, blob_name
        ).download_blob(offset=0, length=length)
        return await download.readall()

    async def delete(self, blob_name: str) -> None:
        try:
            await self._client().get_blob_client(
                self._uploads_container, blob_name
            ).delete_blob(delete_snapshots="include")
        except ResourceNotFoundError:
            pass

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

    @staticmethod
    def _control_blob_name(session_key: str, document_id: UUID) -> str:
        if len(session_key) != 64 or any(character not in "0123456789abcdef" for character in session_key):
            raise ValueError("invalid session key")
        return f"control/{session_key}/{document_id}.lock"

    @staticmethod
    def _artifact_prefixes(session_key: str, document_id: UUID) -> tuple[str, str]:
        AzureBlobStore._control_blob_name(session_key, document_id)
        return (
            f"uploads/{session_key}/{document_id}/",
            f"derived/{session_key}/{document_id}/",
        )

    async def _renew_lease(
        self, lease: RenewableDocumentLease, stopped: asyncio.Event
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=self._lease_renew_interval)
                return
            except TimeoutError:
                try:
                    await lease._lease_client.renew()
                except AzureError:
                    lease.mark_lost()
                    return

    def acquire_document_lease(
        self, session_key: str, document_id: UUID
    ) -> DocumentLeaseContext:
        return self._document_lease_context(session_key, document_id)

    @asynccontextmanager
    async def _document_lease_context(
        self, session_key: str, document_id: UUID
    ) -> AsyncIterator[RenewableDocumentLease]:
        control = self._client().get_blob_client(
            self._control_container, self._control_blob_name(session_key, document_id)
        )
        try:
            await control.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        except AzureError:
            raise
        lease_client = self._lease_factory(control)
        for attempt in range(self._lease_attempts):
            try:
                await lease_client.acquire(lease_duration=LEASE_DURATION_SECONDS)
                break
            except (ResourceExistsError, ResourceModifiedError):
                if attempt + 1 == self._lease_attempts:
                    raise DocumentLeaseBusy from None
                exponential = self._lease_retry_base * (2**attempt)
                jittered = exponential * (1.0 + self._lease_random())
                await self._lease_sleep(min(self._lease_retry_cap, jittered))
        else:
            raise DocumentLeaseBusy
        lease = RenewableDocumentLease(lease_client)
        stopped = asyncio.Event()
        renew_task = asyncio.create_task(self._renew_lease(lease, stopped))
        try:
            yield lease
            lease.ensure_valid()
        finally:
            stopped.set()
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            release_task = asyncio.create_task(lease_client.release())
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                await release_task
                raise
            except AzureError:
                lease.mark_lost()

    async def _artifact_names(
        self, session_key: str, document_id: UUID
    ) -> list[tuple[str, str]]:
        names: list[tuple[str, str]] = []
        try:
            for container_name, prefix in zip(
                (self._uploads_container, self._derived_container),
                self._artifact_prefixes(session_key, document_id),
                strict=True,
            ):
                container = self._client().get_container_client(container_name)
                async for item in container.list_blobs(name_starts_with=prefix):
                    name = getattr(item, "name", None)
                    if isinstance(name, str) and name.startswith(prefix):
                        names.append((container_name, name))
        except AzureError:
            raise TransientArtifactError from None
        return names

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool:
        return bool(await self._artifact_names(session_key, document_id))

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None:
        for container_name, name in await self._artifact_names(session_key, document_id):
            try:
                await self._client().get_blob_client(container_name, name).delete_blob(
                    delete_snapshots="include"
                )
            except ResourceNotFoundError:
                pass
            except AzureError:
                raise TransientArtifactError from None

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