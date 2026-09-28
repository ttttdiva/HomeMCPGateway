from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from home_mcp_gateway import image_library


class ImageLibraryToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "82_Infinite-Image-Browsing"
        self.repo.mkdir()
        self.db = self.repo / "iib.db"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self):
        return patch.dict(os.environ, {
            "HOME_MCP_IMAGE_LIBRARY_ROOT": str(self.repo),
            "HOME_MCP_IMAGE_LIBRARY_DB": str(self.db),
            "HOME_MCP_IMAGE_LIBRARY_DOWNLOAD_DIRS": str(self.downloads),
        }, clear=False)

    def make_image(self, name: str, age: float = 0) -> Path:
        path = self.downloads / name
        Image.new("RGB", (8, 8), "white").save(path)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_default_download_search_uses_only_user_downloads(self):
        profile = self.root / "profile"
        with patch.dict(os.environ, {"USERPROFILE": str(profile)}, clear=True):
            with patch.object(image_library, "GATEWAY_ROOT", self.root):
                _, _, search_dirs = image_library._settings()
        self.assertEqual(search_dirs, [profile / "Downloads"])

    def test_explicit_path_saves_context_and_does_not_modify_image(self):
        path = self.make_image("accepted.png")
        before = path.read_bytes()
        with self.env():
            result = image_library.image_library_register_context(
                instruction="kimono at dusk", source="ChatGPT",
                character_name="meika hime", context_summary="夕景版を採用",
                search_terms=["kimono", "meika hime"], image_path=str(path))
        self.assertTrue(result["saved"])
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT instruction,character_name,context_summary,search_terms,source FROM media_library_chat_context WHERE path=?",
                               (str(path.resolve()),)).fetchone()
        self.assertEqual(row[0], "kimono at dusk")
        self.assertEqual(row[1], "meika hime")
        self.assertEqual(row[2], "夕景版を採用")
        self.assertEqual(json.loads(row[3]), ["kimono", "meika hime"])
        self.assertEqual(row[4], "ChatGPT")

    def test_omitted_path_uses_unambiguous_latest_recent_download(self):
        self.make_image("old.png", age=60)
        latest = self.make_image("latest.png", age=1)
        with self.env():
            result = image_library.image_library_register_context(
                instruction="chosen", source="Gemini", search_terms=["chosen"])
        self.assertTrue(result["saved"])
        self.assertEqual(Path(result["image_path"]), latest.resolve())

    def test_omitted_path_rejects_ambiguous_recent_downloads(self):
        a = self.make_image("a.png", age=1)
        b = self.make_image("b.png", age=2)
        now = time.time()
        os.utime(a, (now, now)); os.utime(b, (now - 2, now - 2))
        with self.env():
            result = image_library.image_library_register_context(
                instruction="chosen", source="ChatGPT")
        self.assertFalse(result["saved"])
        self.assertIn("複数", result["message"])

    def test_same_path_is_idempotent_upsert(self):
        path = self.make_image("accepted.png")
        with self.env():
            first = image_library.image_library_register_context(
                instruction="first", source="ChatGPT", image_path=str(path))
            second = image_library.image_library_register_context(
                instruction="second", source="ChatGPT", image_path=str(path))
        self.assertTrue(first["saved"] and second["saved"])
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT instruction FROM media_library_chat_context").fetchall()
        self.assertEqual(rows, [("second",)])


if __name__ == "__main__":
    unittest.main()
