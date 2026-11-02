"""Prevent Gateway code from opening a direct Edge DevTools connection."""
from __future__ import annotations

import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROKER = (ROOT / "home_mcp_gateway" / "edge_cdp_broker.py").resolve()
SOURCE_SUFFIXES = {
    ".py", ".pyw", ".ps1", ".psm1", ".psd1", ".cmd", ".bat", ".sh",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".toml",
    ".yaml", ".yml",
}
EXCLUDED_DIRS = {
    ".git", ".venv", ".runtime", ".local", "logs", "repo.local",
    "tests", "test", "fixtures", "fixture", "node_modules", "dist", "build",
    "__pycache__",
}
FORBIDDEN_DIRECT_EDGE_MARKERS = {
    "DevToolsActivePort": re.compile(r"DevToolsActivePort", re.IGNORECASE),
    "Playwright connect_over_cdp": re.compile(r"\bconnect_over_cdp\b", re.IGNORECASE),
    "--auto-connect": re.compile(r"--auto-connect", re.IGNORECASE),
    "AGENT_BROWSER_AUTO_CONNECT": re.compile(r"AGENT_BROWSER_AUTO_CONNECT", re.IGNORECASE),
    "DevTools JSON discovery endpoints": re.compile(r"/json/(?:version|list)\b", re.IGNORECASE),
    "default Edge DevTools port": re.compile(r"(?:127\.0\.0\.1|localhost)\s*:\s*9222", re.IGNORECASE),
}


def _production_source_files() -> list[Path]:
    files = []
    for directory, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if name.lower() not in EXCLUDED_DIRS]
        parent = Path(directory)
        for filename in filenames:
            path = parent / filename
            if path.suffix.lower() not in SOURCE_SUFFIXES or path.name.lower().startswith("test_"):
                continue
            files.append(path)
    return files


class DirectEdgeConnectGuardTests(unittest.TestCase):
    def test_existing_edge_devtools_access_is_confined_to_aoi_talk(self):
        self.assertTrue(BROKER.is_file())

        violations: list[str] = []
        for path in _production_source_files():
            source = path.read_text(encoding="utf-8", errors="replace")
            relative = path.relative_to(ROOT).as_posix()
            for name, pattern in FORBIDDEN_DIRECT_EDGE_MARKERS.items():
                if pattern.search(source):
                    violations.append(f"{relative}: {name}")

        self.assertEqual(violations, [], "Direct Edge DevTools access is AoiTalk-only:\n" + "\n".join(violations))
