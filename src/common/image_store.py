"""媒体图片字节的统一读取入口：缩略图包优先、单文件兜底。

缩略图包与缩略图目录同级同名（``thumbnails.zip``）。包一旦存在即视为该 media
缩略图的主要存储；包内条目缺失时回退同名单文件，两者都没有时按"文件缺失"
处理（``FileNotFoundError``），与旧版调用方的异常语义保持一致。
"""

from __future__ import annotations

import os
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


def read_image_bytes(relative_path: str, *, storage=None) -> bytes:
    """Read local ZIP entries or loose assets; WebDAV always reads individual keys."""
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
