# Task 4 progress details

## 2026-09-03 — code-quality hardening

- Decomposition: atomic direct-upload hardening. No scenario skill root, Execution stage, or
  Breakdown Hints were forwarded; the plan's Task 4 research and repository scope were evaluated.
- Design: SAS is generated before state mutation. `MemoryApplicationRepository` owns shared backing
  state and exposes distinct session/document adapters plus one locked `reserve_and_create` transaction.
  `SessionService` performs bounded conflict retries and quota checks before that atomic commit.
- Office validation: conditional downloads use async chunks under a configurable semaphore, a 4 MiB
  `SpooledTemporaryFile`, exact compressed-byte caps, existing ZIP safety checks, and immutable entry
  metadata. Spools close through context management on success, error, and cancellation.
- Lifecycle: upload/blob boundaries now expose `aclose`; lazily created Azure resources are owned and
  closed independently once, injected resources remain caller-owned, and lifespan cancellation of the
  dispatcher precedes upload resource closure.
- Safety: pending outbox failures emit only event name, outbox ID, kind, and exception class;
  opportunistic failures emit only event name and exception class. Completion ETags require a quoted or
  weak-quoted opaque value, at most 256 characters, with no whitespace or control characters.
- RED evidence: initial new suite failed collection for missing `MemoryApplicationRepository`; focused
  review regressions then failed 3/3 for pre-semaphore download, close cleanup, and missing dispatch log;
  Unicode NBSP ETag rejection failed 1/8 before validator strengthening.
- GREEN evidence before final mandated verification: focused Task 4 suite 162 passed; full backend suite
  373 passed; focused review tests 3 passed; ETag boundary tests 10 passed; Ruff and mypy passed.
- Files changed: backend application protocols/models/repository/services/factory and Task 4 service/API/
  model tests, plus Task 4 plan research. No plan checkbox was changed.
- Final review caught partial factory injection splitting the transaction boundary. The in-memory session
  adapter now exposes a document adapter over the same backing state and implements the atomic operation;
  unsupported custom partial graphs fail at startup. The added behavior test failed before this correction.
- Final mandated verification: offline locked sync resolved 89/checked 88 packages; Ruff passed; mypy
  reported no issues in 21 source files; pytest passed 377 tests in 10.43s; `git diff --check` passed.