# Content Understanding Meets GitHub Copilot

An English Technical Console that uploads business documents, extracts structured content with
Microsoft Foundry Content Understanding, indexes grounded evidence in Azure AI Search, and streams
cited answers from `gpt-5`.

> **Workshop data only.** Do not upload confidential, regulated, personal, customer, or production
> information. Use synthetic data such as the PDF generated in memory by the smoke test.

## Architecture and regions

- Frontend, API, worker, cleanup job, Storage, Azure AI Search, and ACR: **Southeast Asia**.
- Microsoft Foundry, Content Understanding, `gpt-5`, and `text-embedding-3-large`: **East US 2**.
- Documents and derived evidence therefore cross regions for AI processing.
- Runtime authentication is keyless through Microsoft Entra ID and managed identities.

See [the workshop](docs/workshop/README.md), [deployment guide](docs/deployment.md), and
[security model](docs/security.md).

## Prerequisites

- Python 3.12 and `uv`
- Node.js 20+ and npm
- Azure CLI, Azure Developer CLI (`azd`), and PowerShell 7
- An Azure subscription with permission to deploy resources and assign roles
- Access to the Microsoft enterprise Python feed configured in `backend/pyproject.toml`
- Optional: Docker for the local container build deferred to the live-delivery gate

The backend resolves Python packages only through
`https://packagefeedproxy.microsoft.io/pypi/simple/`; do not replace it with public PyPI.

## Set up and verify locally

```powershell
Set-Location backend
uv sync --locked
uv run ruff check .
uv run mypy app
uv run pytest -q
Set-Location ../frontend
npm ci
npm run lint
npm run typecheck
npm test -- --run
npm run build
Set-Location ..
az bicep build --file infra/main.bicep
uv --project backend run pytest scripts/tests infra/tests -q
```

The local Compose stack provides the frontend, API, worker, and Azurite. Content Understanding,
embeddings, and GPT-5 have no local emulator; use the deployed smoke test for the full path.

```powershell
Copy-Item .env.example .env
docker compose build
docker compose up -d
docker compose down
```

## Deploy and remove

Sign in, select the intended subscription, and run the canonical PowerShell deployment:

```powershell
az login
./scripts/deploy.ps1 -EnvironmentName cudemo -Subscription <subscription-id>
```

The script provisions Bicep, builds two images with ACR Tasks, deploys immutable digests, bootstraps
the data plane, and runs the generated-PDF smoke test. Full commands, cleanup, and troubleshooting
are in [the deployment guide](docs/deployment.md).

## GitHub CodeQL and Copilot review

The repository includes CI, CodeQL for Python and JavaScript/TypeScript, an OIDC deployment workflow,
Copilot review instructions, and a ruleset setup script. After Bicep provisions the GitHub deployment
identity, configure the `production` environment and default-branch rules:

```powershell
./scripts/configure-github.ps1 -Repo <owner/repository> `
	-AzureClientId <deployment-identity-client-id> `
	-AzureTenantId <tenant-id> `
	-AzureSubscriptionId <subscription-id>
```

Pull requests run CI and CodeQL. The ruleset requires their checks and requests Copilot review when
the GitHub account supports it. Pushes to `main` deploy through GitHub OIDC; no Azure client secret
is stored. Live proof and branch protection activation remain part of Task 19.

## Runtime operations

Start retention cleanup on demand after substituting Bicep outputs:

```powershell
az containerapp job start --name <cleanup-job-name> --resource-group <resource-group>
```

For local storage cleanup, run `docker compose run --rm api cleanup`.
