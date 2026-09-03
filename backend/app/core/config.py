import ipaddress
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _normalize_dns_hostname(hostname: str) -> str:
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("service endpoint hostname has invalid IDNA") from exc

    if len(ascii_hostname) > 253:
        raise ValueError("service endpoint hostname is too long")

    labels = ascii_hostname.split(".")
    if any(not label or DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise ValueError("service endpoint hostname has an invalid DNS label")

    for label in labels:
        if label.startswith("xn--"):
            try:
                decoded = label.encode("ascii").decode("idna")
                canonical = decoded.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError("service endpoint hostname has invalid IDNA") from exc
            if canonical != label:
                raise ValueError("service endpoint hostname has invalid IDNA")

    return ascii_hostname


def validate_https_endpoint(value: object) -> str:
    """Validate and normalize a root HTTPS service endpoint."""
    if not isinstance(value, str) or not value:
        raise ValueError("service endpoint must be a nonempty string")
    if any(
        character == "\\"
        or character.isspace()
        or ord(character) < 32
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    ):
        raise ValueError("service endpoint contains an unsafe character")
    if not value.startswith("https://"):
        raise ValueError("service endpoint must use the exact https scheme")

    try:
        endpoint = urlsplit(value)
        hostname = endpoint.hostname
        port = endpoint.port
    except ValueError as exc:
        raise ValueError("service endpoint is malformed") from exc

    if endpoint.scheme != "https" or not hostname or not endpoint.netloc:
        raise ValueError("service endpoint must be an absolute HTTPS URL")
    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError("service endpoint must not contain credentials")
    if endpoint.netloc.endswith(":"):
        raise ValueError("service endpoint port is malformed")
    if port is not None and port < 1:
        raise ValueError("service endpoint port must be between 1 and 65535")
    if endpoint.query or endpoint.fragment:
        raise ValueError("service endpoint must not contain a query or fragment")
    if endpoint.path not in ("", "/"):
        raise ValueError("service endpoint must be a root URL")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", hostname):
            raise ValueError("service endpoint hostname is not a valid IPv4 address")
        normalized_host = _normalize_dns_hostname(hostname)
    else:
        normalized_host = f"[{address.compressed}]" if address.version == 6 else address.compressed

    netloc = f"{normalized_host}:{port}" if port is not None else normalized_host
    return urlunsplit(("https", netloc, "", "", ""))


HttpsEndpoint = Annotated[str, BeforeValidator(validate_https_endpoint)]


class Settings(BaseSettings):
    """Keyless application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )

    storage_account_name: str = Field(
        default="devstoreaccount1", validation_alias="STORAGE_ACCOUNT_NAME"
    )
    uploads_container: str = Field(default="uploads", validation_alias="UPLOADS_CONTAINER")
    derived_container: str = Field(default="derived", validation_alias="DERIVED_CONTAINER")
    control_container: str = Field(default="control", validation_alias="CONTROL_CONTAINER")
    ingestion_queue: str = Field(default="ingestion", validation_alias="INGESTION_QUEUE")
    content_result_cleanup_queue: str = Field(
        default="cu-result-cleanup", validation_alias="CONTENT_RESULT_CLEANUP_QUEUE"
    )
    ingestion_poison_queue: str = Field(
        default="ingestion-poison", validation_alias="INGESTION_POISON_QUEUE"
    )
    table_name: str = Field(default="workshop", validation_alias="TABLE_NAME")

    search_endpoint: HttpsEndpoint = Field(
        default="https://localhost", validation_alias="SEARCH_ENDPOINT"
    )
    search_index_name: str = Field(
        default="document-chunks", validation_alias="SEARCH_INDEX_NAME"
    )
    foundry_endpoint: HttpsEndpoint = Field(
        default="https://demo.services.ai.azure.com", validation_alias="FOUNDRY_ENDPOINT"
    )
    analyzer_router_id: str = Field(
        default="business-document-router", validation_alias="ANALYZER_ROUTER_ID"
    )
    chat_deployment: str = Field(default="gpt-5", validation_alias="CHAT_DEPLOYMENT")
    embedding_deployment: str = Field(
        default="text-embedding-3-large", validation_alias="EMBEDDING_DEPLOYMENT"
    )
    embedding_dimensions: int = Field(
        default=3072, gt=0, validation_alias="EMBEDDING_DIMENSIONS"
    )

    cookie_name: str = Field(default="cu_session", validation_alias="COOKIE_NAME")
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")
    cookie_http_only: bool = Field(default=True, validation_alias="COOKIE_HTTP_ONLY")
    cookie_same_site: Literal["strict"] = Field(
        default="strict", validation_alias="COOKIE_SAME_SITE"
    )
    cookie_path: str = Field(default="/", validation_alias="COOKIE_PATH")
    cookie_max_age_seconds: int = Field(
        default=86400, gt=0, le=86400, validation_alias="COOKIE_MAX_AGE_SECONDS"
    )

    max_file_bytes: int = Field(
        default=100 * 1024 * 1024,
        gt=0,
        le=100 * 1024 * 1024,
        validation_alias="MAX_FILE_BYTES",
    )
    max_documents: int = Field(default=5, gt=0, le=5, validation_alias="MAX_DOCUMENTS")
    max_session_bytes: int = Field(
        default=500 * 1024 * 1024,
        gt=0,
        le=500 * 1024 * 1024,
        validation_alias="MAX_SESSION_BYTES",
    )
    max_questions_per_hour: int = Field(
        default=30, gt=0, le=30, validation_alias="MAX_QUESTIONS_PER_HOUR"
    )
    session_lifetime_hours: int = Field(
        default=24, gt=0, le=24, validation_alias="SESSION_LIFETIME_HOURS"
    )

    release_sha: str = Field(default="local", validation_alias="RELEASE_SHA")
    app_mode: Literal["local", "test", "production"] = Field(
        default="local", validation_alias="APP_MODE"
    )

    @field_validator("chat_deployment")
    @classmethod
    def require_gpt_5(cls, value: str) -> str:
        if value != "gpt-5":
            raise ValueError("chat deployment must be gpt-5")
        return value

    @field_validator("embedding_dimensions")
    @classmethod
    def require_embedding_dimensions(cls, value: int) -> int:
        if value != 3072:
            raise ValueError("embedding dimensions must be 3072")
        return value
