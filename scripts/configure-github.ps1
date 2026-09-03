#Requires -Version 7.0
<#
.SYNOPSIS
    Configure GitHub delivery policy for the Content Understanding RAG demo.

.DESCRIPTION
    Uses the GitHub CLI ('gh') to:
      1. Create/ensure the 'production' deployment environment.
      2. Set the non-secret OIDC and azd variables the deploy workflow reads (vars.*). No secrets.
      3. Create/update a branch ruleset on the default branch that requires the CI and CodeQL
         status checks (CodeQL blocking) and enables automatic GitHub Copilot code review.

    This script configures GitHub only. It never creates Azure resources: the deployment identity
    and its environment-scoped federated credential are created by the resource-group-scoped Bicep
    (see scripts/deploy.ps1). No client secret is ever created or stored.

    If the account/plan cannot enable automatic Copilot review, the ruleset is still created without
    it and manual instructions are printed (non-fatal).

.NOTES
    Run 'gh auth login' first. AZURE_CLIENT_ID is the deployment identity's application (client) ID.
#>

[CmdletBinding()]
param(
    [string]$Repo = '',
    [Parameter(Mandatory)][string]$AzureClientId,
    [Parameter(Mandatory)][string]$AzureTenantId,
    [Parameter(Mandatory)][string]$AzureSubscriptionId,
    [string]$EnvironmentName = 'cudemo',
    [string]$Location = 'southeastasia',
    [string]$FoundryLocation = 'eastus2',
    [string]$RulesetName = 'main-protection'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$Name' was not found on PATH. $Hint"
    }
}

function Invoke-Gh {
    # Runs 'gh' and throws on a non-zero exit; returns captured stdout.
    param([Parameter(Mandatory)][string[]]$GhArgs, [string]$What)
    $output = & gh @GhArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "gh $($GhArgs -join ' ') failed$(if ($What) { " ($What)" }): $output"
    }
    return $output
}

Assert-Command -Name 'gh' -Hint "Install the GitHub CLI (https://cli.github.com) and run 'gh auth login'."
Invoke-Gh -GhArgs @('auth', 'status') -What 'GitHub authentication' | Out-Null

if (-not $Repo) {
    $Repo = (Invoke-Gh -GhArgs @('repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner') -What 'resolve repository').Trim()
}
Write-Host "==> Repository: $Repo" -ForegroundColor Cyan

# --- 1. Production environment ----------------------------------------------
Write-Host '==> Ensuring production environment' -ForegroundColor Cyan
Invoke-Gh -GhArgs @('api', '-X', 'PUT', "repos/$Repo/environments/production", '--silent') -What 'create environment' | Out-Null

# --- 2. Non-secret environment variables ------------------------------------
function Set-EnvVariable {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        Write-Warning "Skipping variable '$Name' because no value was supplied."
        return
    }
    & gh api "repos/$Repo/environments/production/variables/$Name" --silent 2>$null
    if ($LASTEXITCODE -eq 0) {
        Invoke-Gh -GhArgs @('api', '-X', 'PATCH', "repos/$Repo/environments/production/variables/$Name",
            '-f', "name=$Name", '-f', "value=$Value", '--silent') -What "update variable $Name" | Out-Null
    }
    else {
        Invoke-Gh -GhArgs @('api', '-X', 'POST', "repos/$Repo/environments/production/variables",
            '-f', "name=$Name", '-f', "value=$Value", '--silent') -What "create variable $Name" | Out-Null
    }
    Write-Host "    set $Name"
}

Write-Host '==> Setting production variables (no secrets)' -ForegroundColor Cyan
Set-EnvVariable -Name 'AZURE_CLIENT_ID'       -Value $AzureClientId
Set-EnvVariable -Name 'AZURE_TENANT_ID'       -Value $AzureTenantId
Set-EnvVariable -Name 'AZURE_SUBSCRIPTION_ID' -Value $AzureSubscriptionId
Set-EnvVariable -Name 'AZURE_ENV_NAME'        -Value $EnvironmentName
Set-EnvVariable -Name 'AZURE_LOCATION'        -Value $Location
Set-EnvVariable -Name 'AZURE_FOUNDRY_LOCATION' -Value $FoundryLocation

# --- 3. Branch ruleset: required checks + automatic Copilot review ----------
$requiredChecks = @('backend', 'frontend', 'bicep', 'Analyze (python)', 'Analyze (javascript-typescript)')

function New-RulesetBody {
    param([bool]$WithCopilot)
    $pullRequest = [ordered]@{
        required_approving_review_count   = 0
        dismiss_stale_reviews_on_push     = $true
        require_code_owner_review         = $false
        require_last_push_approval        = $false
        required_review_thread_resolution = $false
    }
    if ($WithCopilot) { $pullRequest['automatic_copilot_code_review_enabled'] = $true }

    $body = [ordered]@{
        name        = $RulesetName
        target      = 'branch'
        enforcement = 'active'
        conditions  = [ordered]@{ ref_name = [ordered]@{ include = @('~DEFAULT_BRANCH'); exclude = @() } }
        rules       = @(
            [ordered]@{ type = 'pull_request'; parameters = $pullRequest }
            [ordered]@{ type = 'required_status_checks'; parameters = [ordered]@{
                    strict_required_status_checks_policy = $true
                    required_status_checks               = @($requiredChecks | ForEach-Object { [ordered]@{ context = $_ } })
                }
            }
            [ordered]@{ type = 'non_fast_forward' }
        )
    }
    return $body | ConvertTo-Json -Depth 12
}

function Set-Ruleset {
    param([string]$BodyJson)
    $existing = Invoke-Gh -GhArgs @('api', "repos/$Repo/rulesets", '--paginate') -What 'list rulesets' | ConvertFrom-Json
    $match = @($existing) | Where-Object { $_.name -eq $RulesetName } | Select-Object -First 1
    $tmp = New-TemporaryFile
    try {
        $BodyJson | Set-Content -Path $tmp -Encoding utf8
        if ($match) {
            Invoke-Gh -GhArgs @('api', '-X', 'PUT', "repos/$Repo/rulesets/$($match.id)", '--input', $tmp, '--silent') -What 'update ruleset' | Out-Null
        }
        else {
            Invoke-Gh -GhArgs @('api', '-X', 'POST', "repos/$Repo/rulesets", '--input', $tmp, '--silent') -What 'create ruleset' | Out-Null
        }
    }
    finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "==> Applying branch ruleset '$RulesetName' (required checks + Copilot review)" -ForegroundColor Cyan
try {
    Set-Ruleset -BodyJson (New-RulesetBody -WithCopilot $true)
    Write-Host '    ruleset applied with automatic Copilot code review enabled.' -ForegroundColor Green
}
catch {
    Write-Warning "Could not apply the ruleset with automatic Copilot review: $($_.Exception.Message)"
    Write-Warning 'Retrying without the Copilot parameter (automatic Copilot review may be unavailable on this plan/GHES).'
    Set-Ruleset -BodyJson (New-RulesetBody -WithCopilot $false)
    Write-Warning 'Ruleset applied WITHOUT automatic Copilot review. To enable it manually (non-fatal):'
    Write-Warning "  Settings > Rules > Rulesets > '$RulesetName' > Require a pull request before merging > 'Request pull request review from Copilot'."
    Write-Warning '  Requires GitHub Copilot Enterprise or Copilot Pro+ with code review enabled for the organization/repository.'
}

Write-Host ''
Write-Host 'GitHub configuration complete.' -ForegroundColor Green
Write-Host "  Environment    : production"
Write-Host "  Required checks: $($requiredChecks -join ', ')"
Write-Host "  Ruleset        : $RulesetName (default branch)"
