"""Infrastructure policy tests.

Compiles ``infra/main.bicep`` to ARM JSON with ``az bicep`` and asserts the MVP
invariants: resource-group scope, correct regions, the two fixed models, the required
resources, keyless access, managed identities, and exactly two container images.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parent.parent
MAIN_BICEP = INFRA_DIR / "main.bicep"

BOOTSTRAP_IMAGE_PREFIX = "mcr.microsoft.com/azuredocs/containerapps-helloworld"
COMPUTE_APPS = ("frontendApp", "apiApp", "workerApp", "cleanupJob")
BACKEND_TARGETS = ("apiApp", "workerApp", "cleanupJob")


@pytest.fixture(scope="module")
def template() -> dict:
    az = shutil.which("az")
    if az is None:
        pytest.skip("Azure CLI (az) is required to compile Bicep")
    result = subprocess.run(
        [az, "bicep", "build", "--file", str(MAIN_BICEP), "--stdout"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bicep build failed:\n{result.stderr}"
    assert ": Warning" not in result.stderr, f"bicep build emitted warnings:\n{result.stderr}"
    assert ": Error" not in result.stderr, f"bicep build emitted errors:\n{result.stderr}"
    return json.loads(result.stdout)


def _iter_resources(container: object) -> list[dict]:
    if isinstance(container, dict):
        return [value for value in container.values() if isinstance(value, dict)]
    if isinstance(container, list):
        return [value for value in container if isinstance(value, dict)]
    return []


def _flatten(template: dict) -> list[dict]:
    """All resources, recursing into inlined AVM module (nested deployment) templates."""
    found: list[dict] = []
    for resource in _iter_resources(template.get("resources")):
        found.append(resource)
        if resource.get("type") == "Microsoft.Resources/deployments":
            inner = resource.get("properties", {}).get("template")
            if isinstance(inner, dict):
                found.extend(_flatten(inner))
    return found


@pytest.fixture(scope="module")
def resources(template: dict) -> list[dict]:
    return _flatten(template)


@pytest.fixture(scope="module")
def types(resources: list[dict]) -> list[str]:
    return [resource.get("type") for resource in resources]


def _res(template: dict, symbolic_name: str) -> dict:
    return template["resources"][symbolic_name]


def _containers(template: dict, symbolic_name: str) -> list[dict]:
    return _res(template, symbolic_name)["properties"]["template"]["containers"]


# --------------------------------------------------------------------------- scope


def test_target_scope_is_resource_group(template: dict) -> None:
    schema = template["$schema"]
    assert "deploymentTemplate.json" in schema
    assert "subscriptionDeploymentTemplate" not in schema


# ------------------------------------------------------------------------- regions


def test_application_region_is_southeast_asia(template: dict) -> None:
    assert template["parameters"]["location"]["defaultValue"] == "southeastasia"
    assert _res(template, "storage")["location"] == "[parameters('location')]"


def test_foundry_region_is_east_us_2(template: dict) -> None:
    assert template["parameters"]["foundryLocation"]["defaultValue"] == "eastus2"
    assert _res(template, "aiFoundry")["location"] == "[parameters('foundryLocation')]"


# -------------------------------------------------------------------------- models


def test_model_names_are_fixed(template: dict) -> None:
    variables = template["variables"]
    assert variables["chatDeployment"] == "gpt-5"
    assert variables["embeddingDeployment"] == "text-embedding-3-large"
    assert variables["embeddingDimensions"] == "3072"


def test_model_deployment_skus(template: dict, types: list[str]) -> None:
    assert types.count("Microsoft.CognitiveServices/accounts/deployments") == 2
    assert _res(template, "gptDeployment")["sku"]["name"] == "GlobalStandard"
    assert _res(template, "embeddingModelDeployment")["sku"]["name"] == "Standard"


def test_embedding_dimensions_flow_to_the_backend(template: dict) -> None:
    env = {item["name"]: item.get("value") for item in _containers(template, "apiApp")[0]["env"]}
    assert "EMBEDDING_DIMENSIONS" in env
    assert env["EMBEDDING_DIMENSIONS"] == "[variables('embeddingDimensions')]"


# ----------------------------------------------------------------------- resources


def test_required_resources_present(template: dict) -> None:
    top = template["resources"]
    counts: dict[str, int] = {}
    for resource in top.values():
        counts[resource["type"]] = counts.get(resource["type"], 0) + 1
    assert counts.get("Microsoft.App/containerApps") == 3
    assert counts.get("Microsoft.App/jobs") == 1
    assert counts.get("Microsoft.Storage/storageAccounts") == 1
    assert counts.get("Microsoft.CognitiveServices/accounts") == 1
    assert counts.get("Microsoft.CognitiveServices/accounts/deployments") == 2
    module_names = {
        resource["name"]
        for resource in top.values()
        if resource["type"] == "Microsoft.Resources/deployments"
    }
    assert {
        "app-identity",
        "acrpull-identity",
        "log-analytics",
        "app-insights",
        "container-registry",
        "search-service",
        "managed-environment",
    } <= module_names


def test_github_oidc_subject_uses_immutable_owner_and_repository_ids(
    template: dict,
) -> None:
    github_identity = _res(template, "githubIdentity")
    serialized = json.dumps(github_identity)
    assert "https://token.actions.githubusercontent.com" in serialized
    assert "api://AzureADTokenExchange" in serialized
    assert "githubOwnerId" in serialized
    assert "githubRepositoryId" in serialized
    assert "environment:production" in serialized


def test_storage_child_objects_present(template: dict) -> None:
    variables = template["variables"]
    assert [variables["uploadsContainer"], variables["derivedContainer"], variables["controlContainer"]] == [
        "uploads",
        "derived",
        "control",
    ]
    assert [variables["ingestionQueue"], variables["cleanupQueue"], variables["poisonQueue"]] == [
        "ingestion",
        "cu-result-cleanup",
        "ingestion-poison",
    ]
    assert variables["tableName"] == "workshop"
    types = [_res(template, name)["type"] for name in ("blobContainers", "storageQueues", "workshopTable")]
    assert "Microsoft.Storage/storageAccounts/blobServices/containers" in types
    assert "Microsoft.Storage/storageAccounts/queueServices/queues" in types
    assert "Microsoft.Storage/storageAccounts/tableServices/tables" in types


def test_blob_cors_is_restricted_to_the_frontend_origin(template: dict) -> None:
    rules = _res(template, "blobService")["properties"]["cors"]["corsRules"]
    assert len(rules) == 1
    assert rules[0]["allowedMethods"] == ["PUT", "OPTIONS"]
    origin = rules[0]["allowedOrigins"][0]
    assert "https://" in origin and "frontendAppName" in origin


def test_lifecycle_expires_application_data(template: dict) -> None:
    rule = _res(template, "storageLifecycle")["properties"]["policy"]["rules"][0]
    assert rule["definition"]["actions"]["baseBlob"]["delete"]["daysAfterCreationGreaterThan"] == 1


# -------------------------------------------------------------------------- keyless


def test_storage_is_keyless(template: dict) -> None:
    props = _res(template, "storage")["properties"]
    assert props["allowSharedKeyAccess"] is False
    assert props["allowBlobPublicAccess"] is False
    assert props["minimumTlsVersion"] == "TLS1_2"


def test_foundry_is_keyless(template: dict) -> None:
    props = _res(template, "aiFoundry")["properties"]
    assert props["disableLocalAuth"] is True
    assert props["customSubDomainName"] == "[variables('foundryName')]"


def test_search_is_keyless_basic_semantic(template: dict) -> None:
    params = _res(template, "search")["properties"]["parameters"]
    assert params["disableLocalAuth"]["value"] is True
    assert params["sku"]["value"] == "basic"
    assert params["semanticSearch"]["value"] == "standard"


def test_acr_admin_disabled(template: dict) -> None:
    params = _res(template, "acr")["properties"]["parameters"]
    assert params["acrAdminUserEnabled"]["value"] is False
    assert params["acrSku"]["value"] == "Basic"


def test_no_key_material_in_container_environment(template: dict) -> None:
    forbidden = ("listKeys", "AccountKey", "accountKey", "SharedAccessKey", "SharedKey")
    for name in COMPUTE_APPS:
        for container in _containers(template, name):
            for item in container.get("env", []):
                value = item.get("value", "")
                assert not any(token in value for token in forbidden), f"{name}:{item['name']}"
                assert "secretRef" not in item, f"{name}:{item['name']}"


# ------------------------------------------------------------------ managed identity


def test_compute_uses_two_user_assigned_identities(template: dict) -> None:
    shared = template["variables"]["computeIdentity"]
    assert shared["type"] == "UserAssigned"
    assert len(shared["userAssignedIdentities"]) == 2
    for name in COMPUTE_APPS:
        assert _res(template, name)["identity"] == "[variables('computeIdentity')]"


def test_user_assigned_identities_exist(types: list[str]) -> None:
    assert types.count("Microsoft.ManagedIdentity/userAssignedIdentities") >= 2


def test_keda_queue_rules_use_managed_identity(template: dict) -> None:
    rules = _res(template, "workerApp")["properties"]["template"]["scale"]["rules"]
    queue_rules = [rule for rule in rules if "azureQueue" in rule]
    assert len(queue_rules) == 2
    for rule in queue_rules:
        assert "identity" in rule["azureQueue"]
        assert "accountName" in rule["azureQueue"]
        assert "auth" not in rule["azureQueue"]


def test_least_privilege_role_definitions(template: dict) -> None:
    roles = template["variables"]["roles"]
    assert roles["contentUnderstandingOwner"] == "4b42bd01-da42-4c92-9b07-15ea5bd6a602"
    assert roles["cognitiveServicesOpenAiUser"] == "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    assert roles["storageBlobDelegator"] == "db58b8e5-c6ad-4a2a-8342-4190687cbf4a"
    assert roles["acrPull"] == "7f951dda-4ed3-4680-a7ca-43fe172d538d"


def test_role_assignments_target_service_principals(types: list[str]) -> None:
    assert types.count("Microsoft.Authorization/roleAssignments") >= 4


# --------------------------------------------------------------------- two images


def test_exactly_two_image_parameters(template: dict) -> None:
    image_params = {name for name in template["parameters"] if name.lower().endswith("image")}
    assert image_params == {"frontendImage", "backendImage"}


def test_bootstrap_defaults_are_public_images(template: dict) -> None:
    for name in ("frontendImage", "backendImage"):
        assert template["parameters"][name]["defaultValue"].startswith(BOOTSTRAP_IMAGE_PREFIX)


def test_frontend_uses_the_frontend_image(template: dict) -> None:
    assert _containers(template, "frontendApp")[0]["image"] == "[parameters('frontendImage')]"


def test_shared_backend_image_and_environment(template: dict) -> None:
    for name in BACKEND_TARGETS:
        assert _containers(template, name)[0]["image"] == "[parameters('backendImage')]"
    api_env = _containers(template, "apiApp")[0]["env"]
    worker_env = _containers(template, "workerApp")[0]["env"]
    cleanup_env = _containers(template, "cleanupJob")[0]["env"]
    assert api_env == worker_env == cleanup_env


# ---------------------------------------------------------------------- ingress


def test_api_is_public_with_cors_and_worker_has_no_ingress(template: dict) -> None:
    api_ingress = _res(template, "apiApp")["properties"]["configuration"]["ingress"]
    assert api_ingress["external"] is True
    origin = api_ingress["corsPolicy"]["allowedOrigins"][0]
    assert "https://" in origin and "frontendAppName" in origin
    assert "ingress" not in _res(template, "workerApp")["properties"]["configuration"]


def test_cleanup_job_runs_on_schedule(template: dict) -> None:
    config = _res(template, "cleanupJob")["properties"]["configuration"]
    assert config["triggerType"] == "Schedule"
    assert config["scheduleTriggerConfig"]["cronExpression"] == "0 * * * *"


# ---------------------------------------------------------------------- outputs


def test_outputs_expose_endpoints_and_identities(template: dict) -> None:
    outputs = set(template["outputs"])
    expected = {
        "FRONTEND_URL",
        "API_URL",
        "FOUNDRY_ENDPOINT",
        "CHAT_DEPLOYMENT",
        "EMBEDDING_DEPLOYMENT",
        "STORAGE_ACCOUNT_NAME",
        "SEARCH_ENDPOINT",
        "CONTAINER_APPS_ENVIRONMENT_NAME",
        "APP_IDENTITY_CLIENT_ID",
        "ACR_PULL_IDENTITY_RESOURCE_ID",
    }
    assert expected <= outputs
