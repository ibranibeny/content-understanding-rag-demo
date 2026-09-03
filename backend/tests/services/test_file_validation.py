from contextlib import nullcontext
from io import BytesIO
from unicodedata import normalize
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from app.core.errors import AppError
from app.services.file_validation import (
    MAX_FILE_BYTES,
    sanitize_file_name,
    validate_declared_upload,
    validate_uploaded_file,
)


def office_package(*entries: tuple[str, bytes], compression: int = ZIP_STORED) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


DOCX = office_package(("[Content_Types].xml", b"types"), ("word/document.xml", b"doc"))
PPTX = office_package(("[Content_Types].xml", b"types"), ("ppt/presentation.xml", b"deck"))


@pytest.mark.parametrize(
    ("name", "mime", "content"),
    [
        ("a.pdf", "application/pdf", b"%PDF-1.7"),
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", DOCX),
        ("a.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", PPTX),
        ("a.png", "image/png", b"\x89PNG\r\n\x1a\nrest"),
        ("a.jpg", "image/jpeg", b"\xff\xd8\xff\xe0"),
        ("a.jpeg", "image/jpeg", b"\xff\xd8\xff\xe1"),
    ],
)
def test_every_supported_type_passes(name: str, mime: str, content: bytes) -> None:
    declared = validate_declared_upload(name, mime, len(content))
    validate_uploaded_file(declared, content[:16], content if name.endswith(("docx", "pptx")) else None)


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.pdf", "image/png"),
        ("a.png", "application/pdf"),
        ("a.docx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("a.exe", "application/octet-stream"),
        ("a.JPG", "image/jpeg"),
    ],
)
def test_extension_and_declared_mime_mismatches_are_rejected(name: str, mime: str) -> None:
    with pytest.raises(AppError) as caught:
        validate_declared_upload(name, mime, 1)
    assert caught.value.code == "unsupported_file_type"


@pytest.mark.parametrize(
    ("name", "mime", "head"),
    [
        ("a.pdf", "application/pdf", b"not-pdf"),
        ("a.png", "image/png", b"not-png"),
        ("a.jpg", "image/jpeg", b"not-jpeg"),
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"not-zip",
        ),
        (
            "a.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            b"not-zip",
        ),
    ],
)
def test_all_bad_magic_is_rejected(name: str, mime: str, head: bytes) -> None:
    declared = validate_declared_upload(name, mime, len(head))
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, head, head)
    assert caught.value.code == "invalid_file_content"


def test_exactly_100_mib_is_allowed_and_one_more_is_rejected() -> None:
    assert validate_declared_upload("a.pdf", "application/pdf", MAX_FILE_BYTES).size_bytes == MAX_FILE_BYTES
    with pytest.raises(AppError) as caught:
        validate_declared_upload("a.pdf", "application/pdf", MAX_FILE_BYTES + 1)
    assert caught.value.code == "file_too_large"


def test_zero_size_is_rejected() -> None:
    with pytest.raises(AppError) as caught:
        validate_declared_upload("a.pdf", "application/pdf", 0)
    assert caught.value.code == "empty_file"


@pytest.mark.parametrize(
    "raw",
    [
        ".hidden.pdf",
        "report.final.pdf",
        "report.pdf.exe",
        "file",
        "..",
        "bad?.pdf",
    ],
)
def test_hidden_ambiguous_empty_and_disallowed_names_are_rejected(raw: str) -> None:
    with pytest.raises(AppError) as caught:
        sanitize_file_name(raw)
    assert caught.value.code == "invalid_file_name"


@pytest.mark.parametrize(
    "raw",
    [
        "../../invoice.pdf",
        "..\\..\\invoice.pdf",
        "directory/file.pdf",
        "/absolute/invoice.pdf",
        "C:\\absolute\\invoice.pdf",
        "\\\\server\\share\\invoice.pdf",
        "directory\\nested/invoice.pdf",
    ],
)
def test_path_components_are_rejected_instead_of_reduced_to_basename(raw: str) -> None:
    with pytest.raises(AppError) as caught:
        sanitize_file_name(raw)
    assert caught.value.code == "invalid_file_name"
    assert caught.value.status_code == 400
    assert caught.value.retryable is False


@pytest.mark.parametrize("control", ["\x00", "\n", "\u200b", "\u200d", "\ufeff"])
@pytest.mark.parametrize("raw_template", ["{}report.pdf", "re{}port.pdf", "report{}.pdf"])
def test_control_characters_are_rejected_instead_of_removed(
    control: str, raw_template: str
) -> None:
    with pytest.raises(AppError) as caught:
        sanitize_file_name(raw_template.format(control))
    assert caught.value.code == "invalid_file_name"


def test_name_is_normalized_to_nfc() -> None:
    decomposed = "Cafe\u0301.pdf"
    assert sanitize_file_name(decomposed) == normalize("NFC", decomposed)


def test_120_char_name_is_allowed_and_121_is_rejected() -> None:
    allowed = f"{'a' * 116}.pdf"
    assert len(allowed) == 120
    assert sanitize_file_name(allowed) == allowed
    with pytest.raises(AppError) as caught:
        sanitize_file_name(f"{'a' * 117}.pdf")
    assert caught.value.code == "invalid_file_name"


@pytest.mark.parametrize(
    ("name", "mime", "package"),
    [
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            office_package(("[Content_Types].xml", b"types")),
        ),
        (
            "a.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            office_package(("[Content_Types].xml", b"types")),
        ),
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            office_package(("[Content_Types].xml", b"types"), ("../word/document.xml", b"doc")),
        ),
    ],
)
def test_office_packages_require_exact_safe_entries(name: str, mime: str, package: bytes) -> None:
    declared = validate_declared_upload(name, mime, len(package))
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, package[:16], package)
    assert caught.value.code == "invalid_office_package"


def test_office_zip_bomb_is_rejected() -> None:
    bomb = office_package(
        ("[Content_Types].xml", b"types"),
        ("word/document.xml", b"0" * (11 * 1024 * 1024)),
        compression=ZIP_DEFLATED,
    )
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(bomb)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, bomb[:16], bomb)
    assert caught.value.code == "invalid_office_package"


def test_legitimate_large_stored_office_package_is_allowed() -> None:
    package = office_package(
        ("[Content_Types].xml", b"types"),
        ("word/document.xml", b"x" * (11 * 1024 * 1024)),
    )
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(package)
    )
    validate_uploaded_file(declared, package[:16], package)


def test_encrypted_office_entry_is_rejected() -> None:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name in ("[Content_Types].xml", "word/document.xml"):
            info = ZipInfo(name)
            info.flag_bits |= 0x1
            archive.writestr(info, b"x")
    package = bytearray(stream.getvalue())
    # Python's writer clears encryption, so set the central-directory encryption flag directly.
    marker = b"PK\x01\x02"
    offset = package.find(marker)
    while offset >= 0:
        package[offset + 8 : offset + 10] = (1).to_bytes(2, "little")
        offset = package.find(marker, offset + 4)
    raw = bytes(package)
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(raw)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, raw[:16], raw)
    assert caught.value.code == "invalid_office_package"


def test_office_package_with_too_many_entries_is_rejected() -> None:
    entries = [("[Content_Types].xml", b"types"), ("word/document.xml", b"doc")]
    entries.extend((f"custom/item-{index}.xml", b"x") for index in range(255))
    package = office_package(*entries)
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(package)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, package[:16], package)
    assert caught.value.code == "invalid_office_package"


def test_office_package_with_absolute_path_is_rejected() -> None:
    package = office_package(
        ("[Content_Types].xml", b"types"),
        ("word/document.xml", b"doc"),
        ("/absolute.xml", b"bad"),
    )
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(package)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, package[:16], package)
    assert caught.value.code == "invalid_office_package"


@pytest.mark.parametrize(
    "entry_name",
    ["word\\..\\evil.xml", "C:/word/document.xml", "word/document.xml\x00.xml", ""],
)
def test_office_package_rejects_ambiguous_or_unsafe_entry_names(entry_name: str) -> None:
    with pytest.warns(UserWarning) if "\x00" in entry_name else nullcontext():
        package = office_package(
            ("[Content_Types].xml", b"types"),
            ("word/document.xml", b"doc"),
            (entry_name, b"bad"),
        )
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(package)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, package[:16], package)
    assert caught.value.code == "invalid_office_package"


def test_office_package_rejects_duplicate_normalized_entry_names() -> None:
    with pytest.warns(UserWarning):
        package = office_package(
            ("[Content_Types].xml", b"types"),
            ("word/document.xml", b"doc"),
            ("word\\document.xml", b"ambiguous"),
        )
    declared = validate_declared_upload(
        "a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", len(package)
    )
    with pytest.raises(AppError) as caught:
        validate_uploaded_file(declared, package[:16], package)
    assert caught.value.code == "invalid_office_package"