from __future__ import annotations

import hashlib
import io
import os
import random
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from loguru import logger
from webdav4.client import (
    BadGatewayError,
    Client,
    ForbiddenOperation,
    InsufficientStorage,
    ResourceAlreadyExists,
    ResourceConflict,
    ResourceLocked,
    ResourceNotFound,
)

from .keys import normalize_prefix, normalize_storage_key
from .types import (
    ObjectStat,
    StorageNotFound,
    StoragePublicationUnknown,
    StorageUnavailable,
)

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_publication_semaphores: dict[int, threading.BoundedSemaphore] = {}
_UPLOAD_TEMP_NAME = re.compile(r"^\..+\.uploading-[0-9a-f]{32}$")


def _lock_for_path(path: str) -> threading.Lock:
    with _locks_guard:
        return _path_locks[path]


def _publication_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Share the configured PUT bound across every backend instance/namespace."""
    with _locks_guard:
        return _publication_semaphores.setdefault(limit, threading.BoundedSemaphore(limit))


class WebDAVStorageBackend:
    supports_direct_immutable_put = True
    def __init__(self, base_url: str, namespace: str, *, username: str = "", password: str = "", root_prefix: str = "", verify_tls: bool = True, timeout: httpx.Timeout | float = 60.0, sleep=time.sleep, retry_delays: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0), final_visibility_retry_delays: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0), publication_concurrency_limit: int = 2, temp_cleanup_interval_seconds: float = 3600, temp_cleanup_max_deletes: int = 16, temp_cleanup_age_seconds: float = 86400, upload_chunk_size: int = 1024 * 1024):
        self.base_url = base_url.rstrip("/")
        prefix = normalize_prefix(root_prefix)
        self.prefix = "/".join(part for part in (prefix, normalize_storage_key(namespace)) if part)
        self.auth = (username, password) if username or password else None
        self.timeout = timeout
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._final_visibility_retry_delays = final_visibility_retry_delays
        self.publication_concurrency_limit = publication_concurrency_limit
        self._publication_semaphore = _publication_semaphore(publication_concurrency_limit)
        self._temp_cleanup_interval_seconds = temp_cleanup_interval_seconds
        self._temp_cleanup_max_deletes = temp_cleanup_max_deletes
        self._temp_cleanup_age_seconds = temp_cleanup_age_seconds
        self._temp_cleanup_lock = threading.Lock()
        self._last_temp_cleanup_at = 0.0
        self._directory_cache: OrderedDict[str, float] = OrderedDict()
        self._directory_cache_lock = threading.Lock()
        self._uncertain_publications: OrderedDict[str, None] = OrderedDict()
        self._uncertain_lock = threading.Lock()
        self.client = Client(self.base_url, auth=self.auth, timeout=timeout, verify=verify_tls, retry=False, chunk_size=upload_chunk_size)
        self.http = httpx.Client(auth=self.auth, timeout=timeout, verify=verify_tls, follow_redirects=True)

    def _path(self, key: str) -> str: return f"{self.prefix}/{normalize_storage_key(key)}"
    def _url(self, key: str) -> str: return f"{self.base_url}/{quote(self._path(key), safe='/')}"

    @staticmethod
    def _size(info: dict) -> int:
        return int(info.get("size") or info.get("content_length") or 0)

    def _stat_once(self, key: str) -> ObjectStat:
        normalized = normalize_storage_key(key)
        try: info = self.client.info(self._path(normalized))
        except ResourceNotFound as exc:
            raise StorageNotFound(normalized) from exc
        except Exception as exc:
            if self._status_code(exc) == 404:
                raise StorageNotFound(normalized) from exc
            raise self._error("stat", exc) from exc
        return ObjectStat(normalized, self._size(info), info.get("type", "file") != "directory", info.get("etag"))

    def stat(self, key: str) -> ObjectStat:
        return self._retry_operation("stat", lambda: self._stat_once(key))

    def exists(self, key: str) -> bool:
        try: self.stat(key)
        except StorageNotFound: return False
        return True

    def _mkdir_parents(self, key: str) -> None:
        parts = self._path(key).split("/")[:-1]
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            with _lock_for_path(f"mkdir:{self.base_url}:{path}"):
                with self._directory_cache_lock:
                    if self._directory_cache.get(path, 0) > time.monotonic():
                        self._directory_cache.move_to_end(path)
                        continue
                try:
                    self._retry_operation("mkdir", lambda path=path: self.client.mkdir(path))
                except Exception as exc:
                    if self._status_code(exc) not in {405, 409, 412}:
                        raise
                    try:
                        info = self._retry_operation("mkdir_stat", lambda path=path: self.client.info(path))
                    except Exception as info_error:
                        if self._status_code(info_error) == 404:
                            raise StorageUnavailable(
                                "WebDAV directory disappeared during creation",
                                stage="mkdir", status_code=409,
                            ) from info_error
                        raise
                    if info.get("type") != "directory":
                        raise StorageUnavailable("WebDAV directory path is not a collection", stage="mkdir") from exc
                with self._directory_cache_lock:
                    self._directory_cache[path] = time.monotonic() + 600
                    self._directory_cache.move_to_end(path)
                    while len(self._directory_cache) > 4096:
                        self._directory_cache.popitem(last=False)

    def _restore_parents(self, key: str, repaired: set[str]) -> bool:
        if key in repaired:
            return False
        repaired.add(key)
        parent = self._path(key).rsplit("/", 1)[0]
        with self._directory_cache_lock:
            for path in list(self._directory_cache):
                if parent == path or parent.startswith(path + "/") or path.startswith(parent + "/"):
                    del self._directory_cache[path]
        self._mkdir_parents(key)
        return True

    def _ensure_parents(self, key: str, repaired: set[str]) -> None:
        try:
            self._mkdir_parents(key)
        except StorageUnavailable as exc:
            if self._status_code(exc) not in {404, 409} or not self._restore_parents(key, repaired):
                raise

    @staticmethod
    def _temporary_key(key: str) -> str:
        path = Path(normalize_storage_key(key))
        return str(path.with_name(f".{path.name}.uploading-{uuid.uuid4().hex}"))

    def _stat_visible(self, key: str, *, stage: str, retry_delays: tuple[float, ...] | None = None) -> ObjectStat:
        delays = self._retry_delays if retry_delays is None else retry_delays
        for attempt in range(len(delays) + 1):
            try:
                return self._stat_once(key)
            except (StorageNotFound, StorageUnavailable) as exc:
                if not (isinstance(exc, StorageNotFound) or self._is_transient(exc)) or attempt == len(delays):
                    status = self._status_code(exc)
                    if isinstance(exc, StorageNotFound):
                        status = 404
                    raise StorageUnavailable(
                        f"WebDAV {stage} visibility failed ({status or 'network'})",
                        stage=stage, status_code=status,
                        retryable=isinstance(exc, StorageNotFound) or self._is_transient(exc),
                    ) from exc
                self._pause(exc, delays[attempt], stage=stage, attempt=attempt + 1)

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        current: BaseException | None = exc
        while current is not None:
            status = getattr(current, "status_code", None) or getattr(getattr(current, "response", None), "status_code", None)
            if status is not None:
                return int(status)
            current = current.__cause__
        for error_type, status in (
            (ResourceNotFound, 404), (StorageNotFound, 404), (ResourceAlreadyExists, 412),
            (ResourceConflict, 409), (ForbiddenOperation, 403),
            (InsufficientStorage, 507), (ResourceLocked, 423), (BadGatewayError, 502),
        ):
            current = exc
            while current is not None:
                if isinstance(current, error_type):
                    return status
                current = current.__cause__
        return None

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        status = cls._status_code(exc)
        if status is not None:
            return status in {408, 423, 429, 500, 502, 503, 504, 509, 530}
        current = exc
        while current is not None:
            if isinstance(current, httpx.TransportError):
                return True
            current = current.__cause__
        return isinstance(exc, StorageUnavailable) and exc.retryable

    @classmethod
    def _error(cls, stage: str, exc: Exception) -> StorageUnavailable:
        status = cls._status_code(exc)
        return StorageUnavailable(
            f"WebDAV {stage} failed ({status or 'transport'})",
            stage=stage, status_code=status, retryable=cls._is_transient(exc),
        )

    def _pause(self, exc: Exception, delay: float, *, stage: str, attempt: int) -> None:
        wait = max(0, delay) * random.uniform(0.8, 1.2)
        if self._status_code(exc) in {429, 503}:
            current = exc
            while current is not None:
                headers = getattr(getattr(current, "response", None), "headers", {})
                value = headers.get("Retry-After")
                if value:
                    try:
                        seconds = float(value)
                    except ValueError:
                        try:
                            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            break
                    wait = max(wait, min(60, max(0, seconds)))
                    break
                current = current.__cause__
        wait = min(60, wait)
        logger.debug("WebDAV retry stage={} attempt={} status={} delay_seconds={:.3f}", stage, attempt, self._status_code(exc), wait)
        self._sleep(wait)

    def _retry_operation(self, stage: str, operation):
        for attempt in range(len(self._retry_delays) + 1):
            try:
                return operation()
            except StorageNotFound:
                raise
            except Exception as exc:
                if not self._is_transient(exc) or attempt == len(self._retry_delays):
                    raise self._error(stage, exc) from exc
                self._pause(exc, self._retry_delays[attempt], stage=stage, attempt=attempt + 1)

    def _destination_matches(
        self,
        key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        destination: ObjectStat | None = None,
    ) -> ObjectStat | None:
        destination = destination or self._stat_once(key)
        if not destination.is_file or destination.size != expected_size:
            return None
        try:
            # webdav4 writes incrementally into this disk-backed handle. Avoid
            # holding another full image in memory solely for verification.
            with tempfile.TemporaryFile() as stream:
                self._download_once(key, stream)
                stream.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except Exception as exc:
            raise self._error("verify_download", exc) from exc
        if digest.hexdigest() != expected_sha256:
            return None
        return destination

    def _move_with_retry(
        self,
        source: str,
        destination: str,
        *,
        destination_key: str,
        expected_size: int,
        expected_sha256: str,
        overwrite: bool,
        repaired: set[str],
    ) -> ObjectStat | None:
        for attempt in range(len(self._retry_delays) + 1):
            try:
                self.client.move(source, destination, overwrite=overwrite)
                return None
            except Exception as exc:
                status = self._status_code(exc)
                transient = self._is_transient(exc) or status in {404, 409}
                conflict = status in {409, 412}
                if transient or conflict:
                    known_mismatch = False
                    missing = False
                    try:
                        published = self._destination_matches(
                            destination_key,
                            expected_size=expected_size,
                            expected_sha256=expected_sha256,
                        )
                        known_mismatch = published is None
                    except StorageNotFound:
                        published = None
                        missing = True
                    except StorageUnavailable as verification_error:
                        published = None
                        if not (self._is_transient(verification_error) or self._status_code(verification_error) == 404):
                            raise StoragePublicationUnknown(
                                destination_key, "WebDAV move result cannot be verified",
                                stage="move_verify", status_code=self._status_code(verification_error),
                            ) from verification_error
                    if published is not None:
                        return published
                    if conflict and known_mismatch:
                        raise StorageUnavailable(
                            f"WebDAV publish move failed ({status}): destination content mismatch",
                            stage="move", status_code=status,
                        ) from exc
                    if status == 409 and missing:
                        self._restore_parents(destination_key, repaired)
                    # A conflict with an unreadable destination is not proof of a collision.
                    transient = True
                if not transient or attempt == len(self._retry_delays):
                    if transient:
                        raise StoragePublicationUnknown(
                            destination_key,
                            f"WebDAV publish move outcome unknown ({status or 'network'})",
                            stage="move", status_code=status, retryable=True,
                        ) from exc
                    raise self._error("publish move", exc) from exc
                self._pause(exc, self._retry_delays[attempt], stage="move", attempt=attempt + 1)

    def _delete_temporary_best_effort(self, key: str) -> None:
        try:
            self.delete(key, missing_ok=True)
        except Exception:
            logger.warning("WebDAV temporary cleanup failed key={}", key)

    def _upload_temporary(self, key: str, tmp_path: str, file_obj, size: int, repaired: set[str]) -> None:
        for attempt in range(len(self._retry_delays) + 1):
            try:
                file_obj.seek(0)
                self.client.upload_fileobj(file_obj, tmp_path, overwrite=True, size=size)
                return
            except Exception as exc:
                directory_error = self._status_code(exc) in {404, 409}
                if directory_error:
                    directory_error = self._restore_parents(key, repaired)
                if not (directory_error or self._is_transient(exc)) or attempt == len(self._retry_delays):
                    raise self._error("temporary upload", exc) from exc
                self._pause(exc, self._retry_delays[attempt], stage="temporary_put", attempt=attempt + 1)

    def _publish_fileobj(self, key: str, file_obj, *, size: int, expected_sha256: str, overwrite: bool) -> ObjectStat:
        normalized = normalize_storage_key(key)
        if not overwrite and self.exists(normalized): raise FileExistsError(normalized)
        final_path = self._path(normalized)
        tmp_key = self._temporary_key(normalized)
        tmp_path = self._path(tmp_key)
        with _lock_for_path(f"file:{self.base_url}:{final_path}"):
            repaired: set[str] = set()
            self._ensure_parents(normalized, repaired)
            try:
                self._upload_temporary(normalized, tmp_path, file_obj, size, repaired)
                tmp_stat = self._stat_visible(tmp_key, stage="temporary")
                if tmp_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV upload size mismatch key={normalized} expected={size} actual={tmp_stat.size}",
                        stage="temporary_verify",
                    )
                reconciled = self._move_with_retry(
                    tmp_path,
                    final_path,
                    destination_key=normalized,
                    expected_size=size,
                    expected_sha256=expected_sha256,
                    overwrite=overwrite,
                    repaired=repaired,
                )
                try:
                    final_stat = reconciled or self._stat_visible(
                        normalized,
                        stage="final",
                        retry_delays=self._final_visibility_retry_delays,
                    )
                except StorageUnavailable as exc:
                    raise StoragePublicationUnknown(
                        normalized, "WebDAV publish committed but final visibility is unknown",
                        stage="final", status_code=self._status_code(exc), retryable=exc.retryable,
                    ) from exc
                if final_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV final size mismatch key={normalized} expected={size} actual={final_stat.size}",
                        stage="final_verify", publication_possible=True,
                    )
                if reconciled is not None:
                    self._delete_temporary_best_effort(tmp_key)
                self._maybe_cleanup_expired_uploads(str(Path(normalized).parent))
                return final_stat
            except StoragePublicationUnknown:
                raise
            except Exception:
                self._delete_temporary_best_effort(tmp_key)
                raise

    def _publish_immutable_fileobj(self, key: str, file_obj, *, size: int, expected_sha256: str) -> ObjectStat:
        """Publish a content-addressed key without the metadata + temporary MOVE protocol."""
        normalized = normalize_storage_key(key)
        final_path = self._path(normalized)
        with _lock_for_path(f"file:{self.base_url}:{final_path}"):
            repaired: set[str] = set()
            self._ensure_parents(normalized, repaired)
            for attempt in range(len(self._retry_delays) + 1):
                try:
                    file_obj.seek(0)
                    self.client.upload_fileobj(
                        file_obj, final_path, overwrite=False, size=size
                    )
                except Exception as exc:
                    status = self._status_code(exc)
                    if not (self._is_transient(exc) or status in {404, 409, 412}):
                        raise self._error("direct upload", exc) from exc
                    known_mismatch = False
                    missing = False
                    try:
                        matching = self._destination_matches(
                            normalized,
                            expected_size=size,
                            expected_sha256=expected_sha256,
                        )
                        known_mismatch = matching is None
                    except StorageNotFound:
                        matching = None
                        missing = True
                    except StorageUnavailable as verification_error:
                        matching = None
                        if not (self._is_transient(verification_error) or self._status_code(verification_error) == 404):
                            raise StoragePublicationUnknown(
                                normalized, "WebDAV direct upload result cannot be verified",
                                stage="verify", status_code=self._status_code(verification_error),
                            ) from verification_error
                    if matching is not None:
                        return matching
                    if status in {409, 412} and known_mismatch:
                        raise StorageUnavailable(
                            f"WebDAV immutable key collision ({status}) key={normalized}",
                            stage="direct_put", status_code=status,
                        ) from exc
                    if status in {404, 409} and missing:
                        self._restore_parents(normalized, repaired)
                    if attempt == len(self._retry_delays):
                        raise StoragePublicationUnknown(
                            normalized, f"WebDAV direct upload outcome unknown ({status or 'network'})",
                            stage="direct_put", status_code=status, retryable=True,
                        ) from exc
                    self._pause(exc, self._retry_delays[attempt], stage="direct_put", attempt=attempt + 1)
                    continue
                return self._verify_published(normalized, size, expected_sha256)
        raise AssertionError("unreachable")

    def _verify_published(self, key: str, size: int, digest: str) -> ObjectStat:
        delays = self._final_visibility_retry_delays
        for attempt in range(len(delays) + 1):
            try:
                verified = self._destination_matches(key, expected_size=size, expected_sha256=digest)
            except (StorageNotFound, StorageUnavailable) as exc:
                transient = self._status_code(exc) == 404 or self._is_transient(exc)
                if not transient or attempt == len(delays):
                    raise StoragePublicationUnknown(
                        key, "WebDAV published object cannot be verified", stage="verify",
                        status_code=self._status_code(exc), retryable=transient,
                    ) from exc
                self._pause(exc, delays[attempt], stage="verify", attempt=attempt + 1)
                continue
            if verified is None:
                raise StorageUnavailable(f"WebDAV final content mismatch key={key}", stage="verify")
            return verified
        raise AssertionError("unreachable")

    def put_file(self, key: str, source: Path, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        if immutable and overwrite:
            raise ValueError("immutable publication requires overwrite=False")
        digest = hashlib.sha256()
        with source.open("rb") as handle, self._publication_semaphore:
            size = os.fstat(handle.fileno()).st_size
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            handle.seek(0)
            return self._publish(key, handle, size=size, expected_sha256=digest.hexdigest(), overwrite=overwrite, immutable=immutable)

    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        if immutable and overwrite:
            raise ValueError("immutable publication requires overwrite=False")
        with self._publication_semaphore:
            return self._publish(key, io.BytesIO(content), size=len(content), expected_sha256=hashlib.sha256(content).hexdigest(), overwrite=overwrite, immutable=immutable)

    def _publish(self, key: str, stream, *, size: int, expected_sha256: str, overwrite: bool, immutable: bool) -> ObjectStat:
        key = normalize_storage_key(key)
        started = time.monotonic()
        try:
            with self._uncertain_lock:
                uncertain = key in self._uncertain_publications
            if uncertain:
                try:
                    matching = self._destination_matches(key, expected_size=size, expected_sha256=expected_sha256)
                except StorageNotFound:
                    matching = None
                except StorageUnavailable as exc:
                    raise StoragePublicationUnknown(
                        key, "WebDAV previous publication cannot be verified",
                        stage="reconcile", status_code=self._status_code(exc), retryable=exc.retryable,
                    ) from exc
                if matching is not None:
                    with self._uncertain_lock:
                        self._uncertain_publications.pop(key, None)
                    return matching
            if immutable:
                result = self._publish_immutable_fileobj(key, stream, size=size, expected_sha256=expected_sha256)
            else:
                result = self._publish_fileobj(key, stream, size=size, expected_sha256=expected_sha256, overwrite=overwrite)
            with self._uncertain_lock:
                self._uncertain_publications.pop(key, None)
            logger.debug("WebDAV publication succeeded key={} bytes={} elapsed_seconds={:.3f}", key, size, time.monotonic() - started)
            return result
        except StoragePublicationUnknown:
            with self._uncertain_lock:
                self._uncertain_publications[key] = None
                self._uncertain_publications.move_to_end(key)
                while len(self._uncertain_publications) > 4096:
                    self._uncertain_publications.popitem(last=False)
            logger.warning("WebDAV publication unknown key={} bytes={} elapsed_seconds={:.3f}", key, size, time.monotonic() - started)
            raise
        except Exception as exc:
            logger.warning("WebDAV publication failed key={} stage={} status={} bytes={} elapsed_seconds={:.3f}", key, getattr(exc, "stage", None), self._status_code(exc), size, time.monotonic() - started)
            raise

    def _maybe_cleanup_expired_uploads(self, prefix: str) -> None:
        now = time.monotonic()
        if now - self._last_temp_cleanup_at < self._temp_cleanup_interval_seconds:
            return
        if not self._temp_cleanup_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if now - self._last_temp_cleanup_at < self._temp_cleanup_interval_seconds:
                return
            self._last_temp_cleanup_at = now
            try:
                self.cleanup_expired_uploads(
                    prefix,
                    older_than=datetime.now(timezone.utc) - timedelta(seconds=self._temp_cleanup_age_seconds),
                    max_deletes=self._temp_cleanup_max_deletes,
                )
            except Exception:
                return
        finally:
            self._temp_cleanup_lock.release()

    def _download_once(self, key: str, target) -> None:
        # webdav4.open() performs another PROPFIND and can resume GET indefinitely.
        # Keep retries at our operation boundary and close partial responses here.
        with self.http.stream("GET", self._url(key)) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                target.write(chunk)

    def open(self, key: str):
        def download():
            target = io.BytesIO()
            try:
                self._download_once(key, target)
            except Exception:
                target.close()
                raise
            target.seek(0)
            return target
        return self._retry_operation("download", download)

    def delete(self, key: str, *, missing_ok: bool = True) -> None:
        def remove():
            try:
                self.client.remove(self._path(key))
            except Exception as exc:
                if self._status_code(exc) == 404:
                    if missing_ok:
                        return
                    raise StorageNotFound(key) from exc
                raise
        self._retry_operation("delete", remove)

    def list(self, prefix: str) -> list[ObjectStat]:
        normalized = normalize_storage_key(prefix)
        try: rows = self._retry_operation("list", lambda: self.client.ls(self._path(normalized), detail=True))
        except ResourceNotFound:
            return []
        except Exception as exc:
            status = self._status_code(exc)
            if status == 404: return []
            raise StorageUnavailable("WebDAV list failed") from exc
        result = []
        for row in rows:
            if row.get("type") == "directory": continue
            name = str(row.get("name", "")).rstrip("/").rsplit("/", 1)[-1]
            if name: result.append(ObjectStat(f"{normalized}/{name}", self._size(row), True, row.get("etag")))
        return result

    @staticmethod
    def _modified_at(value) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError):
                    return None
        else:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    def cleanup_expired_uploads(self, prefix: str, *, older_than: datetime, max_deletes: int = 16) -> list[str]:
        """Best-effort removal of expired publication temps in one known directory."""
        normalized = normalize_storage_key(prefix)
        cutoff = older_than.replace(tzinfo=timezone.utc) if older_than.tzinfo is None else older_than.astimezone(timezone.utc)
        try:
            rows = self._retry_operation("cleanup_list", lambda: self.client.ls(self._path(normalized), detail=True))
        except ResourceNotFound:
            return []
        except Exception as exc:
            status = self._status_code(exc)
            if status == 404:
                return []
            raise StorageUnavailable(f"WebDAV temp cleanup list failed ({status or 'network'})") from exc
        deleted: list[str] = []
        for row in rows:
            if row.get("type") == "directory":
                continue
            name = str(row.get("name", "")).rstrip("/").rsplit("/", 1)[-1]
            modified = self._modified_at(row.get("modified") or row.get("last_modified"))
            if not _UPLOAD_TEMP_NAME.fullmatch(name) or modified is None or modified >= cutoff:
                continue
            key = f"{normalized}/{name}"
            try:
                self.delete(key, missing_ok=True)
            except StorageUnavailable:
                continue
            deleted.append(key)
            if len(deleted) >= max_deletes:
                break
        return deleted

    def local_path(self, key: str): return None

    def range_response(self, key: str, range_header: str | None, content_type: str):
        from fastapi.responses import StreamingResponse
        headers = {"Range": range_header} if range_header else {}
        request = self.http.build_request("GET", self._url(key), headers=headers)
        response = self.http.send(request, stream=True)
        if response.status_code == 404:
            response.close(); raise StorageNotFound(key)
        if response.status_code not in {200, 206, 416}:
            status = response.status_code; response.close(); raise StorageUnavailable(f"WebDAV GET failed ({status})")
        outgoing = {name: value for name, value in response.headers.items() if name.lower() in {"accept-ranges", "content-length", "content-range", "etag", "last-modified"}}
        if response.status_code == 416:
            body = response.read(); response.close()
            return StreamingResponse(iter([body]), status_code=416, headers=outgoing, media_type=content_type)
        def chunks():
            try: yield from response.iter_bytes()
            finally: response.close()
        return StreamingResponse(chunks(), status_code=response.status_code, headers=outgoing, media_type=response.headers.get("content-type", content_type))
