from pathlib import Path

APPROVED_INDEX = "https://packagefeedproxy.microsoft.io/pypi/simple/"
FORBIDDEN_INDEX_MARKERS = (
    "pypi.org",
    "pythonhosted.org",
    "pypi.tuna.tsinghua.edu.cn",
)


def test_python_dependencies_use_only_the_enterprise_feed() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    pyproject = (backend_dir / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (backend_dir / "uv.lock").read_text(encoding="utf-8")

    assert APPROVED_INDEX in pyproject
    assert APPROVED_INDEX in lockfile

    combined = f"{pyproject}\n{lockfile}".lower()
    for marker in FORBIDDEN_INDEX_MARKERS:
        assert marker not in combined
