import atexit
from pathlib import Path
from threading import RLock

import httpx

from src.config.config import settings

from .local import LocalStorageBackend
from .subtitles import LocalSubtitleStorage
from .webdav import WebDAVStorageBackend

_backend_lock = RLock()
_backends = {}


def storage_for(namespace: str):
    if namespace not in {"assets", "clips"}:
        raise ValueError("unsupported storage namespace")
    # lru_cache permits two concurrent cache misses to construct separate pools.
    with _backend_lock:
        if namespace in _backends:
            return _backends[namespace]
        if settings.storage.backend == "local":
            root = settings.media.import_image_root_path if namespace == "assets" else settings.media.media_clip_root_path
            backend = LocalStorageBackend(Path(root))
        else:
            config = settings.storage
            timeout = httpx.Timeout(connect=config.connect_timeout_seconds, read=config.read_timeout_seconds, write=config.write_timeout_seconds, pool=config.pool_timeout_seconds)
            backend = WebDAVStorageBackend(
                config.webdav_base_url, namespace,
                username=config.username, password=config.password,
                root_prefix=config.root_prefix, verify_tls=config.verify_tls,
                timeout=timeout, retry_delays=config.webdav_upload_retry_seconds,
                final_visibility_retry_delays=config.webdav_final_visibility_retry_seconds,
                publication_concurrency_limit=config.webdav_publication_max_workers,
                publication_timeout_seconds=config.webdav_publication_timeout_seconds,
                temp_cleanup_interval_seconds=config.webdav_temp_cleanup_interval_seconds,
                temp_cleanup_age_seconds=config.webdav_temp_cleanup_age_seconds,
                temp_cleanup_max_deletes=config.webdav_temp_cleanup_max_deletes,
                upload_chunk_size=config.upload_chunk_size,
                download_chunk_size=config.download_chunk_size,
            )
        _backends[namespace] = backend
        return backend


def asset_storage():
    return storage_for("assets")


def subtitle_storage():
    if settings.storage.subtitles_backend == "local" and settings.storage.backend != "local":
        return LocalSubtitleStorage(Path(settings.media.import_image_root_path), asset_storage())
    return asset_storage()


def clip_storage():
    return storage_for("clips")


def reset_storage_backends():
    with _backend_lock:
        backends = list(_backends.values())
        _backends.clear()
    for backend in backends:
        close = getattr(backend, "close", None)
        if close is not None:
            close()


atexit.register(reset_storage_backends)
