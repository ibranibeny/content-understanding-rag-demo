# Content Understanding Meets GitHub Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, test, publish, and deploy a public Technical Console that turns mixed business documents into Content Understanding extractions and GPT-5 grounded answers.

**Architecture:** A React/Vite frontend on public Azure Container Apps proxies to an internal FastAPI API. Blob, Queue, and Table Storage provide direct uploads, durable work, state, leases, and lifecycle fencing; a queue-scaled backend worker calls Content Understanding in East US 2, chunks and embeds output, and pushes it to Azure AI Search in Southeast Asia. Bicep is the only IaC language, `azd` orchestrates provisioning, and GitHub Actions uses OIDC, CodeQL, Copilot review, immutable images, candidate revisions, smoke validation, and rollback.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, React Testing Library, Playwright, NGINX; Python 3.12, FastAPI, Pydantic, Azure SDKs, pytest, Ruff, mypy; Azure Container Apps, Storage, AI Search, Microsoft Foundry Content Understanding, `gpt-5`, `text-embedding-3-large`; Bicep + Azure Verified Modules; GitHub Actions, CodeQL, Copilot code review.

**Source of truth:** `docs/superpowers/specs/2026-09-03-content-understanding-rag-demo-design.md`

**Model constraint:** `gpt-5` is the application runtime model. Tasks 14–17 that author or review container, deployment, workflow, or Bicep code MUST be dispatched with **Claude Opus 4.8**. If it is unavailable, stop and report the blocker instead of silently substituting a model.

---

## File map

### Backend

- `backend/pyproject.toml` — Python dependencies, scripts, and lint/test configuration.
- `backend/app/main.py` — FastAPI application factory and middleware.
- `backend/app/core/config.py` — validated environment settings.
- `backend/app/core/errors.py` — stable error envelope and exception mapping.
- `backend/app/core/telemetry.py` — OpenTelemetry setup and redaction.
- `backend/app/domain/models.py` — sessions, documents, queue messages, chunks, citations, and states.
- `backend/app/domain/protocols.py` — narrow interfaces for repositories and Azure adapters.
- `backend/app/repositories/table_repository.py` — Table Storage session/document state with ETags.
- `backend/app/repositories/memory_repository.py` — deterministic test/local repository.
- `backend/app/services/outbox_service.py` — atomically persisted upload work and at-least-once queue dispatch.
- `backend/app/services/session_service.py` — anonymous cookie creation, hashing, expiry, and quota.
- `backend/app/services/file_validation.py` — extension, MIME, signature, and size checks.
- `backend/app/services/blob_service.py` — user-delegation SAS, blobs, and control-blob leases.
- `backend/app/services/upload_service.py` — upload initialization/completion and enqueueing.
- `backend/app/services/content_understanding.py` — token-authenticated analyzer calls and result deletion.
- `backend/app/services/chunking.py` — Markdown-aware, token-bounded chunks.
- `backend/app/services/embeddings.py` — `text-embedding-3-large` batches.
- `backend/app/services/search_service.py` — index bootstrap, idempotent writes, hybrid retrieval, and deletes.
- `backend/app/services/deletion_service.py` — tombstone and lease-fenced physical deletion; API requests leave durable deleting records for the cleanup job.
- `backend/app/services/rag_service.py` — retrieval, lifecycle filtering, GPT-5 streaming, and citation validation.
- `backend/app/worker.py` — ingestion and Content Understanding cleanup queue loops.
- `backend/app/cleanup.py` — expiry/deletion sweep command.
- `backend/app/api/*.py` — session, uploads, documents, chat, and health routes.
- `backend/tests/**` — unit, contract, concurrency, adapter, and integration tests.

### Frontend

- `frontend/src/app/App.tsx` — Technical Console composition.
- `frontend/src/api/client.ts` — typed JSON calls and cookie handling.
- `frontend/src/api/sse.ts` — strict SSE event parser and cancellation.
- `frontend/src/domain/types.ts` — shared client-side contracts.
- `frontend/src/features/documents/**` — upload, list, status, inspector, retry, and delete.
- `frontend/src/features/chat/**` — streamed chat, citations, diagnostics, and source previews.
- `frontend/src/components/**` — reusable status, metric, empty, error, and dialog components.
- `frontend/src/styles/**` — dark visual system, responsive layout, focus, and reduced motion.
- `frontend/tests/**` — unit/component tests.
- `frontend/e2e/**` — Playwright user journeys.

### Azure, deployment, and GitHub

- `infra/main.bicep` — resource-group-scope composition.
- `infra/main.bicepparam` — nonsecret defaults for Southeast Asia and East US 2.
- `infra/modules/*.bicep` — role IDs, identities, observability, storage/search, Foundry, and Container Apps composition.
- `azure.yaml` — `azd` environment and Bicep provider configuration.
- `scripts/bootstrap-data-plane.py` — analyzers, defaults, index, and keyless verification.
- `scripts/deploy.ps1` / `scripts/deploy.sh` — canonical ten-phase deployment flows.
- `scripts/deploy_revisions.py` — digest-preserving candidate labels, smoke gate, promotion, and rollback.
- `scripts/smoke_test.py` — full deployed upload-to-delete validation.
- `.github/workflows/ci.yml` — frontend/backend/IaC/container checks.
- `.github/workflows/codeql.yml` — Python and JavaScript/TypeScript CodeQL.
- `.github/workflows/deploy.yml` — OIDC, immutable images, Bicep provision, candidate deploy, and smoke test.
- `scripts/configure-github.ps1` — variables, environment, OIDC identity, and branch ruleset setup.

---

### Task 1: Bootstrap the monorepo and green health checks

**Execution research (2026-09-03):** The target is a clean linked worktree on
`feature/content-understanding-rag-demo`; only `.gitignore` and the committed design/plan exist.
The installed toolchain is Python/uv `0.11.7`, Node.js `24.16.0`, and npm `11.13.0`. The backend
scope is one FastAPI application factory plus one public liveness router; the frontend scope is one
React 19/Vite shell with a jsdom Vitest setup. Package registry checks confirmed current React
`19.2.8`, Vite `8.2.2`, Vitest `4.1.11`, and Playwright `1.62.1`; lockfiles will capture the complete
resolved dependency graphs. The task is atomic because its two health-shell tests jointly establish
the single monorepo bootstrap gate and neither introduces a reusable feature boundary. No
modernization scenario skill root or Breakdown Hints files were supplied for this task.

**Final quality remediation research (2026-09-03):** The dependency-policy helper in
`backend/tests/test_dependency_policy.py` currently approves a network URL solely by its parsed
hostname after treating both HTTP and HTTPS as network schemes. As a result, approved hosts pass
over insecure HTTP, and host-bearing unsupported schemes such as FTP also reach the host allowlist
and pass. The correction is confined to the policy helper and mutation coverage: every host-bearing
URL must use exactly HTTPS and an exact approved hostname, while existing hostless local file URLs
remain allowed and hosted file URLs remain rejected. This is one atomic validation-rule change;
there is no independent unit or decision point to split. No modernization scenario skill root or
Breakdown Hints files were supplied for this remediation.

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/uv.lock`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/api/health.py`
- Create: `backend/tests/test_health.py`
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/app/App.tsx`
- Create: `frontend/src/app/App.test.tsx`
- Create: `frontend/src/test/setup.ts`
- Create: `.editorconfig`
- Create: `README.md`

- [x] **Step 1: Scaffold dependency manifests**

Use Python 3.12 and `uv`. Add runtime dependencies for FastAPI, Uvicorn, Pydantic Settings, Azure Identity, Blob/Queue/Table Storage, Azure AI Search, Azure Monitor OpenTelemetry, HTTPX, OpenAI, `tiktoken`, and `python-multipart`. Add development dependencies for pytest, pytest-asyncio, pytest-cov, mypy, Ruff, and Azurite-compatible tests. Define these scripts in `backend/pyproject.toml`:

```toml
[project.scripts]
api = "app.main:run"
worker = "app.worker:run"
cleanup = "app.cleanup:run"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--strict-markers --strict-config"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
```

Use React + TypeScript + Vite and add Vitest, Testing Library, MSW, ESLint, Prettier, Playwright, and `axe-core`. Define `lint`, `typecheck`, `test`, `test:coverage`, `build`, and `e2e` scripts.

- [x] **Step 2: Write failing health and shell tests**

```python
# backend/tests/test_health.py
from fastapi.testclient import TestClient
from app.main import create_app


def test_liveness_is_public_and_stable() -> None:
    response = TestClient(create_app()).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

```tsx
// frontend/src/app/App.test.tsx
import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("renders the workshop identity and safety notice", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: /document intelligence console/i })).toBeVisible();
  expect(screen.getByText(/do not upload confidential information/i)).toBeVisible();
});
```

- [x] **Step 3: Run tests to verify failure**

Run: `cd backend && uv sync && uv run pytest tests/test_health.py -q`

Expected: FAIL because `app.main` does not exist.

Run: `cd frontend && npm install && npm test -- --run src/app/App.test.tsx`

Expected: FAIL because the app shell does not exist.

- [x] **Step 4: Implement the minimum app shells**

```python
# backend/app/main.py
from fastapi import FastAPI
from app.api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="Content Understanding RAG Demo", version="0.1.0")
    app.include_router(health_router)
    return app


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8000)
```

```python
# backend/app/api/health.py
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}
```

```tsx
// frontend/src/app/App.tsx
export function App() {
  return (
    <main>
      <h1>Document Intelligence Console</h1>
      <p role="note">Do not upload confidential information.</p>
    </main>
  );
}
```

- [x] **Step 5: Verify the bootstrap**

Run: `cd backend && uv run ruff check . && uv run mypy app && uv run pytest -q`

Expected: all checks pass.

Run: `cd frontend && npm run lint && npm run typecheck && npm test -- --run && npm run build`

Expected: all checks pass and `dist/` is generated.

- [x] **Step 6: Commit**

```bash
git add .editorconfig README.md backend frontend
git commit -m "chore: bootstrap document intelligence monorepo"
```

### Task 2: Define backend contracts, configuration, and stable errors

**Spec-compliance remediation research (2026-09-03):** The existing implementation defines
all nine required persistence/queue/evidence models and seven HTTP DTOs in
`backend/app/domain/models.py`, but its shared `ContractModel` only generates aliases; callers
must currently opt in with `by_alias=True`, so default boundary dumps violate the camel-case
contract. The model suite covers only a subset of those types and does not assert the complete
13-value `DocumentState` set, default `resumeStage`, JSON output, all UUID/session/time guards, or
every API DTO. The application factory currently accepts a mutable `ReadinessRegistry` and, when
none is supplied, registers only an always-true `configuration` probe in every mode. The required
factory boundary instead accepts a mapping of `ReadinessCheck` callables: local/test omission keeps
the configuration-only probe, production omission installs fail-closed probes named `blob`,
`queue`, `table`, `search`, and `foundry`, and production injection validates that exact name set
without invoking probes during import or construction. The readiness response and liveness route
contracts remain unchanged. This remediation is one atomic backend-contract gate: the model and
readiness changes share the same focused contract test/quality-check cycle and introduce no Azure
client implementation or internal strategy decision. No modernization scenario skill root or
Breakdown Hints files were supplied.

**Files:**
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/errors.py`
- Create: `backend/app/core/readiness.py`
- Create: `backend/app/domain/models.py`
- Create: `backend/app/domain/protocols.py`
- Create: `backend/tests/test_config.py`
- Create: `backend/tests/test_errors.py`
- Create: `backend/tests/test_models.py`
- Modify: `backend/app/main.py`

- [x] **Step 1: Write failing model/config tests**

```python
from datetime import UTC, datetime, timedelta
import pytest
from pydantic import ValidationError
from app.core.config import Settings
from app.domain.models import DocumentState, IngestionMessage


def test_runtime_model_is_fixed_to_gpt_5() -> None:
    settings = Settings.model_validate({"foundry_endpoint": "https://demo.services.ai.azure.com"})
    assert settings.chat_deployment == "gpt-5"
    assert settings.embedding_dimensions == 3072


def test_queue_message_rejects_unknown_versions() -> None:
    with pytest.raises(ValidationError):
        IngestionMessage.model_validate({
            "version": 2,
            "sessionKey": "a" * 64,
            "documentId": "9f4b8484-9f6b-44f2-b4d4-e5e7687c80df",
            "blobName": "uploads/a/file.pdf",
            "correlationId": "868fba2c-1695-42d4-af7f-79069e434b34",
            "enqueuedAt": datetime.now(UTC),
        })


def test_state_machine_includes_remote_cleanup() -> None:
    assert DocumentState.RESULT_CLEANUP_PENDING.value == "result_cleanup_pending"


def test_chunking_resume_stage_is_versioned() -> None:
    message = IngestionMessage.model_validate({
        "version": 1,
        "sessionKey": "a" * 64,
        "documentId": "9f4b8484-9f6b-44f2-b4d4-e5e7687c80df",
        "blobName": "uploads/a/file.pdf",
        "correlationId": "868fba2c-1695-42d4-af7f-79069e434b34",
        "enqueuedAt": datetime.now(UTC),
        "resumeStage": "chunking",
    })
    assert message.resume_stage == "chunking"
```

- [x] **Step 2: Run and verify failure**

Run: `cd backend && uv run pytest tests/test_config.py tests/test_models.py -q`

Expected: FAIL because the modules are missing.

- [x] **Step 3: Implement strict settings and domain models**

Define `Settings` with aliases for all endpoints, account names, queue/table/container names, cookie settings, maximums, release SHA, and model deployments. Reject any chat deployment other than `gpt-5` and any embedding dimension other than 3,072.

```python
class DocumentState(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    RESULT_CLEANUP_PENDING = "result_cleanup_pending"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"
```

Define immutable Pydantic models for `SessionRecord`, `DocumentRecord`, `IngestionMessage`, `ContentResultCleanupMessage`, `OutboxRecord`, `DocumentChunk`, `RetrievedEvidence`, `Citation`, and API request/response DTOs. `IngestionMessage.resume_stage` is `Literal["analyzing", "chunking"]` with default `"analyzing"`; cleanup success emits `"chunking"`. Use camel-case aliases at the HTTP and queue boundaries and snake case internally.

- [x] **Step 4: Define dependency protocols**

Create focused async protocols rather than passing Azure SDK clients into route handlers:

```python
class DocumentRepository(Protocol):
    async def get(self, session_key: str, document_id: UUID) -> VersionedDocument | None: ...
    async def create(self, document: DocumentRecord) -> VersionedDocument: ...
    async def replace(self, document: DocumentRecord, etag: str) -> VersionedDocument: ...
    async def list_for_session(self, session_key: str) -> list[VersionedDocument]: ...


class WorkQueue(Protocol):
    async def enqueue_ingestion(self, message: IngestionMessage) -> None: ...
    async def enqueue_result_cleanup(self, message: ContentResultCleanupMessage) -> None: ...
```

Add repository operations `commit_queued_with_outbox(document, document_etag, outbox)` using one same-partition transaction, `list_pending_outbox(limit)`, and `mark_outbox_sent(id, etag)`. Also define protocols for `BlobStore`, `ContentUnderstandingClient`, `EmbeddingClient`, `ChunkSearch`, `ChatModel`, `ReadinessCheck`, and `Clock`.

- [x] **Step 5: Implement stable error mapping**

Create `AppError(code, status_code, message, retryable)` and one FastAPI handler returning:

```json
{"error":{"code":"file_too_large","message":"Files must be 100 MB or smaller.","retryable":false,"correlationId":"..."}}
```

Never expose exception strings. Add correlation-ID middleware that accepts a valid incoming UUID or generates one and echoes `X-Correlation-ID`.

- [x] **Step 6: Add dependency-aware readiness**

Implement `ReadinessRegistry` with named async checks and a two-second total timeout. `GET /health/ready` returns `200 {"status":"ready"}` only when configuration, Blob, Queue, Table, Search, and Foundry token probes succeed; otherwise it returns `503 {"status":"not_ready","failed":["search"]}` without credentials or exception text. Tests inject passing, failing, and timed-out checks.

- [x] **Step 7: Run all backend checks and commit**

Run: `cd backend && uv run ruff check . && uv run mypy app && uv run pytest -q`

Expected: PASS.

```bash
git add backend/app backend/tests
git commit -m "feat: define backend domain contracts"
```

### Task 3: Implement anonymous sessions and quotas

**Execution research (2026-09-03):** Task 2 already supplies frozen `SessionRecord` and
`SessionResponse` models, a narrow async `SessionRepository` protocol, strict UTC validation,
bounded quota/lifetime settings, stable `AppError` handling, and an application factory with
typed state for settings/readiness. `SessionRecord.question_timestamps` is already an immutable
UTC tuple. Task 3 therefore adds one in-memory protocol adapter, one service, and one route, while
extending only the shared error module with a repository-level concurrency signal. The service
will derive repository keys exclusively from decoded 32-byte cookie tokens, retain timestamps
strictly newer than `now - 1 hour` (an event exactly on the boundary is expired), reject document
over-release predictably, retry ETag conflicts at most five times, and expose quota state through
the existing camel-case DTO. `create_app` will accept an injected service or construct an isolated
in-memory service and store it on `app.state`; no global repository is introduced. The task is one
coherent security boundary whose repository, service, and endpoint are validated together, so it
is atomic. No modernization scenario skill root, Execution stage, or Breakdown Hints files were
supplied.

**Files:**
- Create: `backend/app/repositories/memory_repository.py`
- Create: `backend/app/services/session_service.py`
- Create: `backend/app/api/session.py`
- Create: `backend/tests/services/test_session_service.py`
- Create: `backend/tests/api/test_session_api.py`
- Modify: `backend/app/main.py`

- [x] **Step 1: Write failing service tests**

```python
async def test_new_session_hashes_token_and_expires_in_24_hours() -> None:
    clock = FrozenClock("2026-09-03T10:00:00Z")
    service = SessionService(MemorySessionRepository(), clock, token_factory=lambda: b"x" * 32)
    issued = await service.issue()
    assert issued.raw_token != issued.record.session_key
    assert issued.record.session_key == sha256(b"x" * 32).hexdigest()
    assert issued.record.expires_at == clock.now() + timedelta(hours=24)


async def test_question_quota_rejects_31st_question() -> None:
    service, record = session_with_questions(30)
    with pytest.raises(AppError, match="question_quota_exceeded"):
        await service.reserve_question(record.session_key)
```

- [x] **Step 2: Verify tests fail**

Run: `cd backend && uv run pytest tests/services/test_session_service.py -q`

Expected: FAIL because session service/repository do not exist.

- [x] **Step 3: Implement session service**

Generate tokens with `secrets.token_bytes(32)`, hash with SHA-256, use UTC timestamps, maintain a rolling one-hour list/count for questions, and enforce five documents, 500 MB, and 30 questions/hour with ETag retries. Store only the hash.

- [x] **Step 4: Add the session endpoint and cookie policy**

`GET /api/session` creates or reads `cu_session`. In deployed mode set `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, and `Max-Age=86400`. Return only expiry and quota usage; never return token/hash.

- [x] **Step 5: Verify API behavior**

Run: `cd backend && uv run pytest tests/services/test_session_service.py tests/api/test_session_api.py -q`

Expected: cookie has all required flags; repeat request reuses the session; invalid/expired cookie rotates to a new session.

- [x] **Step 6: Commit**

```bash
git add backend/app backend/tests
git commit -m "feat: add anonymous session isolation"
```

### Task 4: Implement file validation and direct upload

**Execution research (2026-09-03):** Tasks 1-3 provide immutable camel-case boundary models,
stable `AppError` handling, an injected `SessionService`, and only a session in-memory repository;
the document/outbox repository, queue, and blob implementations are not present. The locked offline
environment already contains Azure Identity and Azure Blob Storage. Its async SDK exposes
`BlobServiceClient.get_user_delegation_key`, blob property/range download APIs, and synchronous
`generate_blob_sas` with `BlobSasPermissions(create=True, write=True)`, so no dependency or network
change is required. Task 4 will extend the existing protocols with an upload-specific blob adapter
result and bounded package read, add one process-local document/outbox repository that simulates the
required same-partition atomic commit, and keep Azure Table persistence and worker processing out of
scope. ZIP validation will inspect the complete central directory from bounded blob bytes, reject
encrypted/traversing packages, cap entry count and aggregate uncompressed size, and require the exact
Office package markers. The API will resolve/rotate the existing anonymous cookie on both mutations,
store dependencies only on `app.state`, and make lifespan dispatch optional/injectable for deterministic
tests. The work is one coherent upload security boundary with a single state transition and outbox
transaction; its adapters are narrow seams rather than independently deployable layers, so the task is
atomic. No modernization scenario skill root, Execution stage, or Breakdown Hints files were supplied.

**Spec-gap research (2026-09-03):** Review of the committed Task 4 implementation confirmed that
`sanitize_file_name` replaces backslashes and then applies `PurePosixPath(...).name`, intentionally
discarding client-supplied path components. The service and API regression tests currently encode that
unsafe behavior by expecting `../../safe name.pdf` and `../../a.pdf` to succeed. The upload service does
call `validate_declared_upload` before `SessionService.reserve_document`, document creation, and blob SAS
creation, so the narrow fix is to reject either `/` or `\\` in the raw name at the beginning of
`sanitize_file_name`, before control stripping, NFC normalization, or basename handling. Regression
coverage will include relative traversal, ordinary directories, absolute Unix paths, Windows drive and
UNC paths, and mixed separators; service tests will verify no quota, document, or blob side effects, and
API tests will verify the stable nonretryable `invalid_file_name` 400 envelope. Existing NFC normalization
for a legitimate Unicode basename remains required. This correction is atomic: it changes one validation
invariant at the existing pre-side-effect boundary and its service/API contracts. No modernization
scenario skill root, Execution stage, or Breakdown Hints files were supplied for this review fix.

**Code-quality remediation research (2026-09-03):** The current initialization sequence mutates the
session repository, creates a document in a separately locked repository, and only then requests a SAS;
its compensation path releases quota only after a successful document delete, so a failed delete leaves
both records persisted. The in-memory repositories have independent backing dictionaries and cannot
provide the same-partition atomic boundary required by the future Table adapter. Initialization will
instead create its one-blob SAS before persistence and call a focused `reserve_and_create` operation
through a shared application repository; `SessionService` remains the owner of quota validation and its
five-attempt optimistic-concurrency retry. The Azure blob adapter's Office path currently calls
`readall()` for the full declared size and returns those bytes, amplifying up to 100 MiB in memory. It
will use conditional async chunks under a default two-call semaphore, write to a bounded
`SpooledTemporaryFile`, validate the ZIP while the spool is open, and return only immutable entry
metadata. The adapter also has no close boundary despite lazily owning Azure clients and credentials,
while the FastAPI lifespan only cancels the dispatcher. Ownership-aware idempotent async close will be
added and lifespan shutdown will order dispatcher cancellation before upload resource close. Outbox
exceptions are swallowed without observability, and `UploadCompleteRequest.etag` accepts arbitrary text;
safe structured logging and a strict 256-character quoted/weak-quoted ETag type close those gaps. Existing
focused tests (146) pass before remediation. This is one coherent Task 4 hardening change: all findings
concern the direct-upload transaction/resource boundary. No modernization scenario skill root, Execution
stage, or Breakdown Hints files were supplied, so no scenario-specific decomposition rule can be evaluated
and the requested Task 4 remediation is treated as atomic.

**Files:**
- Create: `backend/app/services/file_validation.py`
- Create: `backend/app/services/blob_service.py`
- Create: `backend/app/services/upload_service.py`
- Create: `backend/app/services/outbox_service.py`
- Create: `backend/app/api/uploads.py`
- Create: `backend/tests/services/test_file_validation.py`
- Create: `backend/tests/services/test_upload_service.py`
- Create: `backend/tests/api/test_upload_api.py`
- Modify: `backend/app/main.py`

- [x] **Step 1: Write failing validation tests**

Use exact signatures: PDF `%PDF-`, DOCX/PPTX ZIP `PK\x03\x04` plus package-entry inspection, PNG `\x89PNG\r\n\x1a\n`, JPEG `\xff\xd8\xff`.

```python
@pytest.mark.parametrize(("name", "mime", "head"), [
    ("a.pdf", "application/pdf", b"%PDF-1.7"),
    ("a.png", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("a.jpg", "image/jpeg", b"\xff\xd8\xff\xe0"),
])
def test_supported_signatures_pass(name: str, mime: str, head: bytes) -> None:
    validate_uploaded_file(name, mime, len(head), head)


def test_100_mb_plus_one_byte_is_rejected() -> None:
    with pytest.raises(AppError, match="file_too_large"):
        validate_declared_upload("a.pdf", "application/pdf", 100 * 1024 * 1024 + 1)
```

- [x] **Step 2: Verify failure, then implement declared and post-upload validation**

Run: `cd backend && uv run pytest tests/services/test_file_validation.py -q`

Expected before implementation: FAIL. After implementation: PASS.

Normalize names with `PurePath(name).name`, Unicode NFC, an allowlist, and a 120-character limit. Generate blob paths from server UUIDs, never from user path segments.

- [x] **Step 3: Implement user-delegation SAS generation**

Use `DefaultAzureCredential` and `BlobServiceClient.get_user_delegation_key`. Generate one-blob HTTPS-only SAS with `create=True`, `write=True`, no list/read/delete, start time five minutes in the past, and expiry 15 minutes ahead. The resulting response contains `uploadUrl`, `documentId`, `expiresAt`, and required `x-ms-blob-type: BlockBlob` header.

- [x] **Step 4: Implement completion verification and an atomic outbox**

`POST /api/uploads/{id}/complete` loads properties, compares ETag/length/content type, reads signature bytes, and validates Office ZIP entries. In one Table transaction on the session partition, change `awaiting_upload` to `queued` and create a deterministic outbox row whose ID is `ingest:{documentId}:1`. Only after that commit, opportunistically dispatch to Storage Queue and mark the outbox sent. A repeated completion returns the existing state and can redispatch the same deterministic outbox item safely.

Run an `OutboxDispatcher` in the API lifespan every five seconds. It reads pending rows, enqueues the versioned message, and ETag-marks them sent. A crash before queue send leaves a pending row; a crash after queue send but before mark can duplicate the message, which the leased/idempotent worker accepts safely.

- [x] **Step 5: Run focused and full tests**

Run: `cd backend && uv run pytest tests/services/test_file_validation.py tests/services/test_upload_service.py tests/api/test_upload_api.py -q`

Expected: all upload boundaries, quotas, path sanitization, SAS permissions, ETag mismatch, crash-before-send, crash-after-send, and duplicate completion cases pass.

- [x] **Step 6: Commit**

```bash
git add backend/app backend/tests
git commit -m "feat: add secure direct document uploads"
```

### Task 5: Add Azure Table repositories and lease-fenced deletion

**Azurite read-SAS blocker research (2026-09-03):** The remaining defect is isolated to
`AzureBlobStore.create_read_url`: unlike `create_upload`, it bypasses the injected
`BlobSasSigner` and directly requests a user-delegation key, which Azurite does not implement.
The signer protocol already receives explicit permissions, exact blob identity, start, and expiry,
so the narrow fix is to route reads through that existing protocol. Read grants must target the
requested blob in the configured uploads container, grant only `read`, cap a later caller-requested
expiry at the fixed 15-minute SAS lifetime, use `https,http` only with the injected local signer,
and preserve HTTPS-only user delegation by default. Only `create_local_dependencies` parses an
Azurite account key and injects `LocalBlobSasSigner`; the production dependency factory exposes no
account-key or signer parameter. No SAS or key may enter logs or retained public state. This is one
atomic Blob-adapter correction and focused regression suite; no modernization scenario skill root,
Execution-stage file, or Breakdown Hints files were forwarded.

**Execution research (2026-09-03):** The requested linked worktree is clean on
`feature/content-understanding-rag-demo`, `uv sync --locked --offline` succeeds from the existing
lockfile, and the complete backend baseline is 475 passing tests. Tasks 1-4 already provide frozen
session/document/outbox models, strict Azure ETag validation, a shared in-memory application
repository with atomic `reserve_and_create`, same-partition-like queued/outbox commits, injected
FastAPI services, an ownership-aware Blob adapter, exact-origin mutation guards, and stable error
envelopes. The installed Azure SDK exposes async `TableClient.submit_transaction` and paged
`query_entities`, plus `BlobLeaseClient.acquire(lease_duration=60)`, `renew`, and `release`; the lease
client itself has no async `close`, so closure belongs to owned blob/service clients. Task 5 must add
primitive/versioned entity codecs for all model fields, one injected async Table client adapter,
deterministic retry outbox IDs, a durable deleting-state sweep with 48-hour tombstone retention, and
a renewable control-blob lease abstraction shared by future workers without implementing worker
processing. Local/test may use Azurite's documented development connection string, while production
must use `DefaultAzureCredential` and reject connection-string/shared-key configuration. The requested
scope spans Table persistence, lease infrastructure, lifecycle orchestration, and HTTP contracts, but
they form one externally visible document-lifecycle feature with explicit seams and a single required
verification/commit gate. No modernization scenario skill root, Execution stage, Breakdown Hints,
workflow folder, standalone `task.md`, or `progress-details.md` was forwarded; this plan section is the
execution reference and will not have its checkboxes changed.

**Task 5A controller-decomposition research (2026-09-03):** This bounded unit is only the Azure
Table/Azurite persistence foundation; leases, deletion orchestration, and document routes remain for
later controller units. The existing `SessionRepository`, `DocumentRepository`, and
`SessionDocumentRepository` contracts require session CRUD, document CRUD/listing, atomic quota
reservation plus document creation, and atomic queued-document plus outbox creation. The installed
async Azure Tables SDK supports conditional `update`/`delete` transaction tuples and continuation
token paging. Entities will share `PartitionKey=session:{sessionKey}` with `session`,
`document:{uuid}`, and `outbox:{outboxId}` row keys; a versioned JSON codec keeps Pydantic's existing
camel-case boundary shape while all Table properties remain strict primitives. Azure service errors
for stale, duplicate, and missing mutations map to the repository's stable `ConcurrencyConflict`.
Construction will accept an async TableClient-shaped protocol for deterministic tests, close only
owned dependencies, use `DefaultAzureCredential` with the account Table endpoint in production, and
permit the public Azurite development connection string only in local/test configuration. The unit is
explicitly pre-decomposed and atomic per the controller instruction; no modernization scenario root,
Execution stage, or Breakdown Hints artifacts were supplied or requested.

**Task 5A execution results (2026-09-03):** Strict TDD was observed: the focused repository/config
suite first failed during collection because the Table repository and stable codec error did not
exist, then passed with 135 tests after implementation. Final verification completed locked offline
sync, Ruff, strict mypy over all application modules, and the full backend suite with 489 passing
tests. Docker is not installed or available on `PATH` in this environment, so `docker compose config`
and live Azurite transaction integration could not run; the mandatory transactional fake covers
operation tuple shapes, ETags, duplicate/stale translation, pagination, and rollback/no-partial-commit
behavior. The Compose file remains available for validation in a Docker-enabled environment.

**Task 5B controller-decomposition research (2026-09-03):** This bounded unit is the control-blob
lease abstraction and lease-fenced deletion service only; HTTP document routes, ingestion workers,
and the Azure Search adapter remain outside scope. Task 5A provides optimistic document CRUD in both
memory and Table repositories. The current document model lacks deletion linearization timestamps,
the repository contract lacks a bounded durable lifecycle scan, and the Blob adapter exposes only
single-name deletion. The installed async Blob SDK supports zero-byte `upload_blob(overwrite=False)`,
`BlobLeaseClient.acquire(lease_duration=60)`, renewable leases, release, prefix listing, and conditional
deletes. The implementation will derive control names exclusively as
`control/{sessionKey}/{documentId}.lock`, expose a typed renewable async lease handle reusable by the
future worker, distinguish busy/lost leases without leaking Azure exception text, and preserve injected
client ownership. The deletion linearization point is an ETag-protected transition to `deleting` with
`tombstonedAt` and `deletionRequestedAt` before any Blob/Search side effect. Sweeps select deleting or
expired rows durably, tombstone expired live rows first, acquire the shared lease, re-read state/ETag,
delete all server-derived original/derived blobs and Search chunks idempotently, then clear extraction,
remote-result/source metrics and mark `deleted`. Busy leases and transient adapter failures retain the
tombstone. Purge uses an exact 48-hour inclusive boundary and deletes only after both Blob and Search
confirm absence. This is explicitly controller-pre-decomposed and atomic per the user instruction; no
modernization scenario root, Execution stage, Breakdown Hints, standalone `task.md`, or
`progress-details.md` was supplied, so this plan section is the required execution/progress artifact.

**Task 5B execution progress (2026-09-03):** Tests were authored first and observed failing during
collection for the missing deletion module and lease types, then again for the missing reusable worker
write-lease boundary. The implementation adds deletion timestamps, bounded lifecycle/purge repository
scans, queryable Table projection fields, renewable 60-second leases over zero-byte server-derived
control blobs, cancellation-safe release, server-prefix artifact deletion, typed transient outcomes,
ETag tombstoning, fenced idempotent sweeps, exact 48-hour purge, and the pre/post-acquisition worker
guard. Focused deletion/Blob/Table tests pass (46 tests after the final worker-guard case); the prior
full verification before that final case completed locked offline sync, Ruff, strict mypy, and 506
passing tests. Final full verification and commit evidence follow in the task report.

**Task 5B final verification (2026-09-03):** `uv sync --locked --offline` resolved entirely from
the lock/cache; 46 focused deletion, Blob lease, and Table repository tests passed; Ruff reported no
findings; strict mypy reported no issues across 24 application modules; and the full backend suite
passed with 507 tests. `git diff --check` passed, and the reviewed diff remains confined to the Task
5B service, domain/protocol, repository, test, safe error type, and execution-record scope. No HTTP
document route, queue/worker, Search Azure adapter, ephemeral task, or dependency/feed change was added.

**Task 5C controller-decomposition research (2026-09-03):** This explicitly bounded unit is only
the document lifecycle HTTP API and cohesive application wiring; scheduled cleanup, worker processing,
and RAG remain outside scope. Task 5A/B already provide shared memory/Table document repositories,
same-partition ETag/outbox transactions, targeted outbox dispatch, and logical deletion through
`DeletionService.request_delete`. The document model needs a persisted retry counter so initial upload
attempt 1 and later deterministic retry outbox IDs cannot collide. A focused `DocumentService` will own
list/get/retry/delete policy over one injected repository, deletion service, dispatcher, and clock;
routes will reuse the existing cookie resolver and exact-Origin dependency. Lists and reads hide both
`deleting` and `deleted` immediately, sort newest first with UUID tie-breaking, expose extraction only
through the owner-scoped detail DTO, and never serialize session keys, blob names, SAS values, or remote
operation URLs. Retry will permit only unexpired retryable failures, atomically clear failure fields,
increment the counter, and create `ingest:{documentId}:{nextAttempt}` with conflict convergence and a
targeted dispatch. The app factory will construct all local services from one shared memory repository,
accept cohesive document/deletion injection for Table-backed production composition, and close only
dependencies it constructs. This unit is atomic by explicit controller decomposition and because all
changes implement one route/service boundary with one focused verification gate. No modernization
scenario skill root, Execution stage, Breakdown Hints, standalone `task.md`, or `progress-details.md`
was forwarded; this plan section is the required execution/progress artifact.

**Task 5C execution and verification (2026-09-03):** Strict TDD began with the focused document API
suite failing during collection because `DocumentService` did not exist. The implementation adds
owner-scoped summary/detail DTOs, immediate deleting/deleted visibility fencing, stable newest-first
ordering, persisted retry attempts, atomic deterministic retry outboxes with conflict convergence and
targeted best-effort dispatch, exact-Origin guarded mutations, typed `202` deletion, shared cookie
resolution including rotation on handled errors, and cohesive local/injected dependency graphs. Injected
upload/blob resources are not closed by the factory; only factory-owned Blob resources are closed after
dispatcher cancellation. Locked offline sync succeeded, Ruff reported no findings, strict mypy reported
no issues across 26 application modules, 76 focused session/upload/document API tests passed, and the
full backend suite passed with 528 tests. Docker is unavailable on `PATH`, so `docker compose config`
could not run; no Compose file was changed. `git diff --check` passed before this execution record was
appended, and the reviewed diff is confined to Task 5C API/service/DTO/wiring/tests and this plan.

**Files:**
- Create: `backend/app/repositories/table_repository.py`
- Create: `backend/app/services/deletion_service.py`
- Create: `backend/app/api/documents.py`
- Create: `backend/tests/repositories/test_table_repository.py`
- Create: `backend/tests/services/test_deletion_service.py`
- Create: `backend/tests/api/test_documents_api.py`
- Create: `compose.yml`
- Modify: `backend/app/services/blob_service.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing ETag and race tests**

```python
async def test_stale_etag_cannot_overwrite_document() -> None:
    repo = MemoryDocumentRepository()
    created = await repo.create(document())
    await repo.replace(created.value.model_copy(update={"state": DocumentState.QUEUED}), created.etag)
    with pytest.raises(ConcurrencyConflict):
        await repo.replace(created.value, created.etag)


async def test_delete_waits_for_writer_lease_then_removes_all_artifacts() -> None:
    lease = ControllableLease(held=True)
    service = deletion_service(lease=lease)
    task = asyncio.create_task(service.delete(session_key, document_id))
    await asyncio.sleep(0)
    assert not task.done()
    lease.release_writer()
    await task
    assert service.search.deleted_document_ids == [document_id]
    assert service.blobs.derived_deleted
```

- [ ] **Step 2: Implement Table Storage mappings**

Use partition keys `session:{sessionKey}` and row keys `session` / `document:{documentId}`. Convert Pydantic models to primitive entities, store ISO UTC values, preserve Azure ETags, and translate `ResourceModifiedError` to `ConcurrencyConflict`.

- [ ] **Step 3: Implement control-blob lease fencing**

Create `control/{sessionKey}/{documentId}.lock`. Worker attempts use a renewable 60-second lease for the entire write pipeline. Deletion writes a Table tombstone first, then acquires the same lease before deleting. Lease acquisition retries with bounded jitter; API returns `202` immediately and cleanup finishes asynchronously.

- [ ] **Step 4: Implement lifecycle-safe document routes**

List/get/retry/delete must verify the cookie-derived `sessionKey`. Retry only `failed` states and keeps deterministic IDs. Delete closes logical visibility at the tombstone write and returns `202`; the durable Table record remains in `deleting` state. The hourly cleanup job scans both expired and deleting records, acquires the control lease, and finishes physical removal. No ephemeral background task or unimplemented deletion queue is used. Return extraction only for that session.

- [ ] **Step 5: Run Azurite and concurrency tests**

Create `compose.yml` initially with a pinned Azurite service exposing ports 10000–10002 and a named volume. Task 14 extends this same file with application services.

Run: `docker compose up -d azurite`

Run: `cd backend && uv run pytest tests/repositories/test_table_repository.py tests/services/test_deletion_service.py tests/api/test_documents_api.py -q`

Expected: ETag conflicts, lease renewal, active-writer deletion, redelivery-after-delete, and cross-session access tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app backend/tests compose.yml
git commit -m "feat: fence document lifecycle operations"
```

### Task 6: Define analyzers and Content Understanding adapter

**Files:**
- Create: `analyzers/general-business.json`
- Create: `analyzers/invoice.json`
- Create: `analyzers/receipt.json`
- Create: `analyzers/contract.json`
- Create: `analyzers/router.json`
- Create: `backend/app/services/content_understanding.py`
- Create: `backend/tests/services/test_content_understanding.py`
- Create: `backend/tests/test_analyzer_definitions.py`

- [ ] **Step 1: Write analyzer schema tests**

```python
EXPECTED = {
    "general-business": {"title", "summary", "documentDate", "organizations", "people", "keyTopics", "actionItems", "importantFacts"},
    "invoice": {"vendorName", "customerName", "invoiceNumber", "invoiceDate", "dueDate", "currency", "subtotal", "tax", "total", "lineItems"},
    "receipt": {"merchantName", "transactionDate", "currency", "subtotal", "tax", "total", "paymentMethod", "items"},
    "contract": {"title", "parties", "effectiveDate", "expirationDate", "renewalTerms", "governingLaw", "obligations", "terminationClauses", "riskFlags"},
}


def test_router_has_four_explicit_category_routes() -> None:
    router = load_json("analyzers/router.json")
    assert set(router["config"]["contentCategories"]) == set(EXPECTED)
    for category in EXPECTED:
        assert router["config"]["contentCategories"][category]["analyzerId"] == f"workshop-{category}"
```

Also validate that each analyzer extends `prebuilt-document`, enables Markdown content, defines every approved field exactly once, and uses no undeclared category.

- [ ] **Step 2: Create the four exact schemas and router**

Use GA API `2025-11-01`. Set `baseAnalyzerId` to `prebuilt-document`; set `returnDetails` and source/confidence only where the field is shown as evidence. Router categories use concise descriptions and explicit `analyzerId` targets. Set segmentation false because each upload is one logical document.

- [ ] **Step 3: Write failing token-auth adapter tests**

Test `POST /contentunderstanding/analyzers/{router}:analyze`, polling the exact `Operation-Location`, and `DELETE /contentunderstanding/analyzerResults/{id}`. Assert `Authorization: Bearer` is present and `Ocp-Apim-Subscription-Key` is absent.

- [ ] **Step 4: Implement the adapter**

Use `azure.identity.aio.DefaultAzureCredential` to obtain the `https://cognitiveservices.azure.com/.default` token and `httpx.AsyncClient`. Persist `resultId` immediately from `Operation-Location`. Treat `408`, `409` while busy, `429`, and `5xx` as transient; treat malformed results and `4xx` authorization/configuration errors as terminal. Return normalized Markdown, structured fields, category, source locators, token counts, and page count.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && uv run pytest tests/test_analyzer_definitions.py tests/services/test_content_understanding.py -q`

Expected: all definitions and token-only HTTP interactions pass.

```bash
git add analyzers backend/app/services/content_understanding.py backend/tests
git commit -m "feat: add mixed document analyzers"
```

### Task 7: Implement chunking, embeddings, and Azure AI Search

**Files:**
- Create: `backend/app/services/chunking.py`
- Create: `backend/app/services/embeddings.py`
- Create: `backend/app/services/search_service.py`
- Create: `backend/tests/services/test_chunking.py`
- Create: `backend/tests/services/test_embeddings.py`
- Create: `backend/tests/services/test_search_service.py`
- Create: `scripts/search-index.json`

- [ ] **Step 1: Write failing chunk boundary tests**

```python
def test_chunks_preserve_heading_and_page_locator() -> None:
    markdown = "# Agreement\n<!-- PageNumber=1 -->\n" + ("alpha " * 900) + "\n## Renewal\n<!-- PageNumber=2 -->\n" + ("beta " * 300)
    chunks = chunk_markdown(markdown, document_id=DOC_ID, max_tokens=800, overlap_tokens=120)
    assert all(count_tokens(chunk.content) <= 800 for chunk in chunks)
    assert chunks[-1].section_path == "Agreement > Renewal"
    assert chunks[-1].source_locator == "page 2"
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
```

- [ ] **Step 2: Implement deterministic Markdown chunking**

Split by heading/page markers, then sentence/paragraph boundaries, then token windows only as a fallback. Derive `chunkId` as URL-safe Base64 SHA-256 of `documentId:ordinal:contentHash`. Include a 120-token overlap without crossing unrelated sections.

- [ ] **Step 3: Implement embeddings with strict dimensions**

Batch up to 64 chunks and stay under model input limits. Use Microsoft Entra token auth. Reject responses whose vector length is not 3,072. Add retry-after-aware retry behavior and release-SHA telemetry.

- [ ] **Step 4: Define and test the exact Search index**

Create `document-chunks` with all fields in the design, HNSW cosine profile, `contentVector` dimension 3,072, semantic configuration prioritizing title, section path, and content, and disabled local authentication on the service. Tests compare the generated schema to `scripts/search-index.json`.

- [ ] **Step 5: Implement indexing and retrieval**

Use `merge_or_upload_documents` in batches with per-key failure checks. Build filters only from validated server values:

```python
def build_scope_filter(session_key: str, document_ids: tuple[UUID, ...]) -> str:
    clauses = [f"sessionKey eq '{escape_odata(session_key)}'"]
    if document_ids:
        ids = " or ".join(f"documentId eq '{escape_odata(str(value))}'" for value in document_ids)
        clauses.append(f"({ids})")
    return " and ".join(clauses)
```

Hybrid retrieval uses keyword text, `VectorizedQuery(k_nearest_neighbors=50)`, semantic ranker, and returns top eight.

- [ ] **Step 6: Verify and commit**

Run: `cd backend && uv run pytest tests/services/test_chunking.py tests/services/test_embeddings.py tests/services/test_search_service.py -q`

Expected: chunk limits, deterministic IDs, vector dimensions, mandatory session filter, partial batch failures, and delete-by-document tests pass.

```bash
git add backend/app/services backend/tests scripts/search-index.json
git commit -m "feat: add vector indexing and hybrid retrieval"
```

### Task 8: Build the durable ingestion and result-cleanup worker

**Files:**
- Create: `backend/app/services/ingestion_service.py`
- Create: `backend/app/services/retry.py`
- Create: `backend/app/worker.py`
- Create: `backend/tests/services/test_ingestion_service.py`
- Create: `backend/tests/test_worker.py`

- [ ] **Step 1: Write failing happy-path and resumption tests**

```python
async def test_ingestion_reaches_ready_only_after_remote_result_delete() -> None:
    h = Harness()
    await h.service.process(h.message)
    assert h.states == [
        "analyzing", "classified", "extracted", "chunking", "embedding", "indexing", "ready"
    ]
    assert h.content_understanding.deleted_result_ids == ["result-1"]
    assert h.search.upserted_count > 0


async def test_redelivery_resumes_stored_result_instead_of_reanalyzing() -> None:
    h = Harness(document_overrides={"content_result_id": "result-1", "state": "analyzing"})
    await h.service.process(h.message)
    assert h.content_understanding.begin_calls == 0
    assert h.content_understanding.get_calls >= 1
```

- [ ] **Step 2: Write cleanup-queue and tombstone tests**

Test that a failed result delete stores `result_cleanup_pending`, enqueues exactly one cleanup message, never analyzes again, retries indefinitely with increasing visibility delay, and resumes at `chunking` only after a `204`. Test tombstones before and immediately after lease acquisition and during renewal.

- [ ] **Step 3: Implement the ingestion state machine**

Acquire the control lease, use ETag transitions, save remote result ID before polling, persist normalized output, delete remote result, chunk/embed/upsert, and mark ready. Every retry is idempotent. Include `release_sha` in processing metadata.

- [ ] **Step 4: Implement two queue pumps**

One process polls `ingestion` and `cu-result-cleanup` concurrently with bounded concurrency. Renew queue visibility and blob lease in background tasks. Delete queue messages only after durable state transition. Normal failures have five attempts then poison; remote-result deletion remains on its dedicated durable queue and alerts after five attempts without stopping retries.

- [ ] **Step 5: Verify worker behavior**

Run: `cd backend && uv run pytest tests/services/test_ingestion_service.py tests/test_worker.py -q`

Expected: happy path, all resume points, `429` Retry-After, poison behavior, cleanup-only scale scenario, lease loss, tombstone, and release-SHA tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app backend/tests
git commit -m "feat: add durable document ingestion worker"
```

### Task 9: Implement lifecycle-filtered GPT-5 RAG streaming

**Files:**
- Create: `backend/app/services/rag_service.py`
- Create: `backend/app/api/chat.py`
- Create: `backend/tests/services/test_rag_service.py`
- Create: `backend/tests/api/test_chat_api.py`
- Create: `backend/tests/fixtures/prompt_injection.md`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing retrieval and citation tests**

```python
async def test_tombstoned_evidence_is_removed_before_model_call() -> None:
    h = RagHarness(search_results=[evidence("a"), evidence("b")], states={"a": "ready", "b": "deleting"})
    events = [event async for event in h.service.stream(question="What changed?", session_key=SESSION)]
    assert h.model.evidence_ids == ["a"]
    assert all("b" not in event.model_dump_json() for event in events)


async def test_unknown_model_citation_is_not_emitted() -> None:
    h = RagHarness(model_text="Answer [S99]", search_results=[evidence("S1")])
    events = [event async for event in h.service.stream(question="Q", session_key=SESSION)]
    assert not any(event.type == "citation" and event.citation_id == "S99" for event in events)
```

- [ ] **Step 2: Implement server-owned evidence blocks**

Assign IDs `S1` through `S8`; delimit every block as untrusted evidence; include file name, locator, and content; never place document text in system/developer instructions. Batch-read document state after Search and remove non-ready, expired, foreign, deleting, or tombstoned evidence.

- [ ] **Step 3: Implement the fixed GPT-5 Responses call**

Use `gpt-5`, medium reasoning effort, bounded output, streaming, and Microsoft Entra token auth. The instruction requires evidence-only answers, inline server IDs, and explicit insufficient-evidence behavior. Do not expose model chain-of-thought.

- [ ] **Step 4: Implement strict SSE**

Emit named `retrieval`, `token`, `citation`, `done`, and `error` events. Disable buffering and set `Cache-Control: no-cache`, `X-Accel-Buffering: no`, and correlation ID. Validate citations against retrieved IDs before emission. Cancel the model stream if the client disconnects.

- [ ] **Step 5: Test prompt injection and streaming contracts**

Run: `cd backend && uv run pytest tests/services/test_rag_service.py tests/api/test_chat_api.py -q`

Expected: session filtering, lifecycle postfilter, insufficient evidence, prompt injection, disconnect cancellation, event order, citation validation, and quota tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app backend/tests
git commit -m "feat: stream grounded GPT-5 answers"
```

### Task 10: Implement expiry cleanup and privacy-safe telemetry

**Files:**
- Create: `backend/app/cleanup.py`
- Create: `backend/app/core/telemetry.py`
- Create: `backend/tests/test_cleanup.py`
- Create: `backend/tests/test_telemetry.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing cleanup tests**

Test that expired documents are tombstoned, fenced, and removed; user-created `deleting` records are picked up durably; busy leases remain pending; 48-hour tombstones are removed only after artifacts are absent; and active sessions remain untouched.

- [ ] **Step 2: Implement the scheduled cleanup command**

Scan expiry partitions/pages without loading the whole table. Reuse `DeletionService`, cap concurrency, return a nonzero process exit only for systemic failures, and emit counts for deleted, pending, skipped, and failed records.

- [ ] **Step 3: Add telemetry redaction tests**

```python
@pytest.mark.parametrize("secret", ["sig=abc", "cu_session=raw", "SAS_TOKEN", "full document text"])
def test_sensitive_values_are_redacted(secret: str) -> None:
    assert secret not in sanitize_attributes({"url": f"https://blob/?{secret}", "cookie": secret, "content": secret}).values()
```

- [ ] **Step 4: Configure OpenTelemetry**

Instrument FastAPI, HTTPX, Azure SDK dependencies, queue processing, and custom spans. Record IDs, states, counts, durations, status codes, model deployment, and release SHA. Never record cookies, SAS query strings, document content, extraction JSON, full questions, or prompts.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && uv run pytest tests/test_cleanup.py tests/test_telemetry.py -q`

Expected: cleanup fencing and redaction tests pass.

```bash
git add backend/app backend/tests
git commit -m "feat: add retention cleanup and telemetry"
```

### Task 11: Build the Technical Console shell and design system

**Required skill:** Read and apply the `frontend-design` skill before editing frontend UI files.

**Files:**
- Create: `frontend/src/domain/types.ts`
- Create: `frontend/src/styles/tokens.css`
- Create: `frontend/src/styles/global.css`
- Create: `frontend/src/styles/layout.css`
- Create: `frontend/src/components/StatusBadge.tsx`
- Create: `frontend/src/components/MetricCard.tsx`
- Create: `frontend/src/components/ErrorNotice.tsx`
- Modify: `frontend/src/app/App.tsx`
- Modify: `frontend/src/app/App.test.tsx`

- [ ] **Step 1: Write failing semantic layout tests**

```tsx
test("exposes the three technical-console regions", () => {
  render(<App />);
  expect(screen.getByRole("complementary", { name: /documents/i })).toBeVisible();
  expect(screen.getByRole("main", { name: /pipeline inspector/i })).toBeVisible();
  expect(screen.getByRole("region", { name: /grounded chat/i })).toBeVisible();
});
```

- [ ] **Step 2: Implement tokens and global behavior**

Define midnight navy surfaces, cyan active/success, indigo model operations, amber warning, system sans plus monospace metrics, WCAG AA text contrast, 44px targets, `:focus-visible`, and `prefers-reduced-motion`. Do not use a generic dashboard template or gradients as decoration; preserve the approved workbench character.

- [ ] **Step 3: Implement responsive shell**

Desktop: `190px minmax(0, 1fr) 320px`. Tablet: documents + inspector with chat drawer. Mobile: accessible tablist for Documents, Inspector, Chat. Header shows service health, release SHA, session expiry, region disclosure, and safety notice.

- [ ] **Step 4: Verify UI shell**

Run: `cd frontend && npm test -- --run src/app/App.test.tsx && npm run typecheck && npm run build`

Expected: semantic regions and responsive CSS compile; no accessibility violations in the shell test.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat: build technical console shell"
```

### Task 12: Implement document upload, state, and inspector UI

**Files:**
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/features/documents/useDocuments.ts`
- Create: `frontend/src/features/documents/DocumentUploader.tsx`
- Create: `frontend/src/features/documents/DocumentList.tsx`
- Create: `frontend/src/features/documents/PipelineInspector.tsx`
- Create: `frontend/src/features/documents/ExtractionViewer.tsx`
- Create: `frontend/tests/documents.test.tsx`
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Write failing upload journey tests**

Use MSW to test init → XHR blob upload progress → complete → polling. Verify unsupported type, 100 MB + 1, quota, retry, delete `202`, and cross-state controls.

- [ ] **Step 2: Implement typed API and upload progress**

Use `fetch` for JSON with `credentials: "include"` and `XMLHttpRequest` only for direct Blob upload progress. Send exactly the returned storage headers. Never log the SAS URL; discard it after upload completion.

- [ ] **Step 3: Implement document state and polling**

Poll active documents with visibility-aware backoff from 1 to 10 seconds; stop on terminal states. Show `result_cleanup_pending` as retrying. Keep quota totals synchronized with `/api/session`.

- [ ] **Step 4: Implement inspector and safe extraction rendering**

Render JSON using text nodes and a syntax highlighter that never enables HTML. Render Markdown as source text/sections without raw HTML. Show category, page/slide locator, chunks, vector dimensions, token counts, phase timings, and correlation ID.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --run tests/documents.test.tsx && npm run typecheck`

Expected: all state transitions and error paths pass.

```bash
git add frontend/src frontend/tests
git commit -m "feat: add document ingestion console"
```

### Task 13: Implement grounded chat, SSE, citations, and accessibility

**Files:**
- Create: `frontend/src/api/sse.ts`
- Create: `frontend/src/features/chat/useGroundedChat.ts`
- Create: `frontend/src/features/chat/GroundedChat.tsx`
- Create: `frontend/src/features/chat/CitationList.tsx`
- Create: `frontend/src/features/chat/RetrievalDiagnostics.tsx`
- Create: `frontend/tests/chat.test.tsx`
- Create: `frontend/e2e/console.spec.ts`
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Write failing SSE parser and UI tests**

Test fragmented UTF-8 chunks, multiline `data:`, all five event types, abort, reconnect prohibition, malformed events, validated citations, and insufficient-evidence display.

- [ ] **Step 2: Implement streaming client and state reducer**

Use `fetch` + `ReadableStream` so POST bodies and `AbortController` are supported. Parse named events; append token text; map citations by ID; expose retrieval latency, reranker score, source preview, token usage, and total latency. Keep at most six turns in `sessionStorage`.

- [ ] **Step 3: Implement the approved chat UI**

Disable send while streaming, support Stop, preserve keyboard focus, announce tokens through a throttled polite live region, and make citation chips open source previews. Clear/cancel in-flight chat before document deletion.

- [ ] **Step 4: Add Playwright and accessibility coverage**

Mock APIs for deterministic browser tests. Cover desktop three-pane, mobile tabs, keyboard-only upload/chat/citation, reduced motion, retry, and deletion. Run Axe on main states.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --run && npm run e2e && npm run build`

Expected: component and browser tests pass with no serious Axe findings.

```bash
git add frontend
git commit -m "feat: add grounded chat diagnostics"
```

### Task 14: Containerize and run the complete local stack

**Executor constraint:** Dispatch this task with model **Claude Opus 4.8** because Docker and NGINX are deployment artifacts.

**Files:**
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx/default.conf.template`
- Create: `frontend/nginx/entrypoint.sh`
- Modify: `compose.yml`
- Create: `.env.example`
- Create: `backend/tests/test_container_contract.py`
- Create: `frontend/e2e/container.spec.ts`

- [ ] **Step 1: Write failing container contract tests**

Assert nonroot users, fixed health endpoints, backend command overrides for API/worker/cleanup, frontend `/api` proxy, `X-Accel-Buffering: no`, 100 MB upload behavior, and security headers.

- [ ] **Step 2: Build a shared backend image**

Use a pinned Python 3.12 slim base, `uv sync --frozen --no-dev`, nonroot UID, read-only-friendly filesystem, and `HEALTHCHECK` for API. Do not bake credentials or environment files. The same image must accept API, worker, and cleanup commands.

- [ ] **Step 3: Build the frontend image**

Use Node 22 for build and unprivileged NGINX for runtime. Generate upstream config at startup from `API_UPSTREAM`. Set CSP, HSTS only when HTTPS, frame denial, content-type protection, referrer policy, permissions policy, body limit, proxy timeouts, and SSE buffering off.

- [ ] **Step 4: Add local composition**

Compose starts Azurite, API, worker, frontend, and a one-shot test bootstrap. Local fake adapters are enabled only by `APP_MODE=local`; deployed configuration refuses that mode. Mount no source credentials into images.

- [ ] **Step 5: Verify images and local browser flow**

Run: `docker compose build --pull`

Run: `docker compose up -d && cd frontend && npm run e2e -- --grep "container"`

Expected: health checks pass; fixture upload, processing through fake CU/model adapters, citation display, and deletion pass.

- [ ] **Step 6: Commit**

```bash
git add backend/Dockerfile frontend/Dockerfile frontend/nginx compose.yml .env.example backend/tests frontend/e2e
git commit -m "build: containerize the workshop application"
```

### Task 15: Author Bicep infrastructure with Azure Verified Modules

**Executor constraint:** Dispatch this task with model **Claude Opus 4.8** and require it to read current Bicep best practices. Use AVM where available; do not replace Bicep with Terraform, ARM JSON, Pulumi, or generated CLI provisioning.

**Files:**
- Create: `azure.yaml`
- Create: `infra/main.bicep`
- Create: `infra/main.bicepparam`
- Create: `infra/modules/naming.bicep`
- Create: `infra/modules/role-definitions.bicep`
- Create: `infra/modules/data.bicep`
- Create: `infra/modules/foundry.bicep`
- Create: `infra/modules/compute.bicep`
- Create: `infra/modules/monitoring.bicep`
- Create: `infra/modules/alerts.bicep`
- Create: `infra/tests/main.test.bicepparam`

- [ ] **Step 1: Pin discovered AVM modules**

Use these module references discovered on 2026-09-03:

```bicep
br/public:avm/res/app/managed-environment:0.15.0
br/public:avm/res/app/container-app:0.23.0
br/public:avm/res/app/job:0.7.2
br/public:avm/res/container-registry/registry:0.13.0
br/public:avm/res/storage/storage-account:0.33.0
br/public:avm/res/search/search-service:0.13.0
br/public:avm/res/operational-insights/workspace:0.16.1
br/public:avm/res/insights/component:0.8.0
br/public:avm/res/managed-identity/user-assigned-identity:0.6.0
br/public:avm/res/cognitive-services/account:0.19.0
br/public:avm/res/authorization/role-assignment/rg-scope:0.1.1
```

`Microsoft.CognitiveServices/accounts/deployments` has no standalone AVM; configure both deployments through the cognitive-services account AVM child-resource input or a schema-verified child resource. Do not invent a module.

- [ ] **Step 2: Write failing IaC policy tests**

Create a PowerShell/Python test that compiles Bicep and inspects template JSON. Assert resource-group target scope; Southeast Asia app/data resources; East US 2 Foundry; no secrets, listKeys, Shared Key, Search keys, Foundry keys, or ACR admin; exactly two application image parameters; the one backend image parameter is applied identically to API, worker, and cleanup; two worker queue rules; managed identities; Basic Search/ACR; 24-hour blob lifecycle; multiple revision mode; seven alert categories; bootstrap image only as a first-run default; and every API, worker, cleanup, ACR-pull, Foundry-system, local-bootstrap, and GitHub-deployment role/scope from the specification's runtime RBAC matrix.

- [ ] **Step 3: Implement naming, identities, data, and monitoring**

Use deterministic `uniqueString(subscription().id, resourceGroup().id, environmentName)`. Create separate API, worker, cleanup, and ACR-pull identities. Accept a required `deploymentPrincipalId` for the local bootstrap principal. When nonempty `githubOwner` and `githubRepository` parameters are supplied, also create the GitHub deployment UAMI and a federated credential with subject `repo:{owner}/{repository}:environment:production`, audience `api://AzureADTokenExchange`, and issuer `https://token.actions.githubusercontent.com`.

Assign both bootstrap principals the data-plane roles required by `bootstrap-data-plane.py`: Storage Blob Delegator and Storage Blob Data Contributor on Storage, Search Service Contributor plus Search Index Data Contributor/Reader on Search, and Cognitive Services OpenAI User plus Cognitive Services Content Understanding Owner on Foundry. The GitHub identity additionally receives Contributor and Role Based Access Control Administrator on the application resource group. Configure StorageV2 Standard LRS with `allowSharedKeyAccess: false`, TLS 1.2+, no public blob access, browser CORS methods `PUT,OPTIONS`, allowed headers `content-type,x-ms-blob-type,x-ms-version`, exposed headers `etag,x-ms-request-id`, Blob/Queue/Table child resources, and lifecycle rules. Configure Search Basic with local auth disabled. Configure capped Log Analytics and workspace-based Application Insights.

Create Azure Monitor alerts for ingestion poison depth, Content Understanding cleanup backlog, oldest queue-message age, ingestion failures, API 5xx rate, end-to-end latency, and model `429` throttling. Route them to a parameterized action group email only when a nonempty operations email is supplied; alerts still deploy without an action group for workshop inspection.

- [ ] **Step 4: Implement Foundry in East US 2**

Deploy one `AIServices` account with system identity, custom subdomain, key access disabled, `gpt-5` Global Standard, and `text-embedding-3-large` Standard with 3,072 dimensions. Use parameters for capacity but not model ID substitution. Assign explicit account-scoped roles from the design.

- [ ] **Step 5: Implement Container Apps**

Create environment, frontend/API multiple-revision apps, worker no-ingress app with two managed-identity Azure Queue rules, and hourly cleanup job. Configure ACR pull by UAMI, probes, nonroot containers, resource limits, all endpoint/account/deployment environment variables, Application Insights connection string, and `RELEASE_SHA`. API ingress is internal. Initial image parameters default to Microsoft hello-world bootstrap images; repeated provisioning receives active immutable digests.

- [ ] **Step 6: Compile and validate locally**

Run: `az bicep format --file infra/main.bicep`

Run: `az bicep build --file infra/main.bicep`

Run: `uv --project backend run pytest infra/tests -q`

Expected: zero Bicep errors or unknown-property/type warnings, and every compiled-template policy assertion passes. Live `az deployment group validate` is deferred to Task 19 after the user selects a subscription and the bootstrap resource group exists.

- [ ] **Step 7: Commit**

```bash
git add azure.yaml infra
git commit -m "feat: provision workshop infrastructure with Bicep"
```

### Task 16: Implement data-plane bootstrap and candidate deployment

**Executor constraint:** Dispatch this task with model **Claude Opus 4.8**. All resource creation remains in Bicep; scripts may only perform documented data-plane and revision operations.

**Files:**
- Create: `scripts/bootstrap-data-plane.py`
- Create: `scripts/deploy_revisions.py`
- Create: `scripts/deploy.ps1`
- Create: `scripts/deploy.sh`
- Create: `scripts/smoke_test.py`
- Create: `scripts/verify_reprovision.py`
- Create: `scripts/tests/test_bootstrap.py`
- Create: `scripts/tests/test_deploy_revisions.py`
- Create: `scripts/tests/test_smoke_test.py`
- Create: `scripts/tests/test_verify_reprovision.py`
- Modify: `azure.yaml`

- [ ] **Step 1: Write failing deployment state-machine tests**

Use a fake Azure command adapter. Assert exact phases: preflight → preserve active digests → `azd provision` → bootstrap → build/use digests → API release label → frontend candidate label → drain/pause → candidate worker → smoke → cleanup image → traffic shift. Inject failure after every phase and assert API/frontend traffic, worker/cleanup digests, and queue scaling return to prior values.

- [ ] **Step 2: Implement idempotent data-plane bootstrap**

Use `DefaultAzureCredential`; create/replace four analyzers and router; configure Content Understanding defaults; create/update Search index; upload a tiny fixture; prove analyze, GET, DELETE `204`, embedding length 3,072, Search write/query/delete, and `gpt-5` response using tokens only. Delete all verification artifacts. Exit nonzero on key fallback or model mismatch.

- [ ] **Step 3: Implement digest-preserving provisioning**

Before `azd provision`, read existing images from all four compute targets. Require API, worker, and cleanup to use the same backend digest; abort with repair guidance if drift exists. Set `AZURE_FRONTEND_IMAGE` and that one shared `AZURE_BACKEND_IMAGE` to existing digests or bootstrap images for first run. Never pass mutable tags to Bicep.

- [ ] **Step 4: Implement candidate revisions and rollback**

Use Azure CLI JSON output, not parsed tables. Build the internal API label as `"r-" + release_sha[:12].lower()` and use `candidate` for the public frontend. Pause worker queue rules only after draining with a timeout; update worker; run smoke against the frontend candidate-label URL; assert API and worker `releaseSha`; update cleanup; shift traffic; retain current/previous labels. A `try/finally` rollback restores every saved value.

- [ ] **Step 5: Implement PowerShell and Bash wrappers**

Both wrappers expose the same options: environment name, subscription, resource group, app location default `southeastasia`, Foundry location fixed `eastus2`, repository setup switch, and supplied image digests. They validate tools/login, create the RG only during local bootstrap, run preflight, and call the Python deployment state machine. Bash files must use LF; PowerShell must never use `$Args` as a parameter name.

- [ ] **Step 6: Implement deployed smoke test**

Create session, upload a small PDF, complete, wait with a bounded timeout, assert `ready`, category, extraction, 3,072 dimensions, candidate API/worker release SHA, ask known question, validate citation/source/diagnostics, delete, then issue a new RAG request and verify the tombstoned source is excluded.

- [ ] **Step 7: Implement repeated-provision verification**

`verify_reprovision.py` snapshots the active frontend digest and asserts API, worker, and cleanup share one backend digest. It invokes `azd provision` twice with those two digests as Bicep parameters and asserts all four targets retain byte-for-byte identical digests after each run. Unit tests fake stable, regressing, and preexisting-backend-drift cases. The live invocation runs in Task 19 after Azure bootstrap.

- [ ] **Step 8: Verify deployment code and commit**

Run: `uv --project backend run pytest scripts/tests -q`

Run: `pwsh -File scripts/deploy.ps1 -WhatIf`

Run: `bash -n scripts/deploy.sh`

Expected: state-machine, every-phase rollback, command quoting, LF, idempotent bootstrap, and smoke client tests pass.

```bash
git add scripts azure.yaml
git commit -m "feat: add safe Bicep deployment automation"
```

### Task 17: Add GitHub CI, CodeQL, Copilot review setup, and deployment

**Executor constraint:** Dispatch deployment workflow and OIDC/Bicep integration review with model **Claude Opus 4.8**.

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/codeql.yml`
- Create: `.github/workflows/deploy.yml`
- Create: `.github/copilot-instructions.md`
- Create: `.github/CODEOWNERS`
- Create: `scripts/configure-github.ps1`
- Create: `scripts/tests/test_workflows.py`

- [ ] **Step 1: Write failing workflow policy tests**

Parse YAML and assert pinned action SHAs, minimum permissions, PR-only CI/CodeQL, main-only deployment, OIDC `id-token: write`, no Azure secret credentials, production environment, concurrency, immutable SHA tags, test/scan dependency before build, digest handoff, `azd provision`, and candidate smoke gate.

- [ ] **Step 2: Implement CI and CodeQL**

CI matrices run backend lint/type/tests/coverage, frontend lint/type/tests/build, Bicep format/build/policy tests, both Docker builds, and Playwright mock E2E. CodeQL initializes Python and JavaScript/TypeScript and uploads SARIF. Pin every third-party action by full commit SHA with a version comment.

- [ ] **Step 3: Implement main deployment workflow**

Authenticate with `azure/login` OIDC, run all required checks, log in to ACR with an Entra token, build/push exactly two commit-SHA images, resolve digests, run `azd provision` with preserved/current image parameters, invoke the same deployment state machine, and upload sanitized smoke logs. Use production concurrency with no overlapping deployment.

- [ ] **Step 4: Add Copilot review instructions**

Tell Copilot to focus on session isolation, mandatory Search filters, SAS leakage, keyless auth, prompt injection, citation validation, queue/lease idempotency, Bicep-only infrastructure, revision rollback, and GPT-5 model lock. Exclude generated lockfiles and snapshots from review where GitHub rulesets support exclusions.

- [ ] **Step 5: Implement GitHub setup script**

Using `gh api`, create production environment variables and the main branch ruleset with required checks and automatic Copilot review on open and new pushes. Set the repository/environment values as `azd` parameters and rerun resource-group-scoped Bicep to create the deployment identity and environment-scoped federated credential; Azure CLI must not create those resources imperatively. Print exact manual instructions if the plan/account cannot enable Copilot review. Never store a client secret.

- [ ] **Step 6: Verify and commit**

Run: `uv --project backend run pytest scripts/tests/test_workflows.py -q`

Run: `actionlint .github/workflows/*.yml`

Expected: policy tests and workflow lint pass.

```bash
git add .github scripts/configure-github.ps1 scripts/tests
git commit -m "ci: add secure GitHub delivery pipeline"
```

### Task 18: Complete end-to-end quality gates and workshop documentation

**Files:**
- Create: `docs/workshop/README.md`
- Create: `docs/workshop/architecture.md`
- Create: `docs/workshop/facilitator-guide.md`
- Create: `docs/workshop/troubleshooting.md`
- Create: `docs/security.md`
- Create: `docs/deployment.md`
- Create: `tests/fixtures/general-business.pdf`
- Create: `tests/fixtures/general-business.docx`
- Create: `tests/fixtures/market-review.pptx`
- Create: `tests/fixtures/diagram.png`
- Create: `tests/fixtures/invoice.pdf`
- Create: `tests/fixtures/receipt.jpg`
- Create: `tests/fixtures/contract.pdf`
- Modify: `README.md`

- [ ] **Step 1: Create non-sensitive deterministic fixtures and expected answers**

Use synthetic Contoso/Fabrikam content only. Include at least one PDF, DOCX, PPTX, PNG, and JPEG. Store expected category, key fields, answer phrases, and source locators in `tests/fixtures/expected.json`. Keep each fixture small enough for inexpensive deployed smoke tests. `smoke_test.py --all-formats` uploads each file, waits for readiness, verifies extraction/category, asks one shared evidence question, and deletes every artifact.

- [ ] **Step 2: Run the complete local quality gate**

Run backend lint, mypy, unit/integration coverage, frontend lint/type/unit/E2E/build, Bicep compile/policy tests, workflow tests, Docker builds, secret scan, and dependency audit. Require no high/critical dependency findings and no committed secrets.

- [ ] **Step 3: Write workshop and operations documentation**

Document prerequisites, architecture, 90-minute agenda, deploy/remove commands, GitHub flow, safe sample data, cross-region disclosure, cost controls, quota troubleshooting, Content Understanding failure recovery, candidate rollback, and cleanup verification. State clearly that runtime is `gpt-5`; Claude Opus 4.8 is only the implementation-agent preference.

- [ ] **Step 4: Verify docs and links**

Run Markdown lint and link checking. Confirm every command matches actual scripts and no nonexistent file is referenced.

- [ ] **Step 5: Commit**

```bash
git add README.md docs tests
git commit -m "docs: add workshop delivery guide"
```

### Task 19: Create the public GitHub repository and deploy to Azure

**Files:**
- Modify only if validation finds a defect; otherwise this task changes remote systems.

- [ ] **Step 1: Run pre-publish verification**

Run the complete Task 18 gate and `git status --short`. Expected: all checks pass and the tree is clean.

- [ ] **Step 2: Create the empty public repository without pushing**

Run:

```powershell
gh auth status
gh repo create content-understanding-rag-demo --public --source . --remote origin
```

Expected: repository URL is returned, `origin` is configured, and no workflow has run because no branch has been pushed.

- [ ] **Step 3: Select Azure subscription and bootstrap once locally**

List accessible subscriptions, let the user choose if more than one exists, then run `az deployment group validate` against the selected bootstrap resource group and execute the PowerShell deployment script. The script creates the RG, provisions Bicep, bootstraps data plane, builds images, deploys candidates, validates, and promotes.

- [ ] **Step 4: Configure GitHub environment and OIDC before first push**

Run `scripts/configure-github.ps1 -PrePush` for the new repository and production environment. It creates GitHub variables, supplies owner/repository parameters to Bicep, reprovisions the GitHub deployment UAMI/federated credential and roles, and reads back the credential to assert issuer, subject `repo:{owner}/{repository}:environment:production`, and audience. A local process cannot obtain GitHub's environment OIDC token, so token exchange is intentionally verified by a minimal first job in the main workflow. Verify no repository secret contains an Azure credential.

- [ ] **Step 5: Prove repeated Bicep provisioning preserves images**

Run `scripts/verify_reprovision.py` against the deployed environment. Expected: two real `azd provision` executions succeed and all frontend/API/worker/cleanup digests remain unchanged.

- [ ] **Step 6: Push main and apply branch rules**

Run `git push -u origin main`. Because environment variables and the federated credential already exist, the initial main workflow first obtains a GitHub OIDC token and runs `azure/login`, then proceeds only if `az account show` confirms the expected subscription and principal. Apply `scripts/configure-github.ps1 -ApplyRuleset` after the check names exist and verify required checks plus automatic Copilot review are active, or record the documented account-level blocker.

- [ ] **Step 7: Validate live application and pipeline**

Run `scripts/smoke_test.py --all-formats` against the production URL. Open a test pull request to verify CI, CodeQL, and Copilot review. Merge only after checks pass, then verify the main workflow deploys immutable images and passes its candidate smoke test.

- [ ] **Step 8: Record deployment outputs**

Add the live URL, repository URL, regions, deployed model IDs, resource group, cleanup command, and last smoke correlation ID to the final delivery report. Do not commit subscription IDs, tenant IDs, principal IDs, SAS values, or tokens.

- [ ] **Step 9: Final commit if documentation outputs changed**

```bash
git add README.md docs
git commit -m "docs: record workshop deployment"
git push
```

---

## Final verification matrix

| Requirement | Evidence |
| --- | --- |
| Public English Technical Console | Playwright desktop/mobile and live URL |
| PDF/DOCX/PPTX/PNG/JPEG, max 100 MB | validation unit tests and deployed upload smoke |
| Content Understanding API and four schemas | analyzer contract tests and live extraction |
| Managed identity/keyless runtime | Bicep policy test and token-only bootstrap probes |
| App/data in Southeast Asia | Bicep outputs and Azure resource query |
| Foundry in East US 2 | Bicep output and model deployment query |
| `gpt-5` runtime | settings test, Bicep deployment, smoke metadata |
| `text-embedding-3-large` / 3,072 | adapter and Search schema tests |
| Anonymous 24-hour isolation | API, cross-session, cleanup, and lifecycle tests |
| Queue durability and lease fencing | redelivery, cleanup queue, concurrency, and Azurite tests |
| Hybrid RAG with citations/diagnostics | service tests, UI tests, deployed smoke |
| Frontend/backend Container Apps | Bicep outputs and live revisions |
| Bicep IaC | no alternate IaC files; Bicep compile/validate |
| Claude Opus 4.8 for container/deployment/IaC authoring | implementation dispatch record for Tasks 14–17 |
| GitHub CodeQL and Copilot review | PR evidence and ruleset query |
| Build → ACR → candidate deploy | main workflow run and immutable digests |
| Rollback safety | deployment state-machine tests and injected failure test |
