# Content Understanding Meets GitHub Copilot: Build and Ship an Intelligent Document RAG App

This repository contains a public workshop application for exploring document intelligence and retrieval-augmented generation. The application runtime model is GPT-5.

## Current status

The application is implemented: anonymous sessions, direct-to-Blob upload, Content Understanding
extraction, chunking/embeddings, Azure AI Search retrieval, and grounded GPT-5 streaming chat, with a
containerized local stack. Azure infrastructure (Bicep), CI/CD, and public deployment are in progress.

## Development

- Backend: Python 3.12 managed with `uv`
- Frontend: React 19, TypeScript, and Vite

### Prerequisites

- Python 3.12 and `uv`
- Node.js and npm
- Access to the corporate Microsoft package proxy configured in `backend/pyproject.toml`; Python artifacts resolve only through this proxy

### Local checks

Backend:

```text
cd backend
uv sync --locked
uv run ruff check .
uv run mypy app
uv run pytest -q
```

Frontend:

```text
cd frontend
npm ci
npm run lint
npm run typecheck
npm test -- --run
npm run build
```

### Run the local stack with Docker

The Compose stack runs the frontend (public NGINX), the FastAPI API, the queue worker, and Azurite
for Blob/Queue/Table storage. Content Understanding, embeddings, and GPT-5 have no local emulator, so
point `FOUNDRY_ENDPOINT` and `SEARCH_ENDPOINT` at real keyless Azure endpoints to exercise the full
pipeline; otherwise the shell, health checks, and storage run locally.

```text
cp .env.example .env   # optional: override endpoints; contains no secrets
docker compose build
docker compose up -d
# Frontend http://localhost:8080  |  API liveness http://localhost:8000/health/live
docker compose run --rm api cleanup   # run the retention sweep on demand
docker compose down
```

Do not upload confidential, regulated, or production information to the workshop application.
