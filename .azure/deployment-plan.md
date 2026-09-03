# Azure Deployment Plan

**Status:** Validated

## Goal
Deploy the functional Content Understanding RAG workshop MVP.

## Scope
- Frontend React container on Azure Container Apps
- FastAPI API and queue worker on Azure Container Apps
- Azure Storage for uploads, state, and queues
- Azure AI Search for vector/hybrid retrieval
- Microsoft Foundry in East US 2 with `gpt-5` and `text-embedding-3-large`
- Application resources in Southeast Asia
- Managed identities and keyless runtime access
- Azure Container Registry
- Application Insights / Log Analytics

## Deployment method
- Azure Developer CLI (`azd`)
- Bicep only
- Docker images for frontend and shared backend

## Functional gate
Upload a fixture, extract with Content Understanding, index it, ask a grounded question through GPT-5, verify a citation, then delete the fixture.

## Security
Managed Identity for runtime services, GitHub OIDC for CI/CD, no Azure API keys or client secrets in source control.

## GitHub quality gates
- Pull requests run tests and CodeQL for Python and JavaScript/TypeScript; CodeQL is a required blocking check.
- A branch ruleset automatically requests GitHub Copilot code review for new pull requests and new pushes.
- Copilot review is advisory because GitHub records it as a comment review; tests and CodeQL remain the merge blockers.
- Pushes to `main` build the frontend and backend images, push them to ACR, and deploy the Azure Container Apps revision through OIDC.

## Implemented infrastructure (Task 15 — In Progress)

Authored a simplified, functional MVP. Bicep only. Root template `infra/main.bicep` is
`targetScope = 'resourceGroup'`; `azure.yaml` wires the `azd` Bicep provider and the
`frontend`/`api` services (worker and cleanup job share the backend image and are managed by
the deployment scripts in Task 16).

**Files**
- `azure.yaml` — `azd` project, Bicep provider, `frontend` + `api` services.
- `infra/main.bicep` — full composition (AVM modules + raw resources).
- `infra/main.bicepparam` — non-secret defaults via `readEnvironmentVariable`.
- `infra/tests/main.test.bicepparam` — fixed values that exercise the GitHub + bootstrap conditionals.
- `infra/tests/test_infra_policy.py` — 27 policy assertions over the compiled ARM JSON.

**AVM vs raw (per "AVM where straightforward, raw where complex")** — AVM modules (pinned):
managed identity `0.6.0`, Log Analytics `0.16.1`, Application Insights `0.8.0`, ACR `0.13.0`,
AI Search `0.13.0`, Container Apps environment `0.15.0`. Raw resources: Storage account +
blob/queue/table children + CORS + lifecycle, Foundry account + two model deployments,
Container Apps (frontend/api/worker) + cleanup Job, and all role assignments.

**Resource inventory**
- Region — application/data in `southeastasia`; Foundry in `eastus2` (one resource group).
- Storage — StorageV2 Standard LRS; containers `uploads`/`derived`/`control`; queues
  `ingestion`/`cu-result-cleanup`/`ingestion-poison`; table `workshop`; browser CORS
  (`PUT,OPTIONS`); 24-hour blob lifecycle.
- AI Search — Basic, semantic `standard`, vector-ready, `disableLocalAuth: true`.
- ACR — Basic, admin disabled; AcrPull granted to the pull identity.
- Foundry — one `AIServices` account, system identity, custom subdomain, `disableLocalAuth: true`;
  `gpt-5` GlobalStandard (default 10K TPM) and `text-embedding-3-large` Standard (default 30K TPM),
  deployments serialized.
- Observability — capped Log Analytics (1 GB/day) + workspace-based Application Insights.
- Compute — Container Apps environment; public frontend (8080, `/healthz` probes); **public** API
  (8000, liveness/readiness/startup probes, ingress CORS locked to the frontend origin); no-ingress
  worker with two managed-identity KEDA queue rules (`ingestion`, `cu-result-cleanup`); hourly
  cleanup Job (`0 * * * *`).

**Identities & RBAC (keyless, least privilege)**
- Shared application UAMI (API/worker/cleanup): Storage Blob Data Contributor, Storage Blob
  Delegator, Storage Queue Data Contributor, Storage Table Data Contributor; Search Index Data
  Contributor + Search Service Contributor; Cognitive Services OpenAI User + Content Understanding
  Owner (`4b42bd01-da42-4c92-9b07-15ea5bd6a602`).
- ACR-pull UAMI: AcrPull. Foundry system identity: OpenAI User on its own account.
- Optional `deploymentPrincipalId`: same data-plane roles for local/CI bootstrap when supplied.
- Optional GitHub deployment UAMI (created when `githubOwner`+`githubRepository` set): OIDC federated
  credential `repo:{owner}/{repository}:environment:production`; Contributor + RBAC Administrator on
  the resource group; Content Understanding Owner on Foundry.

**Images** — exactly two parameters (`frontendImage`, `backendImage`), both defaulting to the public
`containerapps-helloworld` bootstrap image; the one `backendImage` is applied identically to API,
worker, and cleanup with an identical environment block.

**Outputs** — endpoints, resource names, ACR login server, container app FQDNs/URLs, App Insights
connection string, and identity client/resource ids.

**MVP simplifications (operator-directed)** — one shared application UAMI (not separate
API/worker/cleanup); public API for functional simplicity (CORS/origin restricted); no Azure Monitor
alerts module; consolidated into `main.bicep` rather than per-concern modules.

## Validation

### All validation checks pass
- [x] 1. AZD Installation
- [x] 2. Schema Validation
- [x] 3. Environment Setup
- [x] 4. Authentication Check
- [x] 5. Subscription/Location Check
- [x] 6. Aspire Pre-Provisioning Checks (not applicable)
- [x] 7. Provision Preview
- [x] 8. Build Verification
- [x] 9. Docker Build Context Validation
- [x] 10. Package Validation (remote ACR build configured; source contracts validated)
- [x] 11. Azure Policy and RBAC static validation
- [x] 12. Aspire Post-Provisioning Checks (not applicable)

- `az bicep format --file infra/main.bicep` — clean.
- `az bicep build --file infra/main.bicep` — zero errors and zero warnings.
- `az bicep build-params` on both `.bicepparam` files — succeeds.
- `uv --project backend run pytest infra/tests -q` — 27 passed (regions, models, resources, keyless,
  managed identity, KEDA MI, two images).
- Deferred: live `az deployment group validate`, container builds, and Azure preflight (Task 19,
  after subscription selection and resource-group bootstrap).

## Caveats

- `gpt-5` / `text-embedding-3-large` deployments omit an explicit model version so Azure resolves the
  current default at deploy time; capacity and region quota are validated during deployment, not at
  compile time.
- The KEDA queue scale-rule `identity` (managed-identity auth) is valid at deploy time but not yet in
  the Bicep type for `Microsoft.App/containerApps@2025-01-01`; a single `#disable-next-line BCP037`
  keeps the build warning-free (tracked upstream at aka.ms/bicep-type-issues).
- The Content Understanding roles are not yet on the Microsoft Learn built-in roles page; GUIDs were
  verified against Azure role catalogs.

## Section 7: Validation Proof

- `azd version` — 1.27.0 installed.
- `azd auth login --check-status` — authenticated as `bibrani@contoso.day`.
- Azure subscription — `ME-MngEnvMCAP708029-benyibrani-1`; app region Southeast Asia; Foundry East US 2.
- `az bicep build --file infra/main.bicep --stdout` — passed.
- `uv --project backend run pytest infra/tests -q` — 27 passed.
- Backend quality gate — Ruff passed, strict mypy passed, 677 tests passed.
- Frontend quality gate — lint, type-check, 7 tests, and production build passed.
- `azd provision --preview --no-prompt` — passed; preview creates 13 expected resources in `rg-cudemo`.
- Docker is not locally installed; both services use `remoteBuild: true`, and deployment uses ACR Tasks.
- Static RBAC review — passed after removing a duplicate Foundry role assignment path.
