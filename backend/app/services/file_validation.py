import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from re import fullmatch
from zipfile import BadZipFile, ZipFile

from app.core.errors import AppError

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FILE_NAME_LENGTH = 120
MAX_OFFICE_ENTRIES = 256
MAX_OFFICE_UNCOMPRESSED_BYTES = MAX_FILE_BYTES
MAX_COMPRESSION_RATIO = 100

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"

TYPE_MAP: dict[str, tuple[str, bytes, str | None]] = {
    ".pdf": (PDF_MIME, b"%PDF-", None),
    ".docx": (DOCX_MIME, b"PK\x03\x04", "word/document.xml"),
    ".pptx": (PPTX_MIME, b"PK\x03\x04", "ppt/presentation.xml"),
    ".png": (PNG_MIME, b"\x89PNG\r\n\x1a\n", None),
    ".jpg": (JPEG_MIME, b"\xff\xd8\xff", None),
    ".jpeg": (JPEG_MIME, b"\xff\xd8\xff", None),
}


@dataclass(frozen=True, slots=True)
class DeclaredUpload:
    file_name: str
    content_type: str
    size_bytes: int
    extension: str

    @property
    def is_office(self) -> bool:
        return TYPE_MAP[self.extension][2] is not None


def _invalid_name() -> AppError:
    return AppError("invalid_file_name", 400, "The file name is invalid.", False)


def sanitize_file_name(raw_name: str) -> str:
    if "/" in raw_name or "\\" in raw_name:
        raise _invalid_name()
    without_controls = "".join(
        character
        for character in raw_name
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    normalized = unicodedata.normalize("NFC", without_controls)
    name = PurePosixPath(normalized.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or len(name) > MAX_FILE_NAME_LENGTH:
        raise _invalid_name()
    if name.startswith(".") or name.count(".") != 1:
        raise _invalid_name()
    stem, extension = name.rsplit(".", 1)
    if not stem or not extension:
        raise _invalid_name()
    if any(
        not (
            character.isalpha()
            or character.isnumeric()
            or character in " .-_()"
        )
        for character in name
    ):
        raise _invalid_name()
    return name


def validate_declared_upload(
    file_name: str,
    content_type: str,
    size_bytes: int,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> DeclaredUpload:
    sanitized = sanitize_file_name(file_name)
    if size_bytes <= 0:
        raise AppError("empty_file", 400, "Files must not be empty.", False)
    if size_bytes > max_file_bytes:
        raise AppError("file_too_large", 413, "Files must be 100 MB or smaller.", False)
    extension = f".{sanitized.rsplit('.', 1)[1]}"
    expected = TYPE_MAP.get(extension)
    if expected is None or content_type != expected[0]:
        raise AppError(
            "unsupported_file_type",
            400,
            "The file extension and content type must match a supported type.",
            False,
        )
    return DeclaredUpload(sanitized, content_type, size_bytes, extension)


def validate_uploaded_file(
    declared: DeclaredUpload,
    header: bytes,
    office_package: bytes | None = None,
) -> None:
    _, signature, required_office_entry = TYPE_MAP[declared.extension]
    if not header.startswith(signature):
        raise AppError("invalid_file_content", 400, "The uploaded file signature is invalid.", False)
    if required_office_entry is not None:
        _validate_office_package(office_package, required_office_entry)


def _validate_office_package(package: bytes | None, required_entry: str) -> None:
    if package is None or len(package) > MAX_FILE_BYTES:
        raise _invalid_office_package()
    try:
        with ZipFile(BytesIO(package)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_OFFICE_ENTRIES:
                raise _invalid_office_package()
            names: set[str] = set()
            total_uncompressed = 0
            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise _invalid_office_package()
                normalized = entry.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if (
                    not normalized
                    or path.is_absolute()
                    or ".." in path.parts
                    or normalized.startswith("/")
                    or fullmatch(r"[A-Za-z]:/.*", normalized) is not None
                    or any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized)
                    or normalized in names
                ):
                    raise _invalid_office_package()
                total_uncompressed += entry.file_size
                if total_uncompressed > MAX_OFFICE_UNCOMPRESSED_BYTES:
                    raise _invalid_office_package()
                if entry.compress_size == 0:
                    if entry.file_size > 0:
                        raise _invalid_office_package()
                elif entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO:
                    raise _invalid_office_package()
                names.add(normalized)
            if "[Content_Types].xml" not in names or required_entry not in names:
                raise _invalid_office_package()
    except (BadZipFile, OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AppError):
            raise
        raise _invalid_office_package() from None


def _invalid_office_package() -> AppError:
    return AppError(
        "invalid_office_package", 400, "The Office document package is invalid.", False
    )