"""Shared fixtures for the deployment-script tests.

Loads the deployment scripts by file path (``bootstrap-data-plane.py`` is not a valid module name)
so their pure logic can be exercised with fakes and mocked HTTP transports. No Azure calls are made.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
ANALYZERS_DIR = Path(__file__).resolve().parents[2] / "analyzers"


def _load(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def bootstrap_module() -> ModuleType:
    return _load("bootstrap_data_plane", "bootstrap-data-plane.py")


@pytest.fixture(scope="session")
def smoke_module() -> ModuleType:
    return _load("smoke_test_script", "smoke_test.py")


@pytest.fixture(scope="session")
def analyzers_dir() -> Path:
    return ANALYZERS_DIR
