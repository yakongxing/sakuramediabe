from functools import lru_cache
from pathlib import Path

import httpx

from src.config.config import settings
from .local import LocalStorageBackend
from .webdav import WebDAVStorageBackend


@lru_cache(maxsize=2)
def storage_for(namespace: str):
    if namespace not in {"assets", "clips"}: raise ValueError("unsupported storage namespace")
    if settings.storage.backend == "local":
        root = settings.media.import_image_root_path if namespace == "assets" else settings.media.media_clip_root_path
        return LocalStorageBackend(Path(root))
    timeout = httpx.Timeout(connect=settings.storage.connect_timeout_seconds, read=settings.storage.read_timeout_seconds, write=settings.storage.write_timeout_seconds, pool=settings.storage.pool_timeout_seconds)
    return WebDAVStorageBackend(settings.storage.webdav_base_url, namespace, username=settings.storage.username, password=settings.storage.password, root_prefix=settings.storage.root_prefix, verify_tls=settings.storage.verify_tls, timeout=timeout)

def asset_storage(): return storage_for("assets")
def clip_storage(): return storage_for("clips")
def reset_storage_backends(): storage_for.cache_clear()
