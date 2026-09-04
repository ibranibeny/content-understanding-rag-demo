import pytest

from app.services.content_range import (
    MAX_CONTENT_PAGES,
    InvalidContentRange,
    normalize_content_range,
)


def test_max_content_pages_is_300() -> None:
    assert MAX_CONTENT_PAGES == 300


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "1"),
        (" 1 - 3 , 5 , 9 - 12 ", "1-3,5,9-12"),
        ("1-300", "1-300"),
        ("7-7", "7"),
        ("5, 1-2", "5,1-2"),
    ],
)
def test_valid_ranges_are_normalized_in_token_order(raw: str, expected: str) -> None:
    assert normalize_content_range(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("0", "page_below_one"),
        ("3-1", "range_reversed"),
        ("1-", "invalid_syntax"),
        ("-5", "invalid_syntax"),
        ("a", "invalid_syntax"),
        ("1,,2", "invalid_syntax"),
        ("1-3,3", "duplicate_or_overlap"),
        ("1-5,2-4", "duplicate_or_overlap"),
        ("1-301", "too_many_pages"),
    ],
)
def test_invalid_ranges_have_stable_machine_readable_reasons(raw: str, reason: str) -> None:
    with pytest.raises(InvalidContentRange) as caught:
        normalize_content_range(raw)

    assert caught.value.reason == reason


def test_more_than_300_discrete_pages_is_rejected() -> None:
    raw = ",".join(str(page) for page in range(1, 302, 2))
    raw += "," + ",".join(str(page) for page in range(2, 301, 2))

    with pytest.raises(InvalidContentRange) as caught:
        normalize_content_range(raw)

    assert caught.value.reason == "too_many_pages"
