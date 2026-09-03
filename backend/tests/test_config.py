import pytest
from pydantic import ValidationError

from app.core.config import Settings, validate_https_endpoint


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
    assert settings.max_ingestion_backlog == 100
    assert settings.frontend_origin == "http://testserver"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_lifetime_hours", 1),
        ("cookie_name", "x"),
        ("cookie_max_age_seconds", 1),
        ("cookie_http_only", False),
        ("cookie_same_site", "lax"),
        ("cookie_path", "/x"),
        ("cookie_secure", True),
    ],
)
def test_settings_do_not_expose_session_security_invariants(field: str, value: object) -> None:
    settings = Settings.model_validate({field: value})

    assert not hasattr(settings, field)


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
        "max_ingestion_backlog",
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
        ("max_ingestion_backlog", 10_001),
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


@pytest.mark.parametrize("character", ["\u200b", "\u200d", "\ufeff"])
@pytest.mark.parametrize(
    "endpoint_template",
    [
        "{}https://example.com",
        "https://exa{}mple.com",
        "https://example.com/{}",
        "https://example.com{}",
    ],
)
def test_endpoint_rejects_format_characters_before_parsing(
    character: str, endpoint_template: str
) -> None:
    with pytest.raises(ValueError, match="unsafe character"):
        validate_https_endpoint(endpoint_template.format(character))


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.01",
        "https://999.999.999.999",
        "https://1.2.3",
        "https://.127.0.0.1",
        "https://127.0.0.1.",
    ],
)
@pytest.mark.parametrize("field", ["foundry_endpoint", "search_endpoint"])
def test_settings_reject_ambiguous_numeric_ipv4_authorities(field: str, value: str) -> None:
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
        ("https://123.example/", "https://123.example"),
        ("https://node-123.example/", "https://node-123.example"),
        ("https://123node.example/", "https://123node.example"),
    ],
)
def test_settings_accept_valid_dns_and_ip_endpoint_authorities(
    value: str, normalized: str
) -> None:
    settings = Settings.model_validate({"search_endpoint": value})

    assert settings.search_endpoint == normalized


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("http://testserver/", "http://testserver"),
        ("https://EXAMPLE.com:8443/", "https://example.com:8443"),
    ],
)
@pytest.mark.parametrize("app_mode", ["local", "test"])
def test_local_and_test_frontend_origin_is_strictly_normalized(
    app_mode: str, value: str, normalized: str
) -> None:
    settings = Settings.model_validate({"app_mode": app_mode, "frontend_origin": value})

    assert settings.frontend_origin == normalized


@pytest.mark.parametrize(
    "value",
    [
        "http://frontend.example.com",
        "https://user:password@frontend.example.com",
        "https://frontend.example.com/path",
        "https://frontend.example.com?query=1",
        "https://frontend.example.com#fragment",
        "https://frontend.example.com:0",
        "https://frontend.example.com:65536",
        "https://frontend.example.com\u200b",
        "https://frontend.example.com\n",
    ],
)
def test_production_rejects_unsafe_or_non_https_frontend_origins(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"app_mode": "production", "frontend_origin": value})


def test_production_requires_frontend_origin_override() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"app_mode": "production"})


@pytest.mark.parametrize("app_mode", ["local", "test"])
def test_azurite_connection_string_is_permitted_only_outside_production(app_mode: str) -> None:
    settings = Settings.model_validate(
        {"app_mode": app_mode, "azurite_table_connection_string": "UseDevelopmentStorage=true"}
    )
    assert settings.azurite_table_connection_string == "UseDevelopmentStorage=true"


def test_production_rejects_nonempty_azurite_connection_string() -> None:
    with pytest.raises(ValidationError, match="connection string"):
        Settings.model_validate(
            {
                "app_mode": "production",
                "frontend_origin": "https://frontend.example.com",
                "azurite_table_connection_string": "UseDevelopmentStorage=true",
            }
        )
