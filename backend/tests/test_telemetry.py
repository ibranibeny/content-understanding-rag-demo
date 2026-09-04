from collections.abc import Callable
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import (
    TelemetryLogRecordRedactor,
    TelemetrySpanRedactor,
    configure_telemetry,
    sanitize_attributes,
)


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("http.request.header.cookie", "cu_session=raw"),
        ("storage.sas_token", "SAS_TOKEN"),
        ("document.content", "full document text"),
        ("gen_ai.prompt", "Ignore previous instructions"),
    ],
)
def test_sensitive_values_are_removed(key: str, secret: str) -> None:
    sanitized = sanitize_attributes({key: secret, "document.id": "safe-id"})

    assert key not in sanitized
    assert secret not in sanitized.values()
    assert sanitized["document.id"] == "safe-id"


def test_url_query_and_fragment_are_removed() -> None:
    sanitized = sanitize_attributes(
        {"url.full": "https://blob.example/uploads/a.pdf?sp=rw&sig=SECRET#fragment"}
    )

    assert sanitized == {"url.full": "https://blob.example/uploads/a.pdf"}


def test_exported_span_attributes_are_sanitized() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(TelemetrySpanRedactor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with provider.get_tracer(__name__).start_as_current_span(
        "upload",
        attributes={
            "http.request.header.cookie": "cu_session=raw",
            "url.full": "https://blob.example/a.pdf?sig=SECRET#fragment",
            "document.id": "safe-id",
        },
    ):
        pass

    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes == {
        "url.full": "https://blob.example/a.pdf",
        "document.id": "safe-id",
    }


def test_log_record_attributes_are_sanitized() -> None:
    record = type(
        "ReadWriteRecord",
        (),
        {
            "log_record": type(
                "Record",
                (),
                {
                    "attributes": {
                        "authorization": "Bearer SECRET",
                        "url.full": "https://example.test/path?token=SECRET",
                        "document.id": "safe-id",
                    }
                },
            )()
        },
    )()

    TelemetryLogRecordRedactor().on_emit(cast(Any, record))

    assert record.log_record.attributes == {
        "url.full": "https://example.test/path",
        "document.id": "safe-id",
    }


def test_telemetry_is_disabled_when_connection_string_is_unset_or_local() -> None:
    calls: list[dict[str, Any]] = []
    configure: Callable[..., None] = lambda **kwargs: calls.append(kwargs)

    assert not configure_telemetry("api", "abc123", "production", {}, configure=configure)
    assert not configure_telemetry(
        "api",
        "abc123",
        "local",
        {"APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=secret"},
        configure=configure,
    )
    assert calls == []


def test_telemetry_uses_safe_service_resource_attributes() -> None:
    calls: list[dict[str, Any]] = []

    enabled = configure_telemetry(
        "worker",
        "abc123",
        "production",
        {"APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=secret"},
        configure=lambda **kwargs: calls.append(kwargs),
    )

    assert enabled
    assert len(calls) == 1
    call = calls[0]
    assert call["connection_string"] == "InstrumentationKey=secret"
    assert call["logger_name"] == "content_understanding"
    assert call["enable_live_metrics"] is False
    assert len(call["span_processors"]) == 1
    assert isinstance(call["span_processors"][0], TelemetrySpanRedactor)
    assert len(call["log_record_processors"]) == 1
    assert isinstance(call["log_record_processors"][0], TelemetryLogRecordRedactor)
    attributes = call["resource"].attributes
    assert attributes["service.name"] == "content-understanding-worker"
    assert attributes["service.version"] == "abc123"
    assert "InstrumentationKey=secret" not in attributes.values()


@pytest.mark.parametrize(
    ("module_name", "service_name"),
    [
        ("app.main", "api"),
        ("app.worker", "worker"),
        ("app.cleanup", "cleanup"),
    ],
)
def test_process_entry_points_initialize_telemetry(
    module_name: str,
    service_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = __import__(module_name, fromlist=["run"])
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        module,
        "configure_telemetry",
        lambda service, release, mode: calls.append((service, release, mode)),
    )
    monkeypatch.setenv("RELEASE_SHA", "release-1")
    monkeypatch.setenv("APP_MODE", "test")

    if module_name == "app.main":
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", type("U", (), {
            "run": staticmethod(lambda *args, **kwargs: None)
        }))
        module.run()
    elif module_name == "app.worker":
        monkeypatch.setattr(module.asyncio, "run", lambda coroutine: coroutine.close())
        module.run()
    else:
        def close_coroutine(coroutine: Any) -> int:
            coroutine.close()
            return 0

        monkeypatch.setattr(module.asyncio, "run", close_coroutine)
        with pytest.raises(SystemExit) as exit_info:
            module.run()
        assert exit_info.value.code == 0

    assert calls == [(service_name, "release-1", "test")]