# Deploy and operate the MVP

This how-to deploys the workshop application to Azure. Infrastructure is created once from validated Bicep. Release container images are built **only on GitHub-hosted runners** by the `Deploy` workflow ([.github/workflows/deploy.yml](../.github/workflows/deploy.yml)): a push to `main` builds both images, pushes immutable SHA-tagged images to ACR, updates the Container Apps, then bootstraps the data plane and runs the smoke test. Authentication is Microsoft Entra (OIDC) end to end.

> Release images are never built from a developer machine and never with ACR Tasks. Any local `docker` or `az acr build` images produced while troubleshooting are non-release and safe to ignore; do not delete shared resources to "clean them up".

## Before deployment

1. Install Azure CLI, `azd`, PowerShell 7, Python 3.12, `uv`, Node.js, and npm.
2. Obtain subscription permissions to deploy resource-group resources and assign roles.
3. Confirm access to the Microsoft enterprise Python feed configured in `backend/pyproject.toml`. The deployment must not fall back to public PyPI.
4. Use only synthetic, non-confidential workshop documents.
5. Confirm regional approval: application and data resources are in Southeast Asia; Foundry and AI processing are in East US 2.

Inspect the deployment without contacting Azure:

```powershell
./scripts/deploy.ps1 -WhatIf
```

## Provision infrastructure (run once, locally)

Provision the resource group from the validated Bicep. Passing the GitHub `owner`/`repository` creates the deployment identity and its `production` federated credential used by the release workflow:

```powershell
az login
az account set --subscription <subscription-id>
./scripts/deploy.ps1 -EnvironmentName cudemo -Subscription <subscription-id>
```

Optional switches:

- `-ResourceGroup <name>` selects an existing or azd-managed resource group name.
- `-SkipLiveModel` validates session and upload plumbing without waiting for AI processing.
- `-SkipBootstrap` skips analyzer and Search-index initialization.
- `-SkipSmoke` skips the deployed smoke test.
- `-SampleFile <path>` replaces the default synthetic PDF generated in memory by `scripts/smoke_test.py`. Never use confidential data.

`deploy.ps1` provisions with Bicep and can also run a **local, non-release** end-to-end rollout (it builds with ACR Tasks, so no local Docker is required). Use it for the one-time provision and for troubleshooting only — it is not the release path.

## Release (GitHub only)

Release images are built and shipped by the `Deploy` workflow, never locally:

1. Push the feature branch and open a pull request.
2. GitHub Copilot code review plus the required CI and CodeQL checks run and must pass.
3. Merge to `main`. The push to `main` triggers `Deploy`, which on a GitHub-hosted runner logs in with OIDC, builds the backend and frontend images, pushes immutable SHA-tagged images to ACR, updates the API/worker/cleanup/frontend Container Apps, bootstraps the data plane, and runs the smoke test.

Publish the non-secret production variables the workflow reads once with `./scripts/configure-github.ps1` (it also creates the `production` environment and the branch ruleset). The rollout is a simple MVP revision update, not candidate traffic orchestration.

## Verify

Use values printed by deployment or returned by `azd env get-values`:

```powershell
uv --project backend run python scripts/smoke_test.py `
  --api-base <api-url> `
  --frontend-origin <frontend-url>
```

With no `--file`, the script generates a small synthetic Contoso PDF in memory, uploads it, waits for ingestion, asks a known question, requires a citation, and requests deletion. No binary fixture is committed.

## Run cleanup

The cleanup job normally runs hourly. Start it manually after substituting Bicep outputs:

```powershell
az containerapp job start --name <cleanup-job-name> --resource-group <resource-group>
```

Confirm execution in the Container Apps job execution history and confirm deleting/expired documents are no longer visible. Local Azurite cleanup is `docker compose run --rm api cleanup`.

## Remove the environment

Review the selected environment and subscription, then remove its resource group:

```powershell
azd env select cudemo
azd down --purge --force
```

If `azd down` cannot identify the resource group, retrieve `AZURE_RESOURCE_GROUP` from `azd env get-values`, verify it carefully, and use `az group delete --name <resource-group>`.

## Troubleshooting

### Model or regional quota

- Capacity or quota failures usually mean East US 2 lacks available `gpt-5` Global Standard or `text-embedding-3-large` Standard capacity for the subscription.
- Check Azure AI Foundry quota for East US 2. Request quota or use another authorized subscription; do not change the fixed runtime deployment from `gpt-5`.
- A model `429` is transient capacity pressure. Retry after the reported delay and inspect Application Insights before increasing capacity parameters.

### Authorization and roles

- The local deployer needs resource deployment and role-assignment permission. A `403` during Bicep role assignment generally requires Owner or Contributor plus Role Based Access Control Administrator at resource-group scope.
- Data-plane bootstrap requires Storage Blob Delegator and Data Contributor, Search Service and Index Data roles, Cognitive Services OpenAI User, and Content Understanding Owner.
- Role propagation can take several minutes. Retry after propagation; do not introduce account keys, connection strings, client secrets, or Search/Foundry keys.
- Under GitHub Actions, verify the environment-scoped OIDC subject and non-secret `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_SUBSCRIPTION_ID` environment variables.

### Content Understanding processing

- Check document state and correlation ID in the Technical Console.
- `result_cleanup_pending` is a durable retry state; do not re-upload immediately.
- Inspect worker and cleanup logs without printing document content, prompts, cookies, tokens, or SAS query strings. After correcting quota or role issues, use the document Retry action for a retryable failed document.

### Enterprise package feed

- If `uv sync --locked` cannot resolve packages, verify network and certificate access to `packagefeedproxy.microsoft.io`.
- Do not add public package indexes as a workaround. Configure the approved enterprise CA when required.
