"""Structural contract for the container build artifacts.

These assertions validate the Dockerfiles, NGINX config, compose stack, and env template
as text so they run without Docker installed. They do not build or run any image.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DOCKERFILE = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
FRONTEND_DOCKERFILE = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
NGINX_TEMPLATE = (ROOT / "frontend" / "nginx" / "default.conf.template").read_text(
    encoding="utf-8"
)
NGINX_ENTRYPOINT = (ROOT / "frontend" / "nginx" / "entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")


def test_backend_image_is_nonroot_and_uses_locked_uv_without_public_pypi() -> None:
    assert "USER app" in BACKEND_DOCKERFILE
    assert "uv sync --frozen --no-dev" in BACKEND_DOCKERFILE
    assert "pypi.org" not in BACKEND_DOCKERFILE
    assert "--index-url" not in BACKEND_DOCKERFILE
    assert "UV_NATIVE_TLS=true" in BACKEND_DOCKERFILE
    # No environment files or secrets are baked into the image.
    assert "COPY .env" not in BACKEND_DOCKERFILE


def test_backend_image_is_shared_across_api_worker_and_cleanup() -> None:
    assert 'CMD ["api"]' in BACKEND_DOCKERFILE
    assert 'api = "app.main:run"' in PYPROJECT
    assert 'worker = "app.worker:run"' in PYPROJECT
    assert 'cleanup = "app.cleanup:run"' in PYPROJECT


def test_backend_healthcheck_targets_liveness() -> None:
    assert "HEALTHCHECK" in BACKEND_DOCKERFILE
    assert "/health/live" in BACKEND_DOCKERFILE


def test_frontend_is_multistage_nonroot_nginx() -> None:
    assert "AS build" in FRONTEND_DOCKERFILE
    assert "npm ci" in FRONTEND_DOCKERFILE
    assert "npm run build" in FRONTEND_DOCKERFILE
    assert "nginx-unprivileged" in FRONTEND_DOCKERFILE
    assert "HEALTHCHECK" in FRONTEND_DOCKERFILE


def test_nginx_proxies_api_with_sse_buffering_disabled() -> None:
    assert "location /api/" in NGINX_TEMPLATE
    assert "proxy_pass ${API_UPSTREAM}" in NGINX_TEMPLATE
    assert "proxy_ssl_server_name on" in NGINX_TEMPLATE
    assert "proxy_set_header Host $proxy_host" in NGINX_TEMPLATE
    assert "proxy_set_header Host $host" not in NGINX_TEMPLATE
    assert "proxy_buffering off" in NGINX_TEMPLATE
    assert 'X-Accel-Buffering "no"' in NGINX_TEMPLATE


def test_nginx_sets_modest_body_limit_and_security_headers() -> None:
    assert "client_max_body_size 4m" in NGINX_TEMPLATE
    assert "X-Content-Type-Options" in NGINX_TEMPLATE
    assert "X-Frame-Options" in NGINX_TEMPLATE
    assert "Content-Security-Policy" in NGINX_TEMPLATE
    assert "try_files" in NGINX_TEMPLATE


def test_nginx_entrypoint_renders_only_explicit_variables() -> None:
    assert "envsubst '${API_UPSTREAM} ${EXTRA_CONNECT_SRC}'" in NGINX_ENTRYPOINT


def test_compose_runs_the_local_stack_against_azurite() -> None:
    for service in ("azurite:", "api:", "worker:", "frontend:"):
        assert service in COMPOSE
    assert 'command: ["api"]' in COMPOSE
    assert 'command: ["worker"]' in COMPOSE
    assert "APP_MODE: local" in COMPOSE
    # The backend reaches Azurite by its compose hostname.
    assert "azurite:10000" in COMPOSE


def test_env_example_contains_no_real_secrets() -> None:
    lowered = ENV_EXAMPLE.lower()
    assert "-----begin" not in lowered
    for banned in ("client_secret=", "api_key=", "password=", "sas_token="):
        assert banned not in lowered
