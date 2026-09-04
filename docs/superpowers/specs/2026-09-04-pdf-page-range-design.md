# PDF Page Range Processing Design

## Objective

Allow workshop users to upload a PDF once and choose which pages Azure Content Understanding processes. The feature prevents documents over the 300-page asynchronous analysis limit from failing when the user selects at most 300 pages.

## Scope

- Add PDF-only page scope controls to the upload flow.
- Support a simple inclusive start/end range and an advanced comma-separated range.
- Persist the normalized range with the document so retries and queue redelivery use the same selection.
- Pass the selection to Azure Content Understanding as the input `range` while retaining `contentRange` in the public app contract.
- Display the selected range in document details.
- Preserve existing behavior for Office documents and images.

This feature does not split or rewrite PDFs, discover a PDF's total page count, add dependencies, or change GPT-5, embeddings, Search, authentication, retention, or quotas.

## User Experience

After a PDF is chosen, the uploader keeps the file pending and shows a **Page scope** control with three modes:

1. **All pages**: no range is sent. This is suitable only when the PDF contains at most 300 pages.
2. **Start / End**: two 1-based inclusive page fields, such as start `301` and end `600`.
3. **Advanced**: a text field supporting single pages and closed ranges, such as `1-3,5,9-12`.

The user then selects **Upload and process**. A secondary action lets the user choose another file. Non-PDF files continue directly without page controls because Content Understanding page ranges are document-specific and the workshop does not need range selection for Office or image inputs.

The UI explains that one analysis can include at most 300 unique pages. **All pages** warns that PDFs over 300 pages must use a range. The uploader cannot know the source PDF's total page count without adding a PDF parser, so a selection beyond the actual last page is left for Content Understanding to validate.

## Range Rules

The canonical range is a string containing comma-separated 1-based pages or inclusive closed ranges:

- Valid: `1`, `1-300`, `1-3,5,9-12`
- Invalid: `0`, `3-1`, `1-`, `-5`, duplicate or overlapping selections, whitespace-only input, nonnumeric tokens

Validation normalizes insignificant whitespace and computes the number of unique selected pages. The count must be between 1 and 300. Duplicate and overlapping selections are rejected rather than silently merged so user intent remains explicit.

The simple mode is normalized to the same canonical form: equal values become `7`; differing values become `7-20`.

The backend is authoritative and applies the same validation. The frontend validation exists for immediate feedback only.

## API and Persistence

`UploadInitRequest` gains an optional `contentRange` property. It is accepted only when `contentType` is `application/pdf`. `null` or omission means all pages.

`DocumentRecord`, `DocumentResponse`, and `DocumentSummaryResponse` gain an optional `contentRange`. The normalized value is written when the upload reservation and document record are created. It is not client-modifiable after initialization.

`IngestionMessage` does not duplicate the range. The worker reads the authoritative range from `DocumentRecord`, which keeps queue retries, redelivery, and manual retries consistent.

`ContentUnderstandingClient.start_analysis()` gains an optional range and emits one input object:

- Without a range: `{ "url": "..." }`
- With a range: `{ "url": "...", "range": "1-3,5" }`

The public API, persistence model, and frontend continue to use `contentRange`; only the Azure REST adapter translates it to the 2025-11-01 AnalysisInput property `range`. Microsoft defines this range as 1-based for documents and supports forms such as `1-3,5,9-`. This workshop intentionally accepts only finite ranges so it can enforce the 300-page limit before service submission.

## Processing and Failure Handling

The existing upload, blob verification, outbox, worker, Content Understanding polling, extraction, chunking, embedding, indexing, retry, and deletion flow remains unchanged apart from range propagation.

Validation errors return the existing safe error envelope:

- `invalid_content_range`, HTTP 400, non-retryable, with actionable text
- `content_range_not_supported`, HTTP 400, non-retryable, when supplied for a non-PDF file

A Content Understanding rejection remains sanitized. The selected range is safe document metadata and appears in API responses and the inspector, but signed blob URLs and service response bodies remain hidden.

## Accessibility and Visual Integration

The new controls use the console's existing compact technical style rather than introducing a modal. Modes use a labeled radio group; numeric and advanced fields have persistent labels, examples, and inline error text connected with `aria-describedby`. Keyboard focus and disabled upload states follow existing controls.

The document inspector shows `PAGES REQUESTED` alongside the existing page, chunk, vector, and token metrics. The value is `ALL` or the canonical range.

## Compatibility

Existing persisted entities without `contentRange` deserialize as all pages because the field defaults to `None`. Existing API clients may omit it. Existing uploads and smoke tests continue unchanged.

## Testing

### Backend

- Unit tests for canonical parsing, invalid syntax, ordering, overlap, duplicates, and the 300-page boundary.
- Upload API tests for optional serialization, PDF-only enforcement, normalization, persistence, and unknown-field safety.
- Content Understanding client tests for exact request JSON with and without the REST `range` property while the app contract remains `contentRange`.
- Ingestion tests proving the persisted range reaches `start_analysis()` and redelivery preserves it.
- Model and repository compatibility tests for old records without the field.

### Frontend

- Uploader tests for PDF-only controls and all three modes.
- Validation tests for simple and advanced ranges, including exactly 300 and 301 pages.
- API client tests proving normalized `contentRange` is included in upload initialization.
- Inspector tests for requested-page display.
- Existing accessibility and application tests remain green.

### Release

- CI: frontend tests/build, backend Ruff/mypy/pytest, and Bicep validation.
- CodeQL for Python and JavaScript/TypeScript.
- GitHub-hosted image build and ACR push using the immutable merge SHA.
- Container Apps deployment and existing end-to-end smoke test.
- A production range smoke using a generated multi-page PDF and a finite range, verifying the document reaches `ready` and reports only the requested page count before deletion.

## Acceptance Criteria

1. A user can choose All, Start/End, or Advanced processing for a PDF.
2. Invalid or over-300 selections are rejected before upload.
3. Non-PDF behavior is unchanged and cannot submit a page range.
4. The canonical selection is persisted, returned, displayed, and reused for retries.
5. Content Understanding receives the exact canonical value through the REST `range` property while the public app contract remains `contentRange`.
6. Existing records and clients remain compatible.
7. CI, CodeQL, deployment, and production range smoke pass without secrets, local builds, or ACR Tasks.
