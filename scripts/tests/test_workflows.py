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


def test_deploy_delegates_to_existing_deploy_script() -> None:
    text = _read("deploy.yml")
    assert "scripts/deploy.ps1" in text, "deploy must call the existing deploy.ps1"


# --- Supporting delivery configuration ---------------------------------------
def test_copilot_instructions_and_configure_script_exist() -> None:
    assert (REPO_ROOT / ".github" / "copilot-instructions.md").is_file()
    configure = REPO_ROOT / "scripts" / "configure-github.ps1"
    assert configure.is_file()
    text = configure.read_text(encoding="utf-8")
    assert "automatic_copilot_code_review_enabled" in text, "configure script must enable Copilot review"
    assert "required_status_checks" in text, "configure script must require status checks (CodeQL blocking)"
