#!/usr/bin/env bash
# MVP deployment automation for the Content Understanding RAG demo (Bash, mirror of deploy.ps1).
#
# Provisions the resource-group-scoped Bicep with azd, builds both container images with ACR Tasks
# (no local Docker), rolls the immutable digests onto the API, worker, cleanup job, and frontend,
# points the frontend proxy at the API FQDN, runs the idempotent data-plane bootstrap, and finishes
# with the deployed smoke test. Microsoft Entra auth only; no keys or secrets are written or printed.
# This is a simple single-revision rollout: no candidate labels and no partial traffic shifting.
#
# Run 'az login' first. Use --what-if to print the plan without contacting Azure.
set -euo pipefail

environment_name="cudemo"
subscription=""
resource_group=""
location="southeastasia"
foundry_location="eastus2"
release_sha=""
sample_file=""
skip_bootstrap="false"
skip_smoke="false"
skip_live_model="false"
what_if="false"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
backend_context="$repo_root/backend"
frontend_context="$repo_root/frontend"

usage() {
  cat <<'USAGE'
Usage: deploy.sh [options]
  --environment-name NAME   azd environment name (default: cudemo)
  --subscription ID         Azure subscription id (default: current az context)
  --resource-group NAME     Resource group name (default: azd default rg-<env>)
  --location LOC            App/data location (default: southeastasia)
  --foundry-location LOC    Foundry location (default: eastus2)
  --release-sha SHA         Release identifier (default: short git sha or 'local')
  --sample-file PATH        Optional document for the smoke upload
  --skip-bootstrap          Skip the data-plane bootstrap
  --skip-smoke              Skip the deployed smoke test
  --skip-live-model         Run a preliminary smoke test (no readiness/RAG)
  --what-if                 Print the plan and exit without contacting Azure
  -h, --help                Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment-name) environment_name="$2"; shift 2 ;;
    --subscription) subscription="$2"; shift 2 ;;
    --resource-group) resource_group="$2"; shift 2 ;;
    --location) location="$2"; shift 2 ;;
    --foundry-location) foundry_location="$2"; shift 2 ;;
    --release-sha) release_sha="$2"; shift 2 ;;
    --sample-file) sample_file="$2"; shift 2 ;;
    --skip-bootstrap) skip_bootstrap="true"; shift ;;
    --skip-smoke) skip_smoke="true"; shift ;;
    --skip-live-model) skip_live_model="true"; shift ;;
    --what-if) what_if="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option '$1'" >&2; usage; exit 2 ;;
  esac
done

assert_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required tool '$1' not found on PATH. $2" >&2
    exit 1
  fi
}

resolve_release_sha() {
  if [[ -n "$release_sha" ]]; then
    printf '%s' "$release_sha"; return
  fi
  if command -v git >/dev/null 2>&1 && git -C "$repo_root" rev-parse --short=12 HEAD >/dev/null 2>&1; then
    git -C "$repo_root" rev-parse --short=12 HEAD
  else
    printf '%s' "local"
  fi
}

get_env() {
  printf '%s\n' "$env_values" | awk -F= -v k="$1" '$1==k { sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit }'
}

require_env() {
  local value
  value="$(get_env "$1")"
  if [[ -z "$value" ]]; then
    echo "error: expected Bicep output '$1' missing from 'azd env get-values'." >&2
    exit 1
  fi
  printf '%s' "$value"
}

release_sha="$(resolve_release_sha)"

if [[ "$what_if" == "true" ]]; then
  echo "WhatIf: MVP deployment plan"
  echo "  environment      : $environment_name"
  echo "  subscription     : ${subscription:-(current az context)}"
  echo "  resource group   : ${resource_group:-rg-$environment_name (azd default)}"
  echo "  app location     : $location"
  echo "  foundry location : $foundry_location"
  echo "  release sha      : $release_sha"
  echo "  phases:"
  echo "    1. preflight: az, azd, uv, git; az login check"
  echo "    2. azd env set then azd provision"
  echo "    3. az acr build backend and frontend; resolve immutable digests"
  echo "    4. az containerapp update API/worker/frontend + job update cleanup"
  echo "    5. set frontend API_UPSTREAM to https://<API_FQDN>"
  echo "    6. data-plane bootstrap$([[ "$skip_bootstrap" == "true" ]] && echo ' (skipped)')"
  echo "    7. smoke test$([[ "$skip_smoke" == "true" ]] && echo ' (skipped)')$([[ "$skip_live_model" == "true" ]] && echo ' (--skip-live-model)')"
  exit 0
fi

cd "$repo_root"

# --- Phase 1: preflight ------------------------------------------------------
assert_command az "Install the Azure CLI."
assert_command azd "Install the Azure Developer CLI."
assert_command uv "Install uv (https://docs.astral.sh/uv/)."
if ! az account show --only-show-errors >/dev/null 2>&1; then
  echo "error: not signed in to Azure CLI. Run 'az login' and retry." >&2
  exit 1
fi
if [[ -n "$subscription" ]]; then
  echo "==> Select subscription $subscription"
  az account set --subscription "$subscription"
fi
principal_id="$(az ad signed-in-user show --query id -o tsv)"
if [[ -z "$principal_id" ]]; then
  echo "error: could not resolve the signed-in principal object id." >&2
  exit 1
fi

# --- Phase 2: azd environment + provision -----------------------------------
if ! azd env list --output json 2>/dev/null | grep -q "\"$environment_name\""; then
  echo "==> Create azd environment $environment_name"
  azd env new "$environment_name" --no-prompt
fi
echo "==> Select azd environment $environment_name"
azd env select "$environment_name"

azd env set AZURE_ENV_NAME "$environment_name" >/dev/null
azd env set AZURE_LOCATION "$location" >/dev/null
azd env set AZURE_FOUNDRY_LOCATION "$foundry_location" >/dev/null
azd env set AZURE_PRINCIPAL_ID "$principal_id" >/dev/null
azd env set AZURE_RELEASE_SHA "$release_sha" >/dev/null
[[ -n "$subscription" ]] && azd env set AZURE_SUBSCRIPTION_ID "$subscription" >/dev/null
[[ -n "$resource_group" ]] && azd env set AZURE_RESOURCE_GROUP "$resource_group" >/dev/null

echo "==> azd provision"
azd provision --no-prompt

env_values="$(azd env get-values)"
rg="$(require_env AZURE_RESOURCE_GROUP)"
acr_name="$(require_env AZURE_CONTAINER_REGISTRY_NAME)"
login_server="$(require_env AZURE_CONTAINER_REGISTRY_ENDPOINT)"
api_app="$(require_env API_CONTAINER_APP_NAME)"
worker_app="$(require_env WORKER_CONTAINER_APP_NAME)"
frontend_app="$(require_env FRONTEND_CONTAINER_APP_NAME)"
cleanup_job="$(require_env CLEANUP_JOB_NAME)"
api_fqdn="$(require_env API_FQDN)"
api_url="$(require_env API_URL)"
frontend_url="$(require_env FRONTEND_URL)"
api_upstream="https://$api_fqdn"

# --- Phase 3: build images with ACR Tasks (no local Docker) ------------------
echo "==> az acr build backend:$release_sha"
az acr build --registry "$acr_name" --image "backend:$release_sha" --file Dockerfile "$backend_context"
echo "==> az acr build frontend:$release_sha"
az acr build --registry "$acr_name" --image "frontend:$release_sha" --file Dockerfile "$frontend_context"

backend_digest="$(az acr repository show --name "$acr_name" --image "backend:$release_sha" --query digest -o tsv)"
frontend_digest="$(az acr repository show --name "$acr_name" --image "frontend:$release_sha" --query digest -o tsv)"
if [[ -z "$backend_digest" || -z "$frontend_digest" ]]; then
  echo "error: could not resolve immutable image digests." >&2
  exit 1
fi
backend_ref="$login_server/backend@$backend_digest"
frontend_ref="$login_server/frontend@$frontend_digest"

# Persist digests so a later 'azd provision' preserves them instead of resetting to bootstrap images.
azd env set AZURE_BACKEND_IMAGE "$backend_ref" >/dev/null
azd env set AZURE_FRONTEND_IMAGE "$frontend_ref" >/dev/null

# --- Phase 4/5: roll immutable digests; point frontend at the API FQDN -------
echo "==> Update API container app"
az containerapp update --name "$api_app" --resource-group "$rg" --image "$backend_ref" --set-env-vars "RELEASE_SHA=$release_sha"
echo "==> Update worker container app"
az containerapp update --name "$worker_app" --resource-group "$rg" --image "$backend_ref" --set-env-vars "RELEASE_SHA=$release_sha"
echo "==> Update cleanup job"
az containerapp job update --name "$cleanup_job" --resource-group "$rg" --image "$backend_ref" --set-env-vars "RELEASE_SHA=$release_sha"
echo "==> Update frontend container app"
az containerapp update --name "$frontend_app" --resource-group "$rg" --image "$frontend_ref" --set-env-vars "API_UPSTREAM=$api_upstream" "RELEASE_SHA=$release_sha"

# --- Phase 6: idempotent data-plane bootstrap (keyless, token-only) ----------
if [[ "$skip_bootstrap" != "true" ]]; then
  echo "==> Data-plane bootstrap"
  FOUNDRY_ENDPOINT="$(require_env FOUNDRY_ENDPOINT)" \
  SEARCH_ENDPOINT="$(require_env SEARCH_ENDPOINT)" \
  SEARCH_INDEX_NAME="$(require_env SEARCH_INDEX_NAME)" \
  CHAT_DEPLOYMENT="$(require_env CHAT_DEPLOYMENT)" \
  EMBEDDING_DEPLOYMENT="$(require_env EMBEDDING_DEPLOYMENT)" \
  ANALYZER_ROUTER_ID="business_document_router" \
    uv --project backend run python scripts/bootstrap-data-plane.py
fi

# --- Phase 7: deployed smoke test -------------------------------------------
if [[ "$skip_smoke" != "true" ]]; then
  echo "==> Smoke test"
  smoke_args=(--api-base "$api_url" --frontend-origin "$frontend_url")
  [[ "$skip_live_model" == "true" ]] && smoke_args+=(--skip-live-model)
  [[ -n "$sample_file" ]] && smoke_args+=(--file "$sample_file")
  uv --project backend run python scripts/smoke_test.py "${smoke_args[@]}"
fi

echo ""
echo "Deployment complete."
echo "  Frontend : $frontend_url"
echo "  API      : $api_url"
echo "  Release  : $release_sha"
