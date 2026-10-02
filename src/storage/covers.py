"""Locally delivered covers stay local even when thumbnails use WebDAV."""

from pathlib import Path, PurePosixPath

from src.config import settings

from .keys import normalize_storage_key
from .local import LocalStorageBackend

LOCAL_COVER_PREFIX = "local-covers/"


def is_local_cover_key(key: str) -> bool:
    normalized = normalize_storage_key(key)
    if normalized.startswith(LOCAL_COVER_PREFIX):
        return True
    # Older first-frame covers were also generated locally, before the namespace.
    parts = PurePosixPath(normalized).parts
    return len(parts) == 4 and parts[0] == "videos" and parts[1].isdigit() and parts[2:] == ("cover", "0.webp")


def local_cover_storage() -> LocalStorageBackend:
    return LocalStorageBackend(Path(settings.media.import_image_root_path))
