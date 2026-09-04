# Copilot instructions

Repository guidance for GitHub Copilot chat and automatic pull request code review.

## What this project is

A public "Document Intelligence Console": a React/Vite frontend on public Azure Container Apps
proxies to an internal FastAPI backend. Uploaded business documents are analyzed with Microsoft
Foundry Content Understanding, chunked, embedded with `text-embedding-3-large`, indexed in Azure
AI Search, and answered by `gpt-5` with grounded citations. Infrastructure is Bicep only; `azd`
provisions; delivery is GitHub Actions with OIDC.

## Review focus (flag these in PRs)

- **Session isolation.** Every read/write must be scoped by the hashed session key. Reject code
  that queries documents, chunks, or chat history across sessions.
- **Mandatory Search filters.** Retrieval must always filter by session and exclude
  tombstoned/deleting documents. A hybrid/vector query without the lifecycle filter is a bug.
- **SAS and secret leakage.** User-delegation SAS URLs, tokens, connection strings, and account
  keys must never appear in logs, error envelopes, API responses, or client code.
- **Keyless auth only.** Azure access is Microsoft Entra (`DefaultAzureCredential`/managed
  identity). Flag any API key, account key, or connection-string fallback.
- **Prompt injection.** Treat document text and user questions as untrusted; they must never be
  able to override system instructions or exfiltrate context.
- **Citation validation.** Chat answers must cite retrieved evidence; reject unvalidated or
  fabricated citation IDs and source locators.
- **Queue and lease idempotency.** Ingestion/cleanup handlers must be safe to retry (at-least-once
  delivery, ETag/lease fencing). Flag non-idempotent state transitions.
- **Bicep-only infrastructure.** Resources are created in Bicep. Flag scripts that create Azure
  resources imperatively (Azure CLI/PowerShell) instead of via Bicep.
- **Immutable revisions and rollback.** Deployments roll immutable image digests; flag mutable
  tags passed to Bicep or rollouts without a restore/rollback path.
- **GPT-5 model lock.** The runtime chat deployment is `gpt-5` and embeddings are 3,072-dim.
  Flag any other model or dimension.

## Conventions

- Backend: Python 3.12, `uv`, FastAPI, strict `mypy`, `ruff`; snake_case internally, camelCase at
  HTTP/queue boundaries; stable error envelope, never raw exception strings.
- Frontend: React 19 + TypeScript, Vitest, ESLint, Prettier.
- Do not review generated lockfiles (`uv.lock`, `package-lock.json`) or snapshots.
