"""Unit tests for the idempotent data-plane bootstrap. Fakes stand in for the Azure adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

EXPECTED_IDS = {
    "workshop-general-business",
    "workshop-invoice",
    "workshop-receipt",
    "workshop-contract",
    "business-document-router",
}


class FakeAnalyzerWriter:
    def __init__(self) -> None:
        self.analyzers: list[tuple[str, dict[str, Any]]] = []
        self.defaults: dict[str, str] | None = None

    async def create_or_replace_analyzer(
        self, analyzer_id: str, definition: Mapping[str, Any]
    ) -> str | None:
        self.analyzers.append((analyzer_id, dict(definition)))
        return None

    async def update_defaults(self, model_deployments: Mapping[str, str]) -> None:
        self.defaults = dict(model_deployments)


class FakeIndexEnsurer:
    def __init__(self) -> None:
        self.calls = 0

    async def create_or_update_index(self) -> None:
        self.calls += 1


def test_load_orders_extraction_before_router(bootstrap_module: ModuleType, analyzers_dir: Path) -> None:
    definitions = bootstrap_module.load_analyzer_definitions(analyzers_dir, "business-document-router")
    assert len(definitions) == 5
    assert {item.analyzer_id for item in definitions} == EXPECTED_IDS
    assert [item.is_router for item in definitions] == [False, False, False, False, True]
    assert definitions[-1].analyzer_id == "business-document-router"


def test_load_rejects_missing_router(bootstrap_module: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "only.json").write_text('{"analyzerId": "workshop-only", "config": {}}', encoding="utf-8")
    try:
        bootstrap_module.load_analyzer_definitions(tmp_path, "business-document-router")
    except ValueError as exc:
        assert "router" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("expected a ValueError for a missing router")


def test_build_defaults_maps_model_to_deployment(bootstrap_module: ModuleType) -> None:
    defaults = bootstrap_module.build_defaults("gpt-5", "text-embedding-3-large")
    assert defaults == {"gpt-5": "gpt-5", "text-embedding-3-large": "text-embedding-3-large"}


def test_bootstrap_applies_analyzers_defaults_and_index(
    bootstrap_module: ModuleType, analyzers_dir: Path
) -> None:
    definitions = bootstrap_module.load_analyzer_definitions(analyzers_dir, "business-document-router")
    writer, index = FakeAnalyzerWriter(), FakeIndexEnsurer()
    report = asyncio.run(
        bootstrap_module.bootstrap(
            writer,
            index,
            definitions,
            defaults={"gpt-5": "gpt-5"},
            index_name="document-chunks",
        )
    )
    assert [analyzer_id for analyzer_id, _ in writer.analyzers][-1] == "business-document-router"
    assert writer.defaults == {"gpt-5": "gpt-5"}
    assert index.calls == 1
    assert report.router_id == "business-document-router"
    assert report.defaults_configured is True
    assert report.index_name == "document-chunks"


def test_bootstrap_is_repeatable_and_skips_defaults_when_none(
    bootstrap_module: ModuleType, analyzers_dir: Path
) -> None:
    definitions = bootstrap_module.load_analyzer_definitions(analyzers_dir, "business-document-router")
    for _ in range(2):
        writer, index = FakeAnalyzerWriter(), FakeIndexEnsurer()
        report = asyncio.run(
            bootstrap_module.bootstrap(
                writer, index, definitions, defaults=None, index_name="document-chunks"
            )
        )
        assert len(writer.analyzers) == 5
        assert writer.defaults is None
        assert index.calls == 1
        assert report.defaults_configured is False
