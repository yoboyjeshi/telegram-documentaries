"""Chat-namespaced temporary media paths and purge utilities."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def _default_media_root() -> Path:
    return Path(tempfile.gettempdir()) / "telegram_documents_media"


MEDIA_ROOT = _default_media_root()


def chat_media_dir(chat_id: int, root: Path | None = None) -> Path:
    root = root or MEDIA_ROOT
    return root / str(chat_id)


def photo_dest(chat_id: int, root: Path | None = None) -> Path:
    return chat_media_dir(chat_id, root=root) / "photo.jpg"


def avatar_dest(chat_id: int, root: Path | None = None, suffix: str = ".png") -> Path:
    if suffix not in (".png", ".jpg"):
        raise ValueError("Unsupported avatar extension")
    return chat_media_dir(chat_id, root=root) / f"avatar{suffix}"


def purge_chat_media(chat_id: int, root: Path | None = None) -> None:
    chat_dir = chat_media_dir(chat_id, root=root)
    if chat_dir.exists():
        shutil.rmtree(chat_dir)
