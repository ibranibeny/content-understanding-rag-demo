from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from app.core.errors import AppError
from app.domain.protocols import BlobUploadGrant, Clock, VerifiedBlobUpload

SAS_CLOCK_SKEW = timedelta(minutes=5)
SAS_LIFETIME = timedelta(minutes=15)
HEADER_BYTES = 16


class Download(Protocol):
    async def readall(self) -> bytes: ...


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
    ) -> None:
        self._account_name = account_name
        self._container_name = container_name
        self._clock = clock
        self._credential = credential
        self._service_client = service_client
        self._sas_factory = sas_factory

    def _client(self) -> BlobServiceClientLike:
        if self._service_client is None:
            self._credential = self._credential or DefaultAzureCredential()
            endpoint = f"https://{self._account_name}.blob.core.windows.net"
            self._service_client = cast(
                BlobServiceClientLike,
                BlobServiceClient(account_url=endpoint, credential=self._credential),
            )
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
            package=content if office else None,
        )