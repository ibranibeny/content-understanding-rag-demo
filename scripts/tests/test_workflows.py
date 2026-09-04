"""Policy tests for the GitHub Actions workflows and delivery configuration.

Parsing is intentionally stdlib-only (regex over the workflow text) so the checks run under a clean
`uv sync` of the existing enterprise backend config without adding a YAML dependency. The assertions
encode the security-relevant delivery policy: least-privilege permissions, PR-triggered CI/CodeQL,
main-only OIDC deployment with no secrets, the required-check contexts, and Copilot review setup.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _read(name: str) -> str:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow: {path}"
    return path.read_text(encoding="utf-8")


def _has_key(text: str, key: str) -> bool:
    return re.search(rf"(?m)^\s*{re.escape(key)}\s*:", text) is not None


# --- Well-formedness sanity: no tabs (YAML forbids them for indentation) ------
@pytest.mark.parametrize("name", ["ci.yml", "codeql.yml", "deploy.yml"])
def test_workflow_uses_spaces_not_tabs(name: str) -> None:
    assert "\t" not in _read(name), f"{name} must not contain tab characters"


@pytest.mark.parametrize("name", ["ci.yml", "codeql.yml", "deploy.yml"])
def test_third_party_actions_are_pinned_to_full_commit_shas(name: str) -> None:
    references = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", _read(name))
    assert references, f"{name} must use at least one action"
    for reference in references:
        action, separator, revision = reference.rpartition("@")
        assert separator and action, f"invalid action reference in {name}: {reference}"
        assert re.fullmatch(r"[0-9a-f]{40}", revision), (
            f"{name} action must be pinned to a full commit SHA: {reference}"
        )


# --- CI -----------------------------------------------------------------------
def test_ci_triggers_on_pull_request() -> None:
    text = _read("ci.yml")
    assert _has_key(text, "pull_request"), "CI must run on pull_request"


def test_ci_has_backend_frontend_and_bicep_jobs() -> None:
    text = _read("ci.yml")
    for job in ("backend:", "frontend:", "bicep:"):
        assert re.search(rf"(?m)^  {job}", text), f"CI missing job {job}"


def test_ci_uses_least_privilege_permissions() -> None:
    text = _read("ci.yml")
    assert re.search(r"(?m)^permissions:\s*$", text), "CI must declare top-level permissions"
    assert re.search(r"(?m)^\s*contents:\s*read\s*$", text), "CI must grant contents: read"
    assert "write" not in re.split(r"(?m)^jobs:", text)[0], "top-level CI permissions must be read-only"


def test_ci_runs_the_workflow_policy_tests() -> None:
    text = _read("ci.yml")
    assert "scripts/tests" in text, "CI must run the scripts/tests policy suite"


def test_ci_uses_frontend_supported_node_runtime() -> None:
    text = _read("ci.yml")
    assert re.search(r"(?m)^\s*node-version:\s*['\"]?24['\"]?\s*$", text), (
        "frontend test dependencies require Node 24"
    )


# --- CodeQL -------------------------------------------------------------------
def test_codeql_runs_on_pull_request_and_main() -> None:
    text = _read("codeql.yml")
    assert _has_key(text, "pull_request"), "CodeQL must run on pull_request"
    assert _has_key(text, "push"), "CodeQL must run on push to main"


def test_codeql_covers_python_and_javascript_typescript() -> None:
    text = _read("codeql.yml")
    match = re.search(r"(?m)^\s*language:\s*\[([^\]]+)\]", text)
    assert match, "CodeQL must define a language matrix"
    languages = {item.strip() for item in match.group(1).split(",")}
    assert languages == {"python", "javascript-typescript"}, languages


def test_codeql_initializes_and_analyzes_with_sarif_permission() -> None:
    text = _read("codeql.yml")
    assert "github/codeql-action/init" in text
    assert "github/codeql-action/analyze" in text
    assert re.search(r"(?m)^\s*security-events:\s*write\s*$", text), "CodeQL needs security-events: write"


# --- Deploy -------------------------------------------------------------------
def test_deploy_runs_on_main_and_workflow_dispatch() -> None:
    text = _read("deploy.yml")
    assert _has_key(text, "push"), "deploy must run on push to main"
    assert _has_key(text, "workflow_dispatch"), "deploy must support manual dispatch"


def test_deploy_uses_oidc_and_production_environment() -> None:
    text = _read("deploy.yml")
    assert re.search(r"(?m)^\s*id-token:\s*write\s*$", text), "deploy needs id-token: write for OIDC"
    assert re.search(r"(?m)^\s*contents:\s*read\s*$", text), "deploy needs contents: read"
    assert re.search(r"(?m)^\s*environment:\s*production\s*$", text), "deploy must target the production environment"
    assert _has_key(text, "concurrency"), "deploy must serialize with a concurrency group"


def test_deploy_authenticates_without_secrets() -> None:
    text = _read("deploy.yml")
    assert "azure/login@" in text, "deploy must use azure/login"
    lowered = text.lower()
    assert "client-secret" not in lowered, "deploy must not use a client secret"
    assert "secrets." not in text, "deploy must be OIDC-only and reference no secrets"


def test_deploy_builds_and_pushes_images_on_github() -> None:
    text = _read("deploy.yml")
    assert "docker build --pull" in text, "deploy must build release images on the GitHub runner"
    assert "docker push" in text, "deploy must push release images to ACR"
    assert "az acr login" in text, "deploy must authenticate Docker to ACR"
    assert "/backend:${GITHUB_SHA}" in text, "backend image must be tagged with the commit SHA"
    assert "/frontend:${GITHUB_SHA}" in text, "frontend image must be tagged with the commit SHA"


def test_deploy_does_not_provision_or_use_acr_tasks() -> None:
    text = _read("deploy.yml")
    assert "az acr build" not in text, "release images must not be built with ACR Tasks"
    assert "azd provision" not in text, "deploy must not provision infrastructure"
    assert "azd " not in text, "deploy must not use azd"
    assert "scripts/deploy.ps1" not in text, "deploy must not delegate to deploy.ps1"


def test_deploy_updates_container_apps_and_cleanup_job() -> None:
    text = _read("deploy.yml")
    assert text.count("az containerapp update") >= 3, "API, worker, and frontend apps must be updated"
    assert "az containerapp job update" in text, "the cleanup job image must be updated"
    assert "API_UPSTREAM=${API_URL}" in text, "frontend proxy must target the API URL"
    assert text.count("ANALYZER_ROUTER_ID=${ANALYZER_ROUTER_ID}") == 3, (
        "API, worker, and cleanup must receive the GA-compatible router ID"
    )


def test_deploy_maps_production_variables_into_job_env() -> None:
    text = _read("deploy.yml")
    required = (
        "AZURE_RESOURCE_GROUP",
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT",
        "API_CONTAINER_APP_NAME",
        "WORKER_CONTAINER_APP_NAME",
        "CLEANUP_JOB_NAME",
        "FRONTEND_CONTAINER_APP_NAME",
        "API_URL",
        "FRONTEND_URL",
        "FOUNDRY_ENDPOINT",
        "SEARCH_ENDPOINT",
        "SEARCH_INDEX_NAME",
        "CHAT_DEPLOYMENT",
        "EMBEDDING_DEPLOYMENT",
        "ANALYZER_ROUTER_ID",
    )
    for name in required:
        assert f"{name}: ${{{{ vars.{name} }}}}" in text, f"deploy job env missing {name}"


def test_deploy_runs_bootstrap_and_smoke_via_backend_uv() -> None:
    text = _read("deploy.yml")
    assert "uv sync" in text, "deploy must sync the backend (enterprise index from pyproject)"
    assert "scripts/bootstrap-data-plane.py" in text, "deploy must bootstrap the data plane"
    assert "scripts/smoke_test.py" in text, "deploy must run the deployed smoke test"
    assert "Wait for frontend API proxy" in text
    assert '"$FRONTEND_URL/api/session"' in text
    assert "for attempt in" in text, "frontend proxy wait must be bounded"
    attempts = int(re.search(r"seq 1 (\d+)", text).group(1))
    request_timeout = int(re.search(r"--max-time (\d+)", text).group(1))
    retry_delay = int(re.search(r"sleep (\d+)", text).group(1))
    assert attempts * request_timeout + (attempts - 1) * retry_delay <= 135
    assert '--api-base "$FRONTEND_URL"' in text, (
        "the release smoke must exercise the public frontend /api proxy"
    )


def test_deploy_runs_preliminary_then_exactly_one_full_ranged_production_smoke() -> None:
    text = _read("deploy.yml")
    smoke_commands = re.findall(r"uv --project backend run python scripts/smoke_test\.py[^\n]+", text)
    assert len(smoke_commands) == 2, "deploy must run preliminary and ranged production smokes"
    assert all('--api-base "$FRONTEND_URL"' in command for command in smoke_commands)
    assert all('--frontend-origin "$FRONTEND_URL"' in command for command in smoke_commands)
    ranged = next(command for command in smoke_commands if "--content-range" in command)
    preliminary = next(command for command in smoke_commands if "--content-range" not in command)
    assert "--skip-live-model" in preliminary
    assert '"${args[@]}"' not in preliminary
    assert "--generated-pages 3" in ranged
    assert "--content-range 2-3" in ranged
    assert "--expect-pages 2" in ranged
    assert "--question" in ranged
    assert "--expect" in ranged
    assert text.count("args+=(--skip-live-model)") == 1
    assert '"${args[@]}"' in ranged


# --- Supporting delivery configuration ---------------------------------------
def test_copilot_instructions_and_configure_script_exist() -> None:
    assert (REPO_ROOT / ".github" / "copilot-instructions.md").is_file()
    configure = REPO_ROOT / "scripts" / "configure-github.ps1"
    assert configure.is_file()
    text = configure.read_text(encoding="utf-8")
    assert "automatic_copilot_code_review_enabled" in text, "configure script must enable Copilot review"
    assert "required_status_checks" in text, "configure script must require status checks (CodeQL blocking)"


def test_configure_script_publishes_deploy_variables() -> None:
    configure = REPO_ROOT / "scripts" / "configure-github.ps1"
    text = configure.read_text(encoding="utf-8")
    for name in (
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT",
        "API_CONTAINER_APP_NAME",
        "WORKER_CONTAINER_APP_NAME",
        "CLEANUP_JOB_NAME",
        "FRONTEND_CONTAINER_APP_NAME",
        "ANALYZER_ROUTER_ID",
    ):
        assert name in text, f"configure script must publish {name}"
