# syntax=docker/dockerfile:1
# Shared image for the API, queue worker, and cleanup job. Override the command with
# "worker" or "cleanup" to select the role; the default runs the API.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NATIVE_TLS=true \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
# Optional corporate root CA for the Microsoft package proxy. Empty by default and never
# fails; pass --build-arg EXTRA_CA_CERT="$(cat corp-ca.pem)" on enterprise networks.
ARG EXTRA_CA_CERT=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && if [ -n "$EXTRA_CA_CERT" ]; then \
         printf '%s\n' "$EXTRA_CA_CERT" > /usr/local/share/ca-certificates/enterprise.crt \
         && update-ca-certificates; \
       fi
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

FROM base AS build
WORKDIR /app
# Resolve strictly from the locked graph and the Microsoft enterprise index declared in
# pyproject.toml. --frozen forbids re-resolution, so public PyPI is never contacted.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
USER app
EXPOSE 8000
# Liveness for the API role; compose overrides this for the worker and cleanup roles.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]
CMD ["api"]
