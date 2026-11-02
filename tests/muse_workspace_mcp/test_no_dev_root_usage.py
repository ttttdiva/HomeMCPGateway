"""Source-level guard: neither the package nor its tests may name or create the real dev root."""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "muse_workspace_mcp"
TESTS = Path(__file__).resolve().parent

# The one place that may spell the production development root.
ALLOWED_DEV_ROOT_FILES = {PACKAGE / "registry.py"}
# A quoted string literal that starts with the real dev root (docstrings and usage text may still mention it).
DEV_ROOT_LITERAL = re.compile(r"""(?i)['"]d:[\\/]+dev\b""")
TEMP_WITH_DIR = re.compile(r"(?:mkdtemp|mkstemp|TemporaryDirectory|NamedTemporaryFile)\s*\([^)]*\bdir\s*=")


def _sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_only_the_registry_spells_the_production_dev_root():
    offenders = []
    for path in _sources(PACKAGE) + _sources(TESTS):
        if path in ALLOWED_DEV_ROOT_FILES or path == Path(__file__).resolve():
            continue
        if DEV_ROOT_LITERAL.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []


def test_tests_never_pass_dir_to_temporary_file_helpers():
    offenders = []
    for path in _sources(TESTS):
        if path == Path(__file__).resolve():
            continue
        if TEMP_WITH_DIR.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []


def test_tests_never_run_subprocesses_with_the_dev_root_as_cwd():
    offenders = []
    for path in _sources(TESTS):
        if path == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"""cwd\s*=\s*r?['"][A-Za-z]:[\\/]+Dev['"]""", text):
            offenders.append(path.name)
    assert offenders == []
