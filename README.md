# Content Understanding Meets GitHub Copilot: Build and Ship an Intelligent Document RAG App

This repository contains a public workshop application for exploring document intelligence and retrieval-augmented generation. The application runtime model is GPT-5.

## Current status

The repository currently provides the initial React frontend shell and FastAPI liveness endpoint. Document upload, extraction, search, grounded chat, deployment, and infrastructure are planned but are not implemented yet.

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

Do not upload confidential, regulated, or production information to the workshop application.
