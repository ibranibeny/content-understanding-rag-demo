import copy
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

APPROVED_INDEX = "https://packagefeedproxy.microsoft.io/pypi/simple/"
APPROVED_INDEX_NAME = "microsoft-enterprise"
APPROVED_PROXY_HOST = "packagefeedproxy.microsoft.io"
APPROVED_DEPENDENCY_HOSTS = frozenset(
    {
        APPROVED_PROXY_HOST,
        "ms-feed-2.pkgs.visualstudio.com",
        "ms-feed-12.pkgs.visualstudio.com",
        "ms-feed-17.pkgs.visualstudio.com",
        "ms-feed-25.pkgs.visualstudio.com",
    }
)
UNKNOWN_PUBLIC_MIRROR = "https://mirror.example.org/simple"
EXTERNAL_FILE_URL = "file://external-host/package.whl"
UNRELATED_AZURE_ARTIFACTS_URL = "https://unrelated.pkgs.visualstudio.com/feed/package.whl"
ADDITIONAL_INDEX_KEYS = ("index-url", "extra-index-url", "find-links")


def _mapping(value: object, description: str) -> Mapping[str, object]:
    assert isinstance(value, Mapping), f"{description} must be a TOML table"
    return value


def _iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, nested_value in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            yield from _iter_strings(nested_value)


def assert_enterprise_feed_only(
    pyproject: Mapping[str, object], lockfile: Mapping[str, object]
) -> None:
    tool = _mapping(pyproject.get("tool"), "tool")
    uv = _mapping(tool.get("uv"), "tool.uv")
    indexes = uv.get("index")
    assert isinstance(indexes, list), "tool.uv.index must be an array of tables"
    assert len(indexes) == 1, "tool.uv.index must contain exactly one index"

    index = _mapping(indexes[0], "tool.uv.index[0]")
    assert index.get("name") == APPROVED_INDEX_NAME
    assert index.get("url") == APPROVED_INDEX
    assert index.get("default") is True

    for key in ADDITIONAL_INDEX_KEYS:
        assert key not in uv, f"tool.uv.{key} can configure an additional package source"

    for document in (pyproject, lockfile):
        for value in _iter_strings(document):
            parsed = urlsplit(value)
            scheme = parsed.scheme.lower()
            if scheme == "file":
                assert parsed.hostname in (None, ""), (
                    f"file dependency URL must not include a host: {parsed.hostname}"
                )
                continue
            if scheme not in {"http", "https"} and parsed.hostname is None:
                continue

            assert scheme == "https", "network dependency URL must use HTTPS"
            host = parsed.hostname.lower() if parsed.hostname else None
            assert host is not None, "network dependency URL must include a host"
            assert host in APPROVED_DEPENDENCY_HOSTS, (
                f"network dependency host is not approved: {host}"
            )


def _load_dependency_documents() -> tuple[dict[str, object], dict[str, object]]:
    backend_dir = Path(__file__).resolve().parents[1]
    with (backend_dir / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    with (backend_dir / "uv.lock").open("rb") as lockfile_file:
        lockfile = tomllib.load(lockfile_file)
    return pyproject, lockfile


def test_python_dependencies_use_only_the_enterprise_feed() -> None:
    pyproject, lockfile = _load_dependency_documents()

    assert_enterprise_feed_only(pyproject, lockfile)


def test_async_azure_transport_is_an_explicit_locked_dependency() -> None:
    pyproject, lockfile = _load_dependency_documents()
    project = _mapping(pyproject.get("project"), "project")
    dependencies = project.get("dependencies")
    assert isinstance(dependencies, list)
    assert "aiohttp>=3.12,<4" in dependencies
    packages = lockfile.get("package")
    assert isinstance(packages, list)
    assert any(
        isinstance(package, Mapping) and package.get("name") == "aiohttp"
        for package in packages
    )


def test_dependency_policy_rejects_an_unknown_public_mirror() -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_lockfile = copy.deepcopy(lockfile)
    mutated_lockfile["unexpected-source"] = UNKNOWN_PUBLIC_MIRROR

    with pytest.raises(AssertionError, match="mirror.example.org"):
        assert_enterprise_feed_only(pyproject, mutated_lockfile)


def test_dependency_policy_rejects_file_urls_with_an_external_host() -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_lockfile = copy.deepcopy(lockfile)
    mutated_lockfile["unexpected-source"] = EXTERNAL_FILE_URL

    with pytest.raises(AssertionError, match="external-host"):
        assert_enterprise_feed_only(pyproject, mutated_lockfile)


def test_dependency_policy_rejects_unrelated_azure_artifacts_hosts() -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_lockfile = copy.deepcopy(lockfile)
    mutated_lockfile["unexpected-source"] = UNRELATED_AZURE_ARTIFACTS_URL

    with pytest.raises(AssertionError, match="unrelated.pkgs.visualstudio.com"):
        assert_enterprise_feed_only(pyproject, mutated_lockfile)


@pytest.mark.parametrize(
    "unsupported_url",
    [
        "http://packagefeedproxy.microsoft.io/pypi/simple/",
        "ftp://ms-feed-2.pkgs.visualstudio.com/path",
        "//ms-feed-12.pkgs.visualstudio.com/path",
    ],
)
def test_dependency_policy_rejects_non_https_network_urls(unsupported_url: str) -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_lockfile = copy.deepcopy(lockfile)
    mutated_lockfile["unexpected-source"] = unsupported_url

    with pytest.raises(AssertionError, match="HTTPS"):
        assert_enterprise_feed_only(pyproject, mutated_lockfile)


def test_dependency_policy_rejects_default_false() -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_pyproject = copy.deepcopy(pyproject)
    tool = cast(dict[str, object], mutated_pyproject["tool"])
    uv = cast(dict[str, object], tool["uv"])
    indexes = cast(list[dict[str, object]], uv["index"])
    indexes[0]["default"] = False

    with pytest.raises(AssertionError):
        assert_enterprise_feed_only(mutated_pyproject, lockfile)


def test_dependency_policy_rejects_additional_index_configuration() -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_pyproject = copy.deepcopy(pyproject)
    tool = cast(dict[str, object], mutated_pyproject["tool"])
    uv = cast(dict[str, object], tool["uv"])
    uv["extra-index-url"] = APPROVED_INDEX

    with pytest.raises(AssertionError, match="additional package source"):
        assert_enterprise_feed_only(mutated_pyproject, lockfile)


@pytest.mark.parametrize("local_url", ["file:///tmp/package.whl", "file:///C:/package.whl"])
def test_dependency_policy_allows_local_file_urls(local_url: str) -> None:
    pyproject, lockfile = _load_dependency_documents()
    mutated_lockfile = copy.deepcopy(lockfile)
    mutated_lockfile["local-source"] = local_url

    assert_enterprise_feed_only(pyproject, mutated_lockfile)
