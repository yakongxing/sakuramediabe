from __future__ import annotations

import hashlib
import io
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from webdav4.client import Client, ResourceAlreadyExists, ResourceNotFound

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
    def __init__(self, base_url: str, namespace: str, *, username: str = "", password: str = "", root_prefix: str = "", verify_tls: bool = True, timeout: httpx.Timeout | float = 60.0, sleep=time.sleep, retry_delays: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0), final_visibility_retry_delays: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0), publication_concurrency_limit: int = 2, temp_cleanup_interval_seconds: float = 3600, temp_cleanup_max_deletes: int = 16, temp_cleanup_age_seconds: float = 86400):
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
        self.client = Client(self.base_url, auth=self.auth, timeout=timeout, verify=verify_tls)
        self.http = httpx.Client(auth=self.auth, timeout=timeout, verify=verify_tls, follow_redirects=True)

    def _path(self, key: str) -> str: return f"{self.prefix}/{normalize_storage_key(key)}"
    def _url(self, key: str) -> str: return f"{self.base_url}/{quote(self._path(key), safe='/')}"

    @staticmethod
    def _size(info: dict) -> int:
        return int(info.get("size") or info.get("content_length") or 0)

    def stat(self, key: str) -> ObjectStat:
        normalized = normalize_storage_key(key)
        try: info = self.client.info(self._path(normalized))
        except ResourceNotFound as exc:
            raise StorageNotFound(normalized) from exc
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise StorageUnavailable(f"WebDAV stat failed ({status or 'network'})") from exc
        return ObjectStat(normalized, self._size(info), info.get("type", "file") != "directory", info.get("etag"))

    def exists(self, key: str) -> bool:
        try: self.stat(key)
        except StorageNotFound: return False
        return True

    def _mkdir_parents(self, key: str) -> None:
        parts = self._path(key).split("/")[:-1]
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            with _lock_for_path(f"mkdir:{path}"):
                try: self.client.mkdir(path)
                except ResourceAlreadyExists:
                    continue
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in {405, 409}: # some servers report existing collections as 405
                        continue
                    try:
                        if self.client.exists(path): continue
                    except Exception:
                        pass
                    raise StorageUnavailable("WebDAV mkdir failed") from exc

    @staticmethod
    def _temporary_key(key: str) -> str:
        path = Path(normalize_storage_key(key))
        return str(path.with_name(f".{path.name}.uploading-{uuid.uuid4().hex}"))

    def _stat_visible(self, key: str, *, stage: str, retry_delays: tuple[float, ...] | None = None) -> ObjectStat:
        delays = self._retry_delays if retry_delays is None else retry_delays
        for attempt in range(len(delays) + 1):
            try:
                return self.stat(key)
            except (StorageNotFound, StorageUnavailable) as exc:
                if not self._is_transient(exc) or attempt == len(delays):
                    status = self._status_code(exc)
                    if isinstance(exc, StorageNotFound):
                        status = 404
                    raise StorageUnavailable(
                        f"WebDAV {stage} visibility failed ({status or 'network'})"
                    ) from exc
                self._sleep(delays[attempt])

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        current: BaseException | None = exc
        while current is not None:
            status = getattr(getattr(current, "response", None), "status_code", None)
            if status is not None:
                return int(status)
            current = current.__cause__
        return None

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        status = cls._status_code(exc)
        return status is None or status in {404, 409, 423, 429, 530} or (500 <= status <= 599)

    def _destination_matches(
        self,
        key: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> ObjectStat | None:
        try:
            destination = self.stat(key)
        except (StorageNotFound, StorageUnavailable):
            return None
        if destination.size != expected_size:
            return None
        try:
            # webdav4 writes incrementally into this disk-backed handle. Avoid
            # holding another full image in memory solely for verification.
            with tempfile.TemporaryFile() as stream:
                self.client.download_fileobj(self._path(key), stream)
                stream.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except StorageUnavailable:
            return None
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
    ) -> ObjectStat | None:
        for attempt in range(len(self._retry_delays) + 1):
            try:
                self.client.move(source, destination, overwrite=overwrite)
                return None
            except Exception as exc:
                status = self._status_code(exc)
                transient = self._is_transient(exc)
                conflict = status in {409, 412}
                if transient or conflict:
                    published = self._destination_matches(
                        destination_key,
                        expected_size=expected_size,
                        expected_sha256=expected_sha256,
                    )
                    if published is not None:
                        return published
                if conflict:
                    raise StorageUnavailable(
                        f"WebDAV publish move failed ({status}): destination content mismatch"
                    ) from exc
                if not transient or attempt == len(self._retry_delays):
                    if transient:
                        raise StoragePublicationUnknown(
                            destination_key,
                            f"WebDAV publish move outcome unknown ({status or 'network'})",
                        ) from exc
                    raise StorageUnavailable(f"WebDAV publish move failed ({status or 'network'})") from exc
                self._sleep(self._retry_delays[attempt])

    def _delete_temporary_best_effort(self, key: str) -> None:
        for attempt in range(len(self._retry_delays) + 1):
            try:
                self.delete(key, missing_ok=True)
                return
            except StorageUnavailable as exc:
                if not self._is_transient(exc) or attempt == len(self._retry_delays):
                    return
                self._sleep(self._retry_delays[attempt])

    def _publish_fileobj(self, key: str, file_obj, *, size: int, expected_sha256: str, overwrite: bool) -> ObjectStat:
        normalized = normalize_storage_key(key)
        if not overwrite and self.exists(normalized): raise FileExistsError(normalized)
        final_path = self._path(normalized)
        tmp_key = self._temporary_key(normalized)
        tmp_path = self._path(tmp_key)
        with _lock_for_path(f"file:{final_path}"):
            self._mkdir_parents(normalized)
            try:
                try:
                    self.client.upload_fileobj(file_obj, tmp_path, overwrite=True, size=size)
                except Exception as exc:
                    status = self._status_code(exc)
                    raise StorageUnavailable(
                        f"WebDAV temporary upload failed ({status or 'network'})"
                    ) from exc
                tmp_stat = self._stat_visible(tmp_key, stage="temporary")
                if tmp_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV upload size mismatch key={normalized} expected={size} actual={tmp_stat.size}"
                    )
                reconciled = self._move_with_retry(
                    tmp_path,
                    final_path,
                    destination_key=normalized,
                    expected_size=size,
                    expected_sha256=expected_sha256,
                    overwrite=overwrite,
                )
                try:
                    final_stat = reconciled or self._stat_visible(
                        normalized,
                        stage="final",
                        retry_delays=self._final_visibility_retry_delays,
                    )
                except StorageUnavailable as exc:
                    raise StoragePublicationUnknown(
                        normalized, "WebDAV publish committed but final visibility is unknown"
                    ) from exc
                if final_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV final size mismatch key={normalized} expected={size} actual={final_stat.size}"
                    )
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
        with _lock_for_path(f"file:{final_path}"):
            self._mkdir_parents(normalized)
            for attempt in range(len(self._retry_delays) + 1):
                try:
                    file_obj.seek(0)
                    self.client.upload_fileobj(
                        file_obj, final_path, overwrite=False, size=size
                    )
                except Exception as exc:
                    status = self._status_code(exc)
                    matching = self._destination_matches(
                        normalized,
                        expected_size=size,
                        expected_sha256=expected_sha256,
                    )
                    if matching is not None:
                        return matching
                    if status in {409, 412}:
                        raise StorageUnavailable(
                            f"WebDAV immutable key collision ({status}) key={normalized}"
                        ) from exc
                    if not self._is_transient(exc) or attempt == len(self._retry_delays):
                        raise StorageUnavailable(
                            f"WebDAV direct upload failed ({status or 'network'})"
                        ) from exc
                    self._sleep(self._retry_delays[attempt])
                    continue
                final_stat = self._stat_visible(
                    normalized,
                    stage="final",
                    retry_delays=self._final_visibility_retry_delays,
                )
                if final_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV final size mismatch key={normalized} expected={size} actual={final_stat.size}"
                    )
                verified = self._destination_matches(
                    normalized,
                    expected_size=size,
                    expected_sha256=expected_sha256,
                )
                if verified is None:
                    raise StorageUnavailable(
                        f"WebDAV final content mismatch key={normalized}"
                    )
                return verified
        raise AssertionError("unreachable")

    def put_file(self, key: str, source: Path, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        digest = hashlib.sha256()
        with source.open("rb") as hash_handle:
            for chunk in iter(lambda: hash_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        with source.open("rb") as handle, self._publication_semaphore:
            if immutable:
                if overwrite:
                    raise ValueError("immutable publication requires overwrite=False")
                return self._publish_immutable_fileobj(key, handle, size=source.stat().st_size, expected_sha256=digest.hexdigest())
            return self._publish_fileobj(key, handle, size=source.stat().st_size, expected_sha256=digest.hexdigest(), overwrite=overwrite)

    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        with self._publication_semaphore:
            if immutable:
                if overwrite:
                    raise ValueError("immutable publication requires overwrite=False")
                return self._publish_immutable_fileobj(key, io.BytesIO(content), size=len(content), expected_sha256=hashlib.sha256(content).hexdigest())
            return self._publish_fileobj(key, io.BytesIO(content), size=len(content), expected_sha256=hashlib.sha256(content).hexdigest(), overwrite=overwrite)

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

    def open(self, key: str):
        target = io.BytesIO()
        try: self.client.download_fileobj(self._path(key), target)
        except Exception as exc: raise StorageUnavailable("WebDAV download failed") from exc
        target.seek(0); return target

    def delete(self, key: str, *, missing_ok: bool = True) -> None:
        try: self.client.remove(self._path(key))
        except ResourceNotFound as exc:
            if missing_ok: return
            raise StorageNotFound(key) from exc
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404 and missing_ok: return
            raise StorageUnavailable("WebDAV delete failed") from exc

    def list(self, prefix: str) -> list[ObjectStat]:
        normalized = normalize_storage_key(prefix)
        try: rows = self.client.ls(self._path(normalized), detail=True)
        except ResourceNotFound:
            return []
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
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
            rows = self.client.ls(self._path(normalized), detail=True)
        except ResourceNotFound:
            return []
        except Exception as exc:
            status = self._status_code(exc)
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
