# Execution Progress Details

## 2026-09-03 — Task 1 final dependency-policy quality remediation

- Scope: tightened `backend/tests/test_dependency_policy.py` without changing dependency manifests or performing network/package operations.
- Root cause: host allowlisting was independent of URL scheme, so approved hosts passed over HTTP, FTP, and scheme-relative URLs.
- TDD red: focused policy suite produced the expected three failures for HTTP, FTP, and scheme-relative mutations (`3 failed, 8 passed`).
- Implementation: host-bearing and HTTP(S)-shaped dependency URLs now require the exact `https` scheme before the existing exact hostname allowlist is applied; hostless local file URLs remain allowed and hosted file URLs remain rejected.
- TDD green and final verification: focused policy tests `11 passed`; Ruff passed; mypy reported no issues in 4 source files; full backend tests `12 passed`; `git diff --check` passed.
- Decomposition: atomic. No scenario skill root, Execution stage, or Breakdown Hints files were supplied.

## 2026-09-03 — Task 2 model-validation guard remediation

- Scope: completed negative validation coverage for every applicable persistence, queue,
	evidence, nested document, and API DTO boundary in `backend/tests/test_models.py`.
- TDD red: the focused model suite reported `3 failed, 70 passed`; all failures showed that
	`VersionedDocument` accepted unvalidated `DocumentRecord` instances with invalid session keys,
	UUIDs, or naive timestamps.
- Implementation: enabled shared Pydantic nested-instance revalidation on `ContractModel`,
	preserving frozen models, camel-case serialization, and the existing annotated validators.
- TDD green and final verification: focused model tests passed; Ruff passed; mypy reported no
	issues in 11 source files; the full backend suite passed with 105 tests; `git diff --check`
	passed; editor diagnostics reported no errors in the modified Python files.
- Decomposition: atomic. The change is one shared contract rule plus parameterized boundary
	coverage. No scenario skill root, Execution stage, or Breakdown Hints files were supplied.

## 2026-09-03 — Task 2 UTC-offset contract remediation

- Scope: added an aware `+07:00` mutation for all 17 timestamp fields across persistence,
	queue, evidence, and API boundary models in `backend/tests/test_models.py`.
- TDD red check: no RED occurred; all 17 new mutations were rejected by the existing shared
	`UtcDateTime` validator, and the focused model suite passed with 92 tests. No production
	correction was needed.
- Final verification: Ruff passed; mypy reported no issues in 11 source files; the full backend
	suite passed with 122 tests; editor diagnostics reported no errors; `git diff --check` passed.
- Decomposition: atomic. This was one parameterized contract-test addition. No scenario skill
	root, Execution stage, or Breakdown Hints files were supplied.

## 2026-09-03 — Task 2 configuration and readiness quality remediation

- Scope: hardened readiness task ownership and all Task 2 Azure endpoint, quota, lifetime,
	cookie-duration, and embedding-dimension settings without package or network access.
- Root cause: `ReadinessRegistry.check()` cleaned up only its normal timeout path, so caller
	cancellation bypassed probe cancellation; numeric settings had no bounds; Azure endpoints were
	unvalidated strings; and the timeout regression depended on wall-clock scheduling.
- TDD red: the new cancellation regression reproduced a lingering `readiness:*` task and the
	configuration mutations were accepted; a separate maximum-bound red run produced the expected
	`6 failed, 34 passed`.
- Implementation: all spawned probes now enter `try/finally` cleanup, unfinished probes are
	cancelled without extending the shared timeout, and callbacks consume every eventual outcome.
	A reusable root-only HTTPS endpoint type validates and normalizes Search and Foundry endpoints.
	Positive lower bounds and specification-derived maxima now constrain all numeric settings.
- TDD green and final verification: focused configuration/readiness tests `53 passed`; Ruff
	passed; strict mypy reported no issues in 11 source files; full backend suite `159 passed`;
	`git diff --check` passed; editor diagnostics reported no errors in modified Python files.

## 2026-09-03 — Task 2 final endpoint-authority quality remediation

- Scope: completed strict Search and Foundry HTTPS authority validation and increased only the
	event-synchronized readiness test wait ceiling from 0.1 seconds to 1.0 second.
- Root cause: `urlsplit()` exposes malformed host text through `hostname` without validating DNS
	label syntax or IDNA, strips some raw controls before parsing, and accepts an empty or zero port;
	the validator trusted those parsed fields as a valid authority.
- TDD red: focused configuration/readiness tests exposed acceptance of the reported percent,
	dot-only, and backslash-confused authorities, plus malformed IDNA, empty and overlong labels,
	leading/trailing hyphens, unsafe whitespace, and zero/empty ports. Out-of-range ports were already
	rejected by `urlsplit()` and remain covered as a regression.
- Implementation: raw endpoint syntax is screened before parsing; the exact lowercase HTTPS
	scheme, credential-free root URL, query/fragment absence, and port range are enforced; IP
	literals use `ipaddress`; DNS names are IDNA-normalized and checked against label and total-length
	limits. The validator remains a Pydantic `BeforeValidator` returning `str` and removes a trailing
	root slash.
- TDD green and final verification: focused configuration/readiness tests `91 passed`; Ruff
	passed; strict mypy reported no issues in 17 source files; full backend suite `197 passed`;
	`git diff --check` passed; editor diagnostics reported no errors in modified Python files.

## 2026-09-03 — Task 2 ambiguous endpoint-authority remediation

- Scope: closed the remaining raw Unicode format-control and numeric dotted-authority bypasses
	without changing valid HTTPS endpoint normalization or using the network.
- Root cause: the raw-input screen checked whitespace, backslashes, and only C0 controls, allowing
	IDNA to erase `Cf` characters; failed `ipaddress` parses unconditionally fell through to DNS
	normalization, allowing malformed IPv4-looking authorities as DNS names.
- TDD red: the focused configuration suite produced the expected `18 failed, 85 passed` for
	U+200B, U+200D, and U+FEFF mutations at URL boundaries and inside hosts/paths, plus malformed
	numeric dotted authorities.
- Implementation: reject every raw character in Unicode category `Cc` or `Cf` before scheme
	parsing, and reject ASCII digit/dot-only hostnames when strict IP parsing fails. Alphanumeric and
	hyphenated DNS labels remain accepted and normalized by the existing DNS path.
- TDD green and final verification: focused configuration tests `103 passed`; Ruff passed; strict
	mypy reported no issues in 11 source files; full backend suite `222 passed`; `git diff --check`
	passed; editor diagnostics reported no errors in the modified Python files.

## 2026-09-03 — Task 3 anonymous sessions and quotas

- Scope: added the process-local ETag session repository, anonymous token issue/resolve service,
	document and rolling-question quota operations, session endpoint, and application-factory
	dependency injection. Upload and Azure Table implementations remain untouched.
- TDD red: the initial focused collection failed with the expected two missing-feature errors
	(`ConcurrencyConflict` import and `app.repositories` module). The first green attempt then exposed
	four fixture/configuration gaps while 23 tests passed. A dedicated production-cookie regression
	subsequently failed because production mode inherited the local insecure default.
- Implementation: raw cookies are canonical unpadded URL-safe encodings of exactly 32 random bytes;
	only lowercase SHA-256 keys are persisted. Missing, malformed, short, unknown, and expired cookies
	rotate. Frozen records, opaque ETags, five-attempt optimistic retries, strict over-release errors,
	and a rolling window that excludes timestamps exactly one hour old enforce quotas.
- API: `GET /api/session` uses app-state dependency injection, returns only the existing explicit
	camel-case quota DTO, sets cookies only on issue/rotation, permits local insecure cookies, and
	forces Secure in production while retaining HttpOnly, SameSite=strict, Path=/, and Max-Age=86400.
- TDD green: focused Task 3 tests passed with 27 tests before the production regression; the complete
	suite passed with 250 tests after the Secure production fix.
- Final verification: `uv sync --locked --offline` resolved 89 and checked 88 cached packages; Ruff
	passed; strict mypy reported no issues in 16 source files; full pytest passed `250 passed`; editor
	diagnostics reported no errors in the five checked implementation/test files; `git diff --check`
	passed.
- Decomposition: atomic. No scenario skill root, Execution stage, or Breakdown Hints files were
	supplied; the repository, service, and route form one session-isolation boundary.

## 2026-09-03 — Task 3 session security invariant remediation

- Scope: removed session lifetime and cookie contract values from runtime configuration while
	preserving quota settings, token rotation, optimistic concurrency, and dependency injection.
- Root cause: session expiry and cookie construction read caller-controlled `Settings` fields, so
	accepted lower values could shorten sessions and change the cookie name, age, flags, or path.
- TDD red: focused configuration, service, and API tests produced the expected `8 failed, 125
	passed`; a separate Secure override case also failed before implementation.
- Implementation: module constants now fix lifetime at 24 hours and the cookie contract at
	`cu_session`, Max-Age 86400, HttpOnly, SameSite strict, and Path `/`. Secure is derived only from
	production mode; local and test remain insecure for local HTTP development.
- TDD green and final verification: focused tests `134 passed`; Ruff passed; strict mypy reported
	no issues in 26 source files; full backend pytest passed `253 passed`; `git diff --check` passed;
	editor diagnostics reported no errors in all six modified Python files.
- Decision: no user decision is required; security invariants are intentionally absent from
	`Settings`, and obsolete overrides are ignored by the existing extra-field policy.
