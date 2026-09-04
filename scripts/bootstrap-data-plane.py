#!/usr/bin/env python
"""Idempotent Content Understanding + AI Search data-plane bootstrap (keyless, token-only).

Reuses the application adapters so the bootstrap and the running services agree on the exact
analyzer/router shapes, index schema, and API version. Creates or replaces the four extraction
analyzers and the router, configures the resource-level Content Understanding default model
deployments (2025-11-01 GA), and ensures the AI Search index. Authentication is Microsoft Entra
only via DefaultAzureCredential; no keys or connection strings are read, printed, or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ANALYZERS_DIR = REPO_ROOT / "analyzers"
DEFAULT_ROUTER_ID = "business_document_router"
DEFAULT_INDEX_NAME = "document-chunks"
DEFAULT_CHAT_DEPLOYMENT = "gpt-5"
DEFAULT_EMBEDDING_DEPLOYMENT = "text-embedding-3-large"


class AnalyzerWriter(Protocol):
    async def create_or_replace_analyzer(
        self, analyzer_id: str, definition: Mapping[str, Any]
    ) -> str | None: ...

    async def wait_for_analyzer(self, operation_url: str) -> None: ...

    async def update_defaults(self, model_deployments: Mapping[str, str]) -> None: ...


class IndexEnsurer(Protocol):
    async def create_or_update_index(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AnalyzerDefinition:
    analyzer_id: str
    definition: Mapping[str, Any]
    is_router: bool


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    analyzer_ids: tuple[str, ...]
    router_id: str
    defaults_configured: bool
    index_name: str


def load_analyzer_definitions(analyzers_dir: Path, router_id: str) -> list[AnalyzerDefinition]:
    """Load analyzer JSON files, ordering extraction analyzers before the router that references them."""
    files = sorted(analyzers_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no analyzer definitions found in {analyzers_dir}")
    extraction: list[AnalyzerDefinition] = []
    routers: list[AnalyzerDefinition] = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        analyzer_id = raw.get("analyzerId")
        if not isinstance(analyzer_id, str) or not analyzer_id:
            raise ValueError(f"{path.name} is missing a string 'analyzerId'")
        config = raw.get("config")
        is_router = analyzer_id == router_id or (
            isinstance(config, dict) and "contentCategories" in config
        )
        item = AnalyzerDefinition(analyzer_id=analyzer_id, definition=raw, is_router=is_router)
        (routers if is_router else extraction).append(item)
    if len(routers) != 1:
        raise ValueError(
            f"expected exactly one router analyzer (id={router_id!r}); found {len(routers)}"
        )
    return extraction + routers


def build_defaults(chat_deployment: str, embedding_deployment: str) -> dict[str, str]:
    """Map Content Understanding model names to Foundry deployment names.

    This deployment names each model deployment after its base model, so keys and values coincide
    (for example ``gpt-5`` -> ``gpt-5``). The mapping shape matches the 2025-11-01 ``defaults`` API.
    """
    return {
        chat_deployment: chat_deployment,
        embedding_deployment: embedding_deployment,
        "prebuilt-analyzer-completion": chat_deployment,
        "prebuilt-analyzer-completion-mini": chat_deployment,
        "prebuilt-analyzer-embedding": embedding_deployment,
    }


async def bootstrap(
    analyzer_writer: AnalyzerWriter,
    index_ensurer: IndexEnsurer,
    definitions: Sequence[AnalyzerDefinition],
    *,
    defaults: Mapping[str, str] | None,
    index_name: str,
) -> BootstrapReport:
    """Apply analyzers, optional defaults, and the search index. Idempotent: safe to re-run."""
    created: list[str] = []
    router_id = ""
    defaults_configured = False
    if defaults:
        await analyzer_writer.update_defaults(defaults)
        defaults_configured = True
    for item in definitions:
        operation = await analyzer_writer.create_or_replace_analyzer(
            item.analyzer_id, item.definition
        )
        if operation is not None:
            await analyzer_writer.wait_for_analyzer(operation)
        created.append(item.analyzer_id)
        if item.is_router:
            router_id = item.analyzer_id
    await index_ensurer.create_or_update_index()
    return BootstrapReport(
        analyzer_ids=tuple(created),
        router_id=router_id,
        defaults_configured=defaults_configured,
        index_name=index_name,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Content Understanding data-plane bootstrap")
    parser.add_argument("--foundry-endpoint", default=None)
    parser.add_argument("--search-endpoint", default=None)
    parser.add_argument("--search-index", default=None)
    parser.add_argument("--chat-deployment", default=None)
    parser.add_argument("--embedding-deployment", default=None)
    parser.add_argument("--router-id", default=None)
    parser.add_argument("--analyzers-dir", default=str(DEFAULT_ANALYZERS_DIR))
    parser.add_argument(
        "--skip-defaults",
        action="store_true",
        help="Do not configure resource-level Content Understanding default model deployments.",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    foundry_endpoint = args.foundry_endpoint or os.environ.get("FOUNDRY_ENDPOINT")
    search_endpoint = args.search_endpoint or os.environ.get("SEARCH_ENDPOINT")
    if not foundry_endpoint or not search_endpoint:
        print(
            "error: FOUNDRY_ENDPOINT and SEARCH_ENDPOINT must be set (env or flags).",
            file=sys.stderr,
        )
        return 2
    index_name = args.search_index or os.environ.get("SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME)
    router_id = args.router_id or os.environ.get("ANALYZER_ROUTER_ID", DEFAULT_ROUTER_ID)
    chat = args.chat_deployment or os.environ.get("CHAT_DEPLOYMENT", DEFAULT_CHAT_DEPLOYMENT)
    embedding = args.embedding_deployment or os.environ.get(
        "EMBEDDING_DEPLOYMENT", DEFAULT_EMBEDDING_DEPLOYMENT
    )

    definitions = load_analyzer_definitions(Path(args.analyzers_dir), router_id)
    defaults = None if args.skip_defaults else build_defaults(chat, embedding)

    # Deferred imports keep the pure logic above importable and unit-testable without Azure SDKs.
    from azure.identity.aio import DefaultAzureCredential

    from app.services.content_understanding import ContentUnderstandingClient
    from app.services.search_service import AzureSearchService

    credential = DefaultAzureCredential()
    analyzer_client = ContentUnderstandingClient(
        foundry_endpoint, credential=credential, owns_credential=False
    )
    search_service = AzureSearchService(
        search_endpoint, index_name, credential=credential, owns_credential=False
    )
    try:
        report = await bootstrap(
            analyzer_client,
            search_service,
            definitions,
            defaults=defaults,
            index_name=index_name,
        )
    finally:
        await analyzer_client.aclose()
        await search_service.aclose()
        await credential.close()

    print(
        f"bootstrap ok: {len(report.analyzer_ids)} analyzers ensured "
        f"(router={report.router_id}), "
        f"defaults={'configured' if report.defaults_configured else 'skipped'}, "
        f"index={report.index_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
