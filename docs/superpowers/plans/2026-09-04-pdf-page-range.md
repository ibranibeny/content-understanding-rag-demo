# PDF Page Range Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users choose All pages, an inclusive Start/End range, or an advanced finite page selection for PDF processing, with a maximum of 300 unique pages per Content Understanding request.

**Architecture:** Normalize and validate page selections at the backend boundary, persist the canonical `contentRange` on `DocumentRecord`, and let the worker read that authoritative value when calling Content Understanding. The React uploader keeps a selected PDF pending while the user chooses one of three modes, performs equivalent immediate validation, and submits the canonical range in upload initialization; existing non-PDF and range-less contracts remain compatible.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest, React 19, TypeScript 5.9, Vitest/Testing Library, Azure Content Understanding REST `2025-11-01`, GitHub Actions, Azure Container Apps.

---

## File Structure

- Create `backend/app/services/content_range.py`: one focused parser/normalizer for finite, 1-based PDF page selections.
- Create `backend/tests/services/test_content_range.py`: exhaustive unit contract for syntax, normalization, overlap, and the 300-page limit.
- Modify `backend/app/domain/models.py`: optional persisted/request/response `content_range` fields.
- Modify `backend/app/services/upload_service.py`: PDF-only authoritative validation and persistence.
- Modify `backend/app/services/content_understanding.py`: optional `contentRange` in the analyze input JSON.
- Modify `backend/app/services/ingestion_service.py`: pass the persisted range to the Content Understanding client.
- Modify backend tests listed below: prove API, persistence compatibility, service request shape, and retry/redelivery behavior.
- Create `frontend/src/features/documents/pageRange.ts`: pure UI normalization using the same finite grammar.
- Create `frontend/src/features/documents/pageRange.test.ts`: frontend boundary cases.
- Modify `frontend/src/features/documents/DocumentUploader.tsx`: pending-file workflow and All/Simple/Advanced controls.
- Modify `frontend/src/features/documents/useDocuments.ts`: upload accepts an optional canonical range.
- Modify `frontend/src/api/client.ts`: include optional `contentRange` in initialization JSON.
- Modify `frontend/src/domain/types.ts`: optional response metadata and page-range types.
- Modify `frontend/src/features/documents/PipelineInspector.tsx`: display requested page scope.
- Create focused frontend component tests and update application tests.
- Modify `scripts/smoke_test.py` and its tests: support generated multipage PDFs and range assertions.
- Modify `.github/workflows/deploy.yml` and workflow contract tests: run both the existing full smoke and a finite-range production smoke.

### Task 1: Backend Page-Range Value Parser

**Files:**
- Create: `backend/app/services/content_range.py`
- Create: `backend/tests/services/test_content_range.py`

- [ ] **Step 1: Write failing normalization tests**

Create tests that import `normalize_content_range` and assert:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", "1"), (" 1 - 3 , 5 , 9 - 12 ", "1-3,5,9-12"), ("7-7", "7")],
)
def test_normalizes_finite_one_based_ranges(raw: str, expected: str) -> None:
    assert normalize_content_range(raw) == expected
```

Also assert `1-300` is accepted and represents exactly 300 selected pages.

- [ ] **Step 2: Run the normalization tests and verify RED**

Run from `backend`:

```text
uv run pytest tests/services/test_content_range.py -q
```

Expected: collection fails because `app.services.content_range` does not exist.

- [ ] **Step 3: Write failing rejection tests**

Parameterize `""`, whitespace, `"0"`, `"3-1"`, `"1-"`, `"-5"`, `"a"`, `"1,,2"`, `"1-3,3"`, `"1-5,2-4"`, and `"1-301"`. Require a dedicated `InvalidContentRange` exception with a stable reason enum/value so callers do not parse exception text.

- [ ] **Step 4: Implement the minimal parser**

Implement:

```python
MAX_CONTENT_PAGES = 300

class InvalidContentRange(ValueError):
    pass

def normalize_content_range(value: str) -> str:
    # Strip whitespace around tokens and hyphens.
    # Accept only digits or one closed inclusive range per comma-delimited token.
    # Require values >= 1 and start <= end.
    # Track selected integers in a set; reject an overlap/duplicate.
    # Reject once the set exceeds MAX_CONTENT_PAGES.
    # Emit `n` for equal bounds and `start-end` otherwise, preserving token order.
```

Do not add a PDF library or accept open-ended ranges.

- [ ] **Step 5: Run parser tests and full backend static checks**

Run:

```text
uv run pytest tests/services/test_content_range.py -q
uv run ruff check app/services/content_range.py tests/services/test_content_range.py
uv run mypy app/services/content_range.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```text
git add backend/app/services/content_range.py backend/tests/services/test_content_range.py
git commit -m "feat: validate PDF content ranges"
```

### Task 2: Persist the Canonical Range at Upload Initialization

**Files:**
- Modify: `backend/app/domain/models.py`
- Modify: `backend/app/services/upload_service.py`
- Modify: `backend/tests/test_models.py`
- Modify: `backend/tests/api/test_upload_api.py`
- Modify: `backend/tests/services/test_upload_service.py`
- Modify: `backend/tests/repositories/test_table_repository.py`

- [ ] **Step 1: Write failing model and API tests**

Add tests proving:

```python
request = UploadInitRequest.model_validate({
    "fileName": "report.pdf",
    "contentType": "application/pdf",
    "sizeBytes": 8,
    "contentRange": " 1 - 3 , 5 ",
})
assert request.content_range == " 1 - 3 , 5 "
```

Post the same request to `/api/uploads/init`, then inspect the memory repository and require `content_range == "1-3,5"`. Existing requests without `contentRange` must still return the exact safe response shape and persist `None`.

Add rejection cases:

- malformed/overlapping/over-300 PDF selection → HTTP 400, `invalid_content_range`, non-retryable;
- any range with DOCX, PPTX, PNG, or JPEG → HTTP 400, `content_range_not_supported`, non-retryable;
- an unknown client field remains HTTP 422.

- [ ] **Step 2: Run targeted tests and verify RED**

Run:

```text
uv run pytest tests/test_models.py tests/api/test_upload_api.py tests/services/test_upload_service.py -q
```

Expected: failures because `contentRange` is currently forbidden or absent.

- [ ] **Step 3: Add optional fields to contracts**

Add `content_range: str | None = None` to:

- `UploadInitRequest`
- `DocumentRecord`
- `DocumentResponse`
- `DocumentSummaryResponse`

Keep the alias generator so JSON uses `contentRange`. Do not add the field to `IngestionMessage`.

- [ ] **Step 4: Validate and persist in `UploadService.initialize()`**

After existing declared-file validation:

```python
content_range = request.content_range
if content_range is not None:
    if declared.content_type != "application/pdf":
        raise AppError(
            "content_range_not_supported", 400,
            "Page selection is supported only for PDF files.", False,
        )
    try:
        content_range = normalize_content_range(content_range)
    except InvalidContentRange:
        raise AppError(
            "invalid_content_range", 400,
            "Choose 1 to 300 pages using values such as 1-3,5,9-12.", False,
        ) from None
```

Pass the normalized value into `DocumentRecord`. Include `content_range` in all response builders by relying on Pydantic attribute validation or assigning it explicitly where constructors are manual.

- [ ] **Step 5: Add persistence compatibility coverage**

In table repository tests, encode/decode a new record with `contentRange`, and decode an old payload without it. Require the new value to round-trip and the old value to default to `None`.

- [ ] **Step 6: Run targeted tests and backend quality checks**

Run:

```text
uv run pytest tests/test_models.py tests/api/test_upload_api.py tests/services/test_upload_service.py tests/repositories/test_table_repository.py -q
uv run ruff check app/domain/models.py app/services/upload_service.py tests/test_models.py tests/api/test_upload_api.py tests/services/test_upload_service.py tests/repositories/test_table_repository.py
uv run mypy app/domain/models.py app/services/upload_service.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```text
git add backend/app/domain/models.py backend/app/services/upload_service.py backend/tests
git commit -m "feat: persist PDF page selections"
```

### Task 3: Send the Persisted Range to Content Understanding

**Files:**
- Modify: `backend/app/services/content_understanding.py`
- Modify: `backend/app/services/ingestion_service.py`
- Modify: `backend/tests/services/test_content_understanding.py`
- Modify: `backend/tests/services/test_ingestion_service.py`

- [ ] **Step 1: Write failing Content Understanding request tests**

Keep the existing no-range assertion exactly:

```json
{"inputs":[{"url":"https://blob.example/file.pdf?sig=secret"}]}
```

Add a test calling:

```python
await service.start_analysis(
    "https://blob.example/file.pdf?sig=secret",
    "business_document_router",
    "1-3,5",
)
```

and require the exact body:

```json
{"inputs":[{"url":"https://blob.example/file.pdf?sig=secret","contentRange":"1-3,5"}]}
```

- [ ] **Step 2: Write failing ingestion propagation test**

Update the ingestion fake to capture `(blob_url, analyzer_id, content_range)`. Create a PDF `DocumentRecord(content_range="301-600")`, process it, and assert the first start call received `"301-600"`. Extend the redelivery test to prove an existing operation is resumed without another start call and without changing the persisted range.

- [ ] **Step 3: Run the service tests and verify RED**

Run:

```text
uv run pytest tests/services/test_content_understanding.py tests/services/test_ingestion_service.py -q
```

Expected: failures due to the two-argument `start_analysis()` protocol and implementation.

- [ ] **Step 4: Implement optional range propagation**

Change both protocol and implementation to:

```python
async def start_analysis(
    self, blob_url: str, analyzer_id: str, content_range: str | None = None
) -> AnalysisStart:
    input_value = {"url": blob_url}
    if content_range is not None:
        input_value["contentRange"] = content_range
    # POST json={"inputs": [input_value]}
```

In ingestion, call:

```python
started = await self._content.start_analysis(
    read_url,
    self._analyzer_id,
    document.content_range,
)
```

Use the current persisted document value, not queue payload data.

- [ ] **Step 5: Run targeted and full backend checks**

Run:

```text
uv run pytest tests/services/test_content_understanding.py tests/services/test_ingestion_service.py -q
uv run ruff check app tests
uv run mypy app
uv run pytest -q
```

Expected: all commands exit 0 with no regressions.

- [ ] **Step 6: Commit**

```text
git add backend/app/services/content_understanding.py backend/app/services/ingestion_service.py backend/tests/services/test_content_understanding.py backend/tests/services/test_ingestion_service.py
git commit -m "feat: apply page range during analysis"
```

### Task 4: Frontend Range Normalization

**Files:**
- Create: `frontend/src/features/documents/pageRange.ts`
- Create: `frontend/src/features/documents/pageRange.test.ts`

- [ ] **Step 1: Write failing frontend parser tests**

Test these public functions:

```typescript
expect(normalizeAdvancedRange(" 1 - 3, 5 ")).toBe("1-3,5");
expect(normalizeSimpleRange("301", "600")).toBe("301-600");
expect(normalizeSimpleRange("7", "7")).toBe("7");
```

Require invalid syntax, zero, descending ranges, overlap/duplicates, missing simple bounds, and 301 selected pages to throw `PageRangeError`; require exactly 300 pages to pass.

- [ ] **Step 2: Run the focused test and verify RED**

Run from `frontend`:

```text
npm test -- --run src/features/documents/pageRange.test.ts
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement pure TypeScript normalization**

Export:

```typescript
export const MAX_CONTENT_PAGES = 300;
export class PageRangeError extends Error {}
export function normalizeAdvancedRange(value: string): string;
export function normalizeSimpleRange(start: string, end: string): string;
```

Mirror the backend finite grammar exactly. Keep this module independent of React and network code.

- [ ] **Step 4: Run focused tests, lint, and typecheck**

Run:

```text
npm test -- --run src/features/documents/pageRange.test.ts
npm run lint
npm run typecheck
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```text
git add frontend/src/features/documents/pageRange.ts frontend/src/features/documents/pageRange.test.ts
git commit -m "feat: normalize PDF page ranges in UI"
```

### Task 5: Add All, Start/End, and Advanced Upload UI

**Files:**
- Modify: `frontend/src/features/documents/DocumentUploader.tsx`
- Create: `frontend/src/features/documents/DocumentUploader.test.tsx`
- Modify: `frontend/src/styles/index.css` (or the existing stylesheet that owns `.uploader`)
- Modify: `frontend/src/app/App.tsx`

- [ ] **Step 1: Write failing component tests**

Cover these user behaviors with Testing Library:

1. Non-PDF selection immediately invokes `onUpload(file, undefined)` and never shows Page scope.
2. PDF selection shows a labeled `Page scope` radio group with All pages, Start / End, and Advanced.
3. All pages invokes `onUpload(file, undefined)` only after **Upload and process**.
4. Start/End `301` and `600` invokes `onUpload(file, "301-600")`.
5. Advanced `1-3,5` invokes `onUpload(file, "1-3,5")`.
6. Invalid or over-300 input shows inline error text and does not invoke `onUpload`.
7. **Choose another file** clears the pending selection and range error.
8. Busy state disables file-changing, mode, range, and submit controls.

- [ ] **Step 2: Run component tests and verify RED**

Run:

```text
npm test -- --run src/features/documents/DocumentUploader.test.tsx
```

Expected: failures because current uploader sends every file immediately and lacks range controls.

- [ ] **Step 3: Implement the pending-PDF state machine**

Change the callback contract to:

```typescript
onUpload: (file: File, contentRange?: string) => void;
```

Use local state for `pendingFile`, mode (`"all" | "simple" | "advanced"`), start, end, advanced value, and range error. For PDF, hold the file until submit; for all other allowed types, retain immediate upload behavior. Clear the native input value when resetting so reselecting the same PDF triggers `change`.

Use semantic elements:

- `fieldset` + `legend` for Page scope;
- `type="number"`, `min={1}` for start/end;
- persistent examples and `aria-describedby`;
- `role="alert"` for validation errors.

- [ ] **Step 4: Connect the revised callback in `App`**

Use:

```tsx
onUpload={(file, contentRange) => void documents.upload(file, contentRange)}
```

- [ ] **Step 5: Add compact console styling**

Extend the existing uploader stylesheet with scoped classes for the selected-file row, radio modes, two-column numeric range, advanced input, hint, error, and secondary action. Preserve the current palette, zero/low-radius technical-console character, visible focus, responsive stacking, and reduced-motion behavior.

- [ ] **Step 6: Run component and accessibility-facing app tests**

Run:

```text
npm test -- --run src/features/documents/DocumentUploader.test.tsx src/app/App.test.tsx
npm run lint
npm run typecheck
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```text
git add frontend/src/features/documents/DocumentUploader.tsx frontend/src/features/documents/DocumentUploader.test.tsx frontend/src/styles frontend/src/app/App.tsx
git commit -m "feat: add PDF page scope controls"
```

### Task 6: Carry Range Through the Frontend API and Inspector

**Files:**
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/api/client.test.ts`
- Modify: `frontend/src/domain/types.ts`
- Modify: `frontend/src/features/documents/useDocuments.ts`
- Modify: `frontend/src/features/documents/PipelineInspector.tsx`
- Create: `frontend/src/features/documents/PipelineInspector.test.tsx`

- [ ] **Step 1: Write failing API and inspector tests**

Require:

```typescript
await api.initUpload(pdf, "1-3,5");
expect(JSON.parse(fetchBody)).toEqual({
  fileName: pdf.name,
  contentType: "application/pdf",
  sizeBytes: pdf.size,
  contentRange: "1-3,5",
});
```

A call without a range must omit the key, not send `null` or an empty string.

Render the inspector with `contentRange: "301-600"` and require `Pages requested` → `301-600`; with absent metadata require `ALL`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```text
npm test -- --run src/api/client.test.ts src/features/documents/PipelineInspector.test.tsx
```

Expected: type/signature/assertion failures.

- [ ] **Step 3: Implement frontend data propagation**

Add `contentRange?: string | null` to `DocumentSummary`. Change:

```typescript
initUpload: (file: File, contentRange?: string) => {
  const body: Record<string, unknown> = {
    fileName: file.name,
    contentType: file.type,
    sizeBytes: file.size,
  };
  if (contentRange) body.contentRange = contentRange;
  return json<UploadInit>("/api/uploads/init", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
```

Change `upload(file, contentRange?)` in the hook and pass it to `api.initUpload`. Add a `Pages requested` metric/card in the inspector while preserving the existing metrics and responsive layout.

- [ ] **Step 4: Run all frontend tests and build**

Run:

```text
npm test -- --run
npm run lint
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```text
git add frontend/src/api frontend/src/domain/types.ts frontend/src/features/documents
git commit -m "feat: expose requested page scope"
```

### Task 7: Add Range-Aware Deployment Smoke Coverage

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `scripts/tests/test_smoke_test.py`
- Modify: `.github/workflows/deploy.yml`
- Modify: `scripts/tests/test_workflows.py`

- [ ] **Step 1: Extend the existing smoke tests**

In `scripts/tests/test_smoke_test.py`, add failing tests requiring:

- `make_sample_pdf(lines, page_count=3)` emits a valid three-page PDF;
- `SmokeConfig(content_range="2-3", expected_page_count=2)` sends `contentRange` during init;
- readiness polling rejects a ready document whose `pageCount` is not 2;
- the default smoke omits `contentRange` and remains backward compatible.

- [ ] **Step 2: Run smoke tests and verify RED**

Run from `backend`:

```text
uv run pytest ../scripts/tests/test_smoke_test.py -q
```

Expected: failures because those arguments and assertions do not exist.

- [ ] **Step 3: Implement multipage generation and range assertions**

Extend `SmokeConfig` with optional `content_range` and `expected_page_count`. Build a PDF page object and content stream per page while retaining correct object offsets and xref data. Include `contentRange` only when configured. Have `_poll_until_ready()` return the document JSON or separately validate `pageCount` before chat.

Add CLI options:

```text
--content-range 2-3
--expect-pages 2
--generated-pages 3
```

Reject range options for a non-PDF supplied file with a safe `SmokeError`.

- [ ] **Step 4: Add a second production smoke invocation**

Keep the existing full one-page smoke. Add a subsequent range smoke using the public frontend URL:

```text
uv --project backend run python scripts/smoke_test.py \
  --api-base "$FRONTEND_URL" \
  --frontend-origin "$FRONTEND_URL" \
  --generated-pages 3 \
  --content-range "2-3" \
  --expect-pages 2
```

When `skipLiveModel` is true, both invocations use `--skip-live-model`; page-count assertions only run in the full processing mode.

- [ ] **Step 5: Update workflow contract tests**

Require the deploy workflow to retain frontend-routed smoke and include `--generated-pages`, `--content-range`, and `--expect-pages` in the range invocation. Preserve OIDC, GitHub-hosted Docker builds, immutable SHA tags, ACR push, and Container Apps rollout assertions.

- [ ] **Step 6: Run scripts and workflow tests**

Run:

```text
uv run pytest ../scripts/tests/test_smoke_test.py ../scripts/tests/test_workflows.py -q
uv run ruff check ../scripts/smoke_test.py ../scripts/tests
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```text
git add scripts/smoke_test.py scripts/tests .github/workflows/deploy.yml
git commit -m "test: verify ranged PDF processing in production"
```

### Task 8: Full Verification, UI Inspection, Review, and Protected Release

**Files:**
- Modify only files required by findings from verification or review.

- [ ] **Step 1: Run complete backend verification**

From `backend`:

```text
uv sync
uv run ruff check app tests ../scripts
uv run mypy app
uv run pytest -q
```

Expected: exit 0, no lint/type/test failures.

- [ ] **Step 2: Run complete frontend verification**

From `frontend`:

```text
npm ci
npm test -- --run
npm run lint
npm run build
```

Expected: exit 0, no test/lint/type/build failures.

- [ ] **Step 3: Validate infrastructure and workflow contracts**

From repository root:

```text
az bicep build --file infra/main.bicep --stdout > $null
uv --project backend run pytest scripts/tests/test_workflows.py backend/tests/test_container_contract.py -q
```

Expected: exit 0.

- [ ] **Step 4: Inspect the UI in a browser**

Run the existing frontend development task/server, open the uploader, and inspect desktop and narrow-mobile layouts. Verify keyboard interaction, labels, focus, inline errors, disabled states, All/Simple/Advanced switching, and that non-PDF selection still uploads directly. Capture and correct any visual overflow or accessibility defect, then rerun Task 8 Steps 1–3 if code changes.

- [ ] **Step 5: Request independent code review**

Use a read-only code-review subagent with:

- requirement source: `docs/superpowers/specs/2026-09-04-pdf-page-range-design.md`;
- base SHA: `b2a2827b91ce1856618314765828fdab53ec7c8b`;
- head SHA: current feature HEAD;
- focus: validation parity, backward compatibility, persisted authority, retry behavior, safe errors, accessibility, and release smoke reliability.

Fix every Critical and Important finding using TDD and rerun full verification.

- [ ] **Step 6: Push branch and create PR**

Push `feature/pdf-page-range`, create a PR against `main`, and enable squash auto-merge. Do not bypass branch protection.

- [ ] **Step 7: Require protected checks and merge**

Wait for backend, frontend, Bicep, CodeQL Python, and CodeQL JavaScript/TypeScript to succeed. If any fails, diagnose, fix on the branch, rerun local verification, and push. Merge only when all required checks are green.

- [ ] **Step 8: Verify production deployment**

For the resulting `main` SHA, require CI, CodeQL, and Deploy conclusions to be `success`. Inspect deploy job steps for OIDC login, GitHub runner image builds, ACR push, Container Apps update, bootstrap, frontend proxy readiness, original full smoke, and page-range smoke.

- [ ] **Step 9: Perform fresh public verification**

Require HTTP 200 from frontend root, frontend `/api/session`, API live, and API readiness. Confirm the running frontend, API, and worker images use the merged SHA. Confirm the range smoke reports `ready`, expected requested page count, at least one citation, and deletion.

- [ ] **Step 10: Final report**

Report the PR, merge SHA, CI/CodeQL/Deploy run links, live URL, exact production range-smoke outcome, and any remaining non-blocking warning. Do not claim completion without fresh evidence from Steps 1–9.
