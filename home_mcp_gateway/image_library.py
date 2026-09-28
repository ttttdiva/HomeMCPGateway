"""One-shot bridge from chat context to Infinite Image Browsing metadata."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from typing import Any

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
MAX_RECENT_AGE_SECONDS = 30 * 60
AMBIGUOUS_WINDOW_SECONDS = 5


class ImageLibraryError(Exception):
    pass


def _option(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    env_path = GATEWAY_ROOT / ".env"
    if env_path.is_file():
        try:
            for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip() == name:
                    return val.strip()
        except (OSError, UnicodeError):
            pass
    return default


def _settings() -> tuple[Path, Path, list[Path]]:
    root = Path(_option("HOME_MCP_IMAGE_LIBRARY_ROOT", str(GATEWAY_ROOT.parent / "82_Infinite-Image-Browsing"))).expanduser()
    if not root.is_absolute():
        root = (GATEWAY_ROOT / root).resolve()
    db = Path(_option("HOME_MCP_IMAGE_LIBRARY_DB", str(root / "iib.db"))).expanduser()
    if not db.is_absolute():
        db = (root / db).resolve()
    configured = _option("HOME_MCP_IMAGE_LIBRARY_DOWNLOAD_DIRS", "")
    dirs = [Path(x).expanduser() for x in configured.split(";") if x.strip()]
    if not dirs:
        profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        dirs = [profile / "Downloads"]
    return root, db, dirs


def _clean(value: Any, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ImageLibraryError("文字列の入力形式が不正です。")
    value = value.strip()
    if required and not value:
        raise ImageLibraryError("必須項目が空です。")
    if len(value) > limit or any(ord(ch) < 9 for ch in value):
        raise ImageLibraryError("入力が長すぎるか不正な文字を含みます。")
    return value


def _resolve_image(image_path: str | None, search_dirs: list[Path]) -> Path:
    if image_path is not None:
        value = _clean(image_path, 4096, True)
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ImageLibraryError("image_path は絶対パスで指定してください。")
        path = path.resolve()
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ImageLibraryError("指定画像が見つからないか、対応画像形式ではありません。")
        return path

    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for folder in search_dirs:
        try:
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                try:
                    if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
                        stamp = entry.stat().st_mtime
                        if now - stamp <= MAX_RECENT_AGE_SECONDS:
                            candidates.append((stamp, entry.resolve()))
                except OSError:
                    continue
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        raise ImageLibraryError("直近30分のダウンロード画像を見つけられません。image_path を指定してください。")
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] <= AMBIGUOUS_WINDOW_SECONDS:
        raise ImageLibraryError("直近画像が複数あり対象を一意に決められません。image_path を指定してください。")
    return candidates[0][1]


def image_library_register_context(
    instruction: str,
    source: str,
    character_name: str = "",
    context_summary: str = "",
    search_terms: list[str] | None = None,
    image_path: str | None = None,
) -> dict[str, Any]:
    """Register context for one accepted/downloaded generated image.

    Call this only after the user explicitly asks to register an accepted image.
    It does not inspect chat history: provide the generation instruction, character
    name, short context summary, search terms, and source from the current chat.
    image_path may be omitted only when the newest recent download is unambiguous.
    This never modifies the image file itself and is idempotent for the same path.
    """
    try:
        instruction = _clean(instruction, 50000, True)
        source = _clean(source, 200, True)
        character_name = _clean(character_name, 1000)
        context_summary = _clean(context_summary, 10000)
        if search_terms is None:
            search_terms = []
        if not isinstance(search_terms, list) or len(search_terms) > 100:
            raise ImageLibraryError("search_terms の形式が不正です。")
        terms: list[str] = []
        for item in search_terms:
            item = _clean(item, 500)
            if item and item not in terms:
                terms.append(item)
        _, db_path, search_dirs = _settings()
        path = _resolve_image(image_path, search_dirs)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path, timeout=30)) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("""CREATE TABLE IF NOT EXISTS media_library_chat_context (
                path TEXT PRIMARY KEY,
                instruction TEXT NOT NULL,
                character_name TEXT NOT NULL DEFAULT '',
                context_summary TEXT NOT NULL DEFAULT '',
                search_terms TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS ml_chat_context_source ON media_library_chat_context(source)")
            conn.execute("""INSERT INTO media_library_chat_context
                (path,instruction,character_name,context_summary,search_terms,source,updated_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(path) DO UPDATE SET instruction=excluded.instruction,
                  character_name=excluded.character_name, context_summary=excluded.context_summary,
                  search_terms=excluded.search_terms, source=excluded.source,
                  updated_at=CURRENT_TIMESTAMP""",
                (str(path), instruction, character_name, context_summary,
                 json.dumps(terms, ensure_ascii=False), source))
            conn.commit()
        return {"ok": True, "saved": True, "image_path": str(path), "source": source,
                "search_terms": terms, "message": "採用画像の生成チャット文脈を画像管理DBへ保存しました。"}
    except (ImageLibraryError, OSError, sqlite3.Error) as exc:
        return {"ok": False, "saved": False, "error": "image_library_error", "message": str(exc)}


TOOLS = [image_library_register_context]
