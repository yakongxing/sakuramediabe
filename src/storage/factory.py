from functools import lru_cache
from pathlib import Path

import httpx

from src.config.config import settings

from .local import LocalStorageBackend
from .subtitles import LocalSubtitleStorage
from .webdav import WebDAVStorageBackend


@lru_cache(maxsize=2)
def storage_for(namespace: str):
    if namespace not in {"assets", "clips"}: raise ValueError("unsupported storage namespace")
    if settings.storage.backend == "local":
        root = settings.media.import_image_root_path if namespace == "assets" else settings.media.media_clip_root_path
        return LocalStorageBackend(Path(root))
    timeout = httpx.Timeout(connect=settings.storage.connect_timeout_seconds, read=settings.storage.read_timeout_seconds, write=settings.storage.write_timeout_seconds, pool=settings.storage.pool_timeout_seconds)
    return WebDAVStorageBackend(settings.storage.webdav_base_url, namespace, username=settings.storage.username, password=settings.storage.password, root_prefix=settings.storage.root_prefix, verify_tls=settings.storage.verify_tls, timeout=timeout, final_visibility_retry_delays=settings.storage.webdav_final_visibility_retry_seconds, publication_concurrency_limit=settings.storage.webdav_publication_max_workers, temp_cleanup_interval_seconds=settings.storage.webdav_temp_cleanup_interval_seconds, temp_cleanup_age_seconds=settings.storage.webdav_temp_cleanup_age_seconds, temp_cleanup_max_deletes=settings.storage.webdav_temp_cleanup_max_deletes, upload_chunk_size=settings.storage.upload_chunk_size)

def asset_storage(): return storage_for("assets")
def subtitle_storage():
    if settings.storage.subtitles_backend == "local" and settings.storage.backend != "local":
        return LocalSubtitleStorage(Path(settings.media.import_image_root_path), asset_storage())
    return asset_storage()
def clip_storage(): return storage_for("clips")
def reset_storage_backends(): storage_for.cache_clear()
