#Requires -Version 7.0
<#
.SYNOPSIS
    MVP deployment automation for the Content Understanding RAG demo (PowerShell, primary).

.DESCRIPTION
    Provisions the resource-group-scoped Bicep with azd, builds the two container images with ACR
    Tasks (no local Docker required), rolls the immutable digests onto the API, worker, cleanup job,
    and frontend, points the frontend proxy at the API FQDN, runs the idempotent data-plane
    bootstrap, and finishes with the deployed smoke test. Authentication is Microsoft Entra only;
    no keys, connection strings, or tokens are written or printed. This is intentionally a simple,
    single-revision rollout - it does not create candidate labels or shift partial traffic.

.NOTES
    Run 'az login' first. Use -WhatIf to print the plan without contacting Azure.
#>

[CmdletBinding()]
param(
    [string]$EnvironmentName = 'cudemo',
    [string]$Subscription = '',
    [string]$ResourceGroup = '',
    [string]$Location = 'southeastasia',
    [string]$FoundryLocation = 'eastus2',
    [string]$ReleaseSha = '',
    [string]$SampleFile = '',
    [switch]$SkipBootstrap,
    [switch]$SkipSmoke,
    [switch]$SkipLiveModel,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendContext = Join-Path $RepoRoot 'backend'
$FrontendContext = Join-Path $RepoRoot 'frontend'

function Assert-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$Name' was not found on PATH. $Hint"
    }
}

function Invoke-Checked {
    param([Parameter(Mandatory)][scriptblock]$Action, [Parameter(Mandatory)][string]$What)
    Write-Host "==> $What" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed (exit $LASTEXITCODE): $What"
    }
}

function Resolve-ReleaseSha {
    param([string]$Requested)
    if ($Requested) { return $Requested }
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $sha = (git -C $RepoRoot rev-parse --short=12 HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $sha) { return $sha.Trim() }
    }
    return 'local'
}

function Get-AzdEnvValues {
    $map = @{}
    foreach ($line in (azd env get-values)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

function Require-Value {
    param([hashtable]$Map, [string]$Key)
    if (-not $Map.ContainsKey($Key) -or [string]::IsNullOrWhiteSpace($Map[$Key])) {
        throw "Expected Bicep output '$Key' was not present in 'azd env get-values'."
    }
    return $Map[$Key]
}

$ReleaseSha = Resolve-ReleaseSha -Requested $ReleaseSha

if ($WhatIf) {
    Write-Host 'WhatIf: MVP deployment plan' -ForegroundColor Yellow
    Write-Host "  environment      : $EnvironmentName"
    Write-Host "  subscription     : $(if ($Subscription) { $Subscription } else { '(current az context)' })"
    Write-Host "  resource group   : $(if ($ResourceGroup) { $ResourceGroup } else { "rg-$EnvironmentName (azd default)" })"
    Write-Host "  app location     : $Location"
    Write-Host "  foundry location : $FoundryLocation"
    Write-Host "  release sha      : $ReleaseSha"
    Write-Host '  phases:'
    Write-Host '    1. preflight: az, azd, uv, git; az login check'
    Write-Host '    2. azd env set (env/location/foundry/principal/release) then azd provision'
    Write-Host '    3. az acr build backend and frontend; resolve immutable digests'
    Write-Host '    4. az containerapp update API/worker/frontend + job update cleanup (immutable digests)'
    Write-Host '    5. set frontend API_UPSTREAM to https://<API_FQDN>'
    Write-Host "    6. data-plane bootstrap$(if ($SkipBootstrap) { ' (skipped)' } else { '' })"
    Write-Host "    7. smoke test$(if ($SkipSmoke) { ' (skipped)' } elseif ($SkipLiveModel) { ' (--skip-live-model)' } else { '' })"
    exit 0
}

Set-Location $RepoRoot

# --- Phase 1: preflight ------------------------------------------------------
Assert-Command -Name 'az' -Hint "Install the Azure CLI."
Assert-Command -Name 'azd' -Hint "Install the Azure Developer CLI."
Assert-Command -Name 'uv' -Hint "Install uv (https://docs.astral.sh/uv/)."
az account show --only-show-errors 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw "Not signed in to Azure CLI. Run 'az login' and retry." }
if ($Subscription) {
    Invoke-Checked -What "Select subscription $Subscription" -Action { az account set --subscription $Subscription }
}
$principalId = (az ad signed-in-user show --query id -o tsv)
if ($LASTEXITCODE -ne 0 -or -not $principalId) {
    throw "Could not resolve the signed-in principal object id for data-plane role assignment."
}

# --- Phase 2: azd environment + provision -----------------------------------
$envNames = @()
$envListJson = (azd env list --output json 2>$null)
if ($LASTEXITCODE -eq 0 -and $envListJson) {
    $envNames = ($envListJson | ConvertFrom-Json | ForEach-Object { $_.Name })
}
if ($envNames -notcontains $EnvironmentName) {
    Invoke-Checked -What "Create azd environment $EnvironmentName" -Action { azd env new $EnvironmentName --no-prompt }
}
Invoke-Checked -What "Select azd environment $EnvironmentName" -Action { azd env select $EnvironmentName }

azd env set AZURE_ENV_NAME $EnvironmentName | Out-Null
azd env set AZURE_LOCATION $Location | Out-Null
azd env set AZURE_FOUNDRY_LOCATION $FoundryLocation | Out-Null
azd env set AZURE_PRINCIPAL_ID $principalId | Out-Null
azd env set AZURE_RELEASE_SHA $ReleaseSha | Out-Null
if ($Subscription) { azd env set AZURE_SUBSCRIPTION_ID $Subscription | Out-Null }
if ($ResourceGroup) { azd env set AZURE_RESOURCE_GROUP $ResourceGroup | Out-Null }

Invoke-Checked -What 'azd provision' -Action { azd provision --no-prompt }

$envValues = Get-AzdEnvValues
$rg = Require-Value -Map $envValues -Key 'AZURE_RESOURCE_GROUP'
$acrName = Require-Value -Map $envValues -Key 'AZURE_CONTAINER_REGISTRY_NAME'
$loginServer = Require-Value -Map $envValues -Key 'AZURE_CONTAINER_REGISTRY_ENDPOINT'
$apiApp = Require-Value -Map $envValues -Key 'API_CONTAINER_APP_NAME'
$workerApp = Require-Value -Map $envValues -Key 'WORKER_CONTAINER_APP_NAME'
$frontendApp = Require-Value -Map $envValues -Key 'FRONTEND_CONTAINER_APP_NAME'
$cleanupJob = Require-Value -Map $envValues -Key 'CLEANUP_JOB_NAME'
$apiFqdn = Require-Value -Map $envValues -Key 'API_FQDN'
$apiUrl = Require-Value -Map $envValues -Key 'API_URL'
$frontendUrl = Require-Value -Map $envValues -Key 'FRONTEND_URL'
$apiUpstream = "https://$apiFqdn"

# --- Phase 3: build images with ACR Tasks (no local Docker) ------------------
Invoke-Checked -What "az acr build backend:$ReleaseSha" -Action {
    az acr build --registry $acrName --image "backend:$ReleaseSha" --file Dockerfile $BackendContext
}
Invoke-Checked -What "az acr build frontend:$ReleaseSha" -Action {
    az acr build --registry $acrName --image "frontend:$ReleaseSha" --file Dockerfile $FrontendContext
}
$backendDigest = (az acr repository show --name $acrName --image "backend:$ReleaseSha" --query digest -o tsv)
if ($LASTEXITCODE -ne 0 -or -not $backendDigest) { throw "Could not resolve the backend image digest." }
$frontendDigest = (az acr repository show --name $acrName --image "frontend:$ReleaseSha" --query digest -o tsv)
if ($LASTEXITCODE -ne 0 -or -not $frontendDigest) { throw "Could not resolve the frontend image digest." }
$backendRef = "$loginServer/backend@$backendDigest"
$frontendRef = "$loginServer/frontend@$frontendDigest"

# Persist immutable digests so a later 'azd provision' preserves them instead of resetting to bootstrap.
azd env set AZURE_BACKEND_IMAGE $backendRef | Out-Null
azd env set AZURE_FRONTEND_IMAGE $frontendRef | Out-Null

# --- Phase 4/5: roll immutable digests; point frontend at the API FQDN -------
Invoke-Checked -What "Update API container app" -Action {
    az containerapp update --name $apiApp --resource-group $rg --image $backendRef --set-env-vars "RELEASE_SHA=$ReleaseSha"
}
Invoke-Checked -What "Update worker container app" -Action {
    az containerapp update --name $workerApp --resource-group $rg --image $backendRef --set-env-vars "RELEASE_SHA=$ReleaseSha"
}
Invoke-Checked -What "Update cleanup job" -Action {
    az containerapp job update --name $cleanupJob --resource-group $rg --image $backendRef --set-env-vars "RELEASE_SHA=$ReleaseSha"
}
Invoke-Checked -What "Update frontend container app" -Action {
    az containerapp update --name $frontendApp --resource-group $rg --image $frontendRef --set-env-vars "API_UPSTREAM=$apiUpstream" "RELEASE_SHA=$ReleaseSha"
}

# --- Phase 6: idempotent data-plane bootstrap (keyless, token-only) ----------
if (-not $SkipBootstrap) {
    $env:FOUNDRY_ENDPOINT = Require-Value -Map $envValues -Key 'FOUNDRY_ENDPOINT'
    $env:SEARCH_ENDPOINT = Require-Value -Map $envValues -Key 'SEARCH_ENDPOINT'
    $env:SEARCH_INDEX_NAME = Require-Value -Map $envValues -Key 'SEARCH_INDEX_NAME'
    $env:CHAT_DEPLOYMENT = Require-Value -Map $envValues -Key 'CHAT_DEPLOYMENT'
    $env:EMBEDDING_DEPLOYMENT = Require-Value -Map $envValues -Key 'EMBEDDING_DEPLOYMENT'
    $env:ANALYZER_ROUTER_ID = 'business-document-router'
    Invoke-Checked -What 'Data-plane bootstrap' -Action {
        uv --project backend run python scripts/bootstrap-data-plane.py
    }
}

# --- Phase 7: deployed smoke test -------------------------------------------
if (-not $SkipSmoke) {
    $smokeArgs = @('--api-base', $apiUrl, '--frontend-origin', $frontendUrl)
    if ($SkipLiveModel) { $smokeArgs += '--skip-live-model' }
    if ($SampleFile) { $smokeArgs += @('--file', $SampleFile) }
    Invoke-Checked -What 'Smoke test' -Action {
        uv --project backend run python scripts/smoke_test.py @smokeArgs
    }
}

Write-Host ''
Write-Host "Deployment complete." -ForegroundColor Green
Write-Host "  Frontend : $frontendUrl"
Write-Host "  API      : $apiUrl"
Write-Host "  Release  : $ReleaseSha"
