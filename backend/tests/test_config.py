import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_defaults_lock_required_models_and_limits() -> None:
    settings = Settings.model_validate(
        {"foundry_endpoint": "https://demo.services.ai.azure.com"}
    )

    assert settings.chat_deployment == "gpt-5"
    assert settings.embedding_deployment == "text-embedding-3-large"
    assert settings.embedding_dimensions == 3072
    assert settings.max_file_bytes == 100 * 1024 * 1024
    assert settings.max_documents == 5
    assert settings.max_session_bytes == 500 * 1024 * 1024
    assert settings.max_questions_per_hour == 30
    assert settings.session_lifetime_hours == 24


@pytest.mark.parametrize(
    ("override", "value"),
    [("chat_deployment", "gpt-4.1"), ("embedding_dimensions", 1536)],
)
def test_settings_reject_unsupported_model_contracts(override: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({override: value})


def test_settings_accept_environment_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com")
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "workshopstorage")

    settings = Settings()

    assert settings.foundry_endpoint == "https://example.services.ai.azure.com"
    assert settings.storage_account_name == "workshopstorage"
    assert not hasattr(settings, "api_key")


@pytest.mark.parametrize(
    "field",
    [
        "max_file_bytes",
        "max_documents",
        "max_session_bytes",
        "max_questions_per_hour",
        "session_lifetime_hours",
        "cookie_max_age_seconds",
        "embedding_dimensions",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_settings_reject_nonpositive_counts_and_durations(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_file_bytes", 100 * 1024 * 1024 + 1),
        ("max_documents", 6),
        ("max_session_bytes", 500 * 1024 * 1024 + 1),
        ("max_questions_per_hour", 31),
        ("session_lifetime_hours", 25),
        ("cookie_max_age_seconds", 86401),
    ],
)
def test_settings_reject_values_above_workshop_contract_maxima(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


@pytest.mark.parametrize(
    "value",
    [
        "http://example.services.ai.azure.com",
        "HTTPS://example.services.ai.azure.com",
        "https://user:password@example.services.ai.azure.com",
        "not-a-url",
        "https:///missing-host",
        "https://%",
        "https://.",
        "https://example.com\\evil",
        "https://example..com",
        "https://-example.com",
        "https://example-.com",
        "https://\u200d.example",
        f"https://{'a' * 64}.example",
        f"https://{'a.' * 126}a.example",
        "https://example.com:0",
        "https://example.com:",
        "https://example.com:65536",
        "https://example.com\t",
        "https://example.com\n",
        "https://example.com\x7f",
        "https://example.com ",
        "https://example.services.ai.azure.com?api-version=1",
        "https://example.services.ai.azure.com#fragment",
        "https://example.services.ai.azure.com/service-path",
    ],
)
@pytest.mark.parametrize("field", ["foundry_endpoint", "search_endpoint"])
def test_settings_reject_invalid_azure_service_endpoints(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


@pytest.mark.parametrize("field", ["foundry_endpoint", "search_endpoint"])
def test_settings_normalize_azure_service_endpoint_trailing_slash(field: str) -> None:
    settings = Settings.model_validate({field: "https://example.services.ai.azure.com/"})

    assert getattr(settings, field) == "https://example.services.ai.azure.com"


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("https://EXAMPLE.com:443/", "https://example.com:443"),
        ("https://127.0.0.1/", "https://127.0.0.1"),
        ("https://[2001:db8::1]:8443/", "https://[2001:db8::1]:8443"),
        ("https://b\u00fccher.example/", "https://xn--bcher-kva.example"),
    ],
)
def test_settings_accept_valid_dns_and_ip_endpoint_authorities(
    value: str, normalized: str
) -> None:
    settings = Settings.model_validate({"search_endpoint": value})

    assert settings.search_endpoint == normalized
