import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
EXPECTED = {
    "general-business": {"title", "summary", "documentDate", "organizations", "people", "keyTopics", "actionItems", "importantFacts"},
    "invoice": {"vendorName", "customerName", "invoiceNumber", "invoiceDate", "dueDate", "currency", "subtotal", "tax", "total", "lineItems"},
    "receipt": {"merchantName", "transactionDate", "currency", "subtotal", "tax", "total", "paymentMethod", "items"},
    "contract": {"title", "parties", "effectiveDate", "expirationDate", "renewalTerms", "governingLaw", "obligations", "terminationClauses", "riskFlags"},
}


def load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "analyzers" / f"{name}.json").read_text(encoding="utf-8"))


def test_four_analyzers_have_exact_static_schemas() -> None:
    for category, expected_fields in EXPECTED.items():
        analyzer = load(category)
        expected_id = f"workshop_{category.replace('-', '_')}"
        assert analyzer["analyzerId"] == expected_id
        assert re.fullmatch(r"[A-Za-z0-9._]{1,64}", analyzer["analyzerId"])
        assert analyzer["baseAnalyzerId"] == "prebuilt-document"
        assert analyzer["dynamicFieldSchema"] is False
        assert analyzer["config"]["returnDetails"] is True
        assert analyzer["config"]["tableFormat"] == "markdown"
        assert analyzer["models"] == {
            "completion": "gpt-5",
            "embedding": "text-embedding-3-large",
        }
        assert set(analyzer["fieldSchema"]["fields"]) == expected_fields


def test_router_has_only_four_explicit_category_routes_without_segmentation() -> None:
    router = load("router")
    categories = router["config"]["contentCategories"]
    assert router["analyzerId"] == "business_document_router"
    assert re.fullmatch(r"[A-Za-z0-9._]{1,64}", router["analyzerId"])
    assert router["baseAnalyzerId"] == "prebuilt-document"
    assert router["dynamicFieldSchema"] is False
    assert router["config"]["enableSegment"] is False
    assert router["config"]["omitContent"] is True
    assert router["models"] == {"completion": "gpt-5"}
    assert set(categories) == set(EXPECTED)
    for category, route in categories.items():
        assert route["analyzerId"] == f"workshop_{category.replace('-', '_')}"
        assert isinstance(route["description"], str) and 10 <= len(route["description"]) <= 160
