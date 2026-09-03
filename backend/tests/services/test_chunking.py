from uuid import UUID

import pytest

from app.services.chunking import chunk_markdown, count_tokens

DOC_ID = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")


def test_chunks_preserve_heading_page_limit_and_section_local_overlap() -> None:
    markdown = (
        "# Agreement\n<!-- PageNumber=1 -->\n"
        + ("alpha " * 900)
        + "\n## Renewal\n<!-- PageNumber=2 -->\n"
        + ("beta " * 300)
    )

    chunks = chunk_markdown(markdown, document_id=DOC_ID, max_tokens=800, overlap_tokens=120)

    assert all(count_tokens(chunk.content) <= 800 for chunk in chunks)
    assert chunks[-1].section_path == "Agreement > Renewal"
    assert chunks[-1].source_locator == "page 2"
    assert chunks[-1].page_number == 2
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    renewal = [chunk for chunk in chunks if chunk.section_path == "Agreement > Renewal"]
    assert all("alpha" not in chunk.content for chunk in renewal)


def test_prefers_paragraph_and_sentence_boundaries_before_token_windows() -> None:
    markdown = "# Notes\nFirst paragraph.\n\nSecond sentence. Third sentence."

    chunks = chunk_markdown(markdown, document_id=DOC_ID, max_tokens=6, overlap_tokens=1)

    assert chunks[0].content == "First paragraph."
    assert all(count_tokens(chunk.content) <= 6 for chunk in chunks)
    assert "" not in [chunk.content for chunk in chunks]


def test_slide_section_and_image_locators_are_preserved() -> None:
    markdown = (
        "# Deck\n<!-- SlideNumber=3 -->\nSlide text.\n"
        "## Diagram\n<!-- ImageNumber=2 -->\nImage description."
    )

    chunks = chunk_markdown(markdown, document_id=DOC_ID, max_tokens=100, overlap_tokens=10)

    assert [(chunk.section_path, chunk.source_locator) for chunk in chunks] == [
        ("Deck", "slide 3"),
        ("Deck > Diagram", "image 2"),
    ]


def test_chunk_ids_are_url_safe_and_deterministic() -> None:
    first = chunk_markdown("# A\nText.", document_id=DOC_ID)
    second = chunk_markdown("# A\nText.", document_id=DOC_ID)

    assert first == second
    assert all(set(chunk.chunk_id) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for chunk in first)


def test_invalid_chunk_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_markdown("text", document_id=DOC_ID, max_tokens=10, overlap_tokens=10)


def test_tokenizer_loader_failure_uses_deterministic_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import chunking

    monkeypatch.setattr(chunking, "_load_tiktoken", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    chunking._encoding.cache_clear()
    try:
        assert chunking.count_tokens("Hello, world!") == 4
        assert chunking.count_tokens("Hello, world!") == 4
        chunks = chunking.chunk_markdown("one two three four", document_id=DOC_ID, max_tokens=3, overlap_tokens=1)
        assert [chunk.content for chunk in chunks] == ["one two three", "three four"]
    finally:
        chunking._encoding.cache_clear()
