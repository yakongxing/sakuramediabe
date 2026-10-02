"""Read packed thumbnails on either backend, with loose reads for older assets."""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from src.common.image_references import is_nonlocal_image_reference
from src.common.media_paths import image_pack_relative_path
from src.config import settings
from src.storage import asset_storage
from src.storage.keys import normalize_storage_key
from src.storage.types import StorageNotFound


def require_local_image_packs() -> None:
    if settings.storage.backend != "local":
        raise RuntimeError("image_pack_requires_local_storage")


def image_pack_path(relative_path: str, *, storage=None) -> Path | None:
    """Only local assets have packs; remote storage never consults the local cache."""
    relative_path = normalize_storage_key(relative_path)
    pack_relative = image_pack_relative_path(relative_path)
    if pack_relative is None:
        return None
    storage = storage if storage is not None else asset_storage()
    return getattr(storage, "local_path", lambda key: None)(pack_relative.as_posix())


def thumbnail_generation_pack_key(relative_path: str) -> str | None:
    path = PurePosixPath(normalize_storage_key(relative_path))
    if path.parent.parent.name == "thumbnails" and re.fullmatch(r"[0-9a-f]{32}", path.parent.name):
        return path.parent.with_suffix(".zip").as_posix()
    return None


def read_image_bytes(relative_path: str, *, storage=None) -> bytes:
    """Remote ZIP reads use the remote backend exclusively, never a local cache."""
    if is_nonlocal_image_reference(relative_path):
        raise ValueError("image_reference_nonlocal")
    relative_path = normalize_storage_key(relative_path)
    storage = storage if storage is not None else asset_storage()
    pack_path = image_pack_path(relative_path, storage=storage)
    if pack_path is not None and pack_path.is_file():
        try:
            with zipfile.ZipFile(pack_path) as archive:
                return archive.read(PurePosixPath(relative_path).name)
        except (KeyError, zipfile.BadZipFile):
            pass
    pack_key = thumbnail_generation_pack_key(relative_path)
    if pack_path is None and pack_key is not None:
        try:
            stream = storage.open(pack_key)
        except StorageNotFound:
            pass  # An older generation may still consist of loose images.
        else:
            with stream, zipfile.ZipFile(stream) as archive:
                try:
                    return archive.read(PurePosixPath(relative_path).name)
                except KeyError as exc:
                    raise FileNotFoundError(relative_path) from exc
    try:
        with storage.open(relative_path) as stream:
            return stream.read()
    except StorageNotFound as exc:
        raise FileNotFoundError(relative_path) from exc


def write_pack(pack_path: Path, entries: Iterable[tuple[str, Path | bytes]]) -> None:
    """写 ZIP_STORED 缩略图包并 fsync；调用方负责临时路径与原子替换。

    缩略图已是压缩格式，包只作容器不压缩；逐条目按给定顺序写入。
    """
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        pack_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for entry_name, source in entries:
            if isinstance(source, bytes):
                archive.writestr(entry_name, source)
            else:
                archive.write(source, arcname=entry_name)
    descriptor = os.open(pack_path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
