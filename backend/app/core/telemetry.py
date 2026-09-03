from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.sdk.resources import Resource

LOGGER_NAME = "content_understanding"
_SAFE_RELEASE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "body",
        "content",
        "cookie",
        "document_text",
        "extraction",
        "message",
        "prompt",
        "question",
        "request_body",
        "response_body",
        "sas",
        "set_cookie",
    }
)
_TOKEN_KEY_PARTS = frozenset({"access_token", "api_key", "sas_token", "token"})


class AzureMonitorConfigurator(Protocol):
    def __call__(self, **kwargs: Any) -> None: ...


def _normalized_key_parts(key: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    parts = set(normalized.split("_"))
    parts.add(normalized)
    return parts


def _is_sensitive_key(key: str) -> bool:
    parts = _normalized_key_parts(key)
    if parts & _SENSITIVE_KEY_PARTS:
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return any(token_key in normalized for token_key in _TOKEN_KEY_PARTS)


def _without_url_secrets(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def sanitize_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    """Return telemetry attributes with private payloads and URL secrets removed."""
    sanitized: dict[str, object] = {}
    for key, value in attributes.items():
        if _is_sensitive_key(key):
            continue
        sanitized[key] = _without_url_secrets(value) if isinstance(value, str) else value
    return sanitized


def configure_telemetry(
    service: str,
    release_sha: str,
    app_mode: str,
    environ: Mapping[str, str] | None = None,
    *,
    configure: AzureMonitorConfigurator | None = None,
) -> bool:
    """Enable Azure Monitor only for explicitly configured production processes."""
    environment = os.environ if environ is None else environ
    connection_string = environment.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if app_mode != "production" or not connection_string:
        return False
    if service not in {"api", "worker", "cleanup"}:
        raise ValueError("telemetry service name is invalid")
    safe_release = release_sha if _SAFE_RELEASE_PATTERN.fullmatch(release_sha) else "unknown"
    resource = Resource.create(
        {
            "service.name": f"content-understanding-{service}",
            "service.version": safe_release,
        }
    )
    if configure is None:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure = configure_azure_monitor
    configure(
        connection_string=connection_string,
        logger_name=LOGGER_NAME,
        resource=resource,
        enable_live_metrics=False,
    )
    return True


__all__ = ["configure_telemetry", "sanitize_attributes"]