from __future__ import annotations

import hashlib
import io
import math
import os
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx
from loguru import logger
from webdav4.client import (
    BadGatewayError,
    Client,
    ForbiddenOperation,
    InsufficientStorage,
    MultiStatusError,
    ResourceAlreadyExists,
    ResourceConflict,
    ResourceLocked,
    ResourceNotFound,
)

from .keys import normalize_prefix, normalize_storage_key
from .types import (
    ObjectStat,
    PublicationResult,
    StorageConflict,
    StorageError,
    StorageIntegrityError,
    StorageNotFound,
    StoragePublicationUnknown,
    StorageUnavailable,
)
from .upload import PublicationBudget, path_locks, upload_limiter

_UPLOAD_TEMP_NAME = re.compile(r"^\..+\.uploading-[0-9a-f]{32}$")
_budget: ContextVar[PublicationBudget | None] = ContextVar("publication_budget", default=None)


class _HashSink:
    def __init__(self, size: int):
        self.expected_size = size
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes):
        self.size += len(chunk)
        if self.size > self.expected_size:
            raise StorageIntegrityError("WebDAV content exceeds expected size", stage="verify")
        self.digest.update(chunk)


class WebDAVStorageBackend:
    # Immutable writes use the same atomic publish protocol, not check-then-PUT.
    supports_direct_immutable_put = False

    def __init__(
        self, base_url: str, namespace: str, *, username: str = "", password: str = "",
        root_prefix: str = "", verify_tls: bool = True,
        timeout: httpx.Timeout | float = 60.0, sleep=time.sleep,
        retry_delays: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0),
        final_visibility_retry_delays: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
        publication_concurrency_limit: int = 2,
        publication_timeout_seconds: float = 600,
        temp_cleanup_interval_seconds: float = 3600,
        temp_cleanup_max_deletes: int = 16,
        temp_cleanup_age_seconds: float = 86400,
        upload_chunk_size: int = 1024 * 1024,
        download_chunk_size: int = 1024 * 1024,
    ):
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("WebDAV base URL must be http(s), without credentials, query or fragment")
        if publication_concurrency_limit < 1 or min(upload_chunk_size, download_chunk_size) < 1:
            raise ValueError("WebDAV concurrency and chunk sizes must be positive")
        if not math.isfinite(publication_timeout_seconds) or publication_timeout_seconds <= 0:
            raise ValueError("WebDAV publication timeout must be finite and positive")
        if any(not math.isfinite(delay) or delay < 0 for delay in (*retry_delays, *final_visibility_retry_delays)):
            raise ValueError("WebDAV retry delays must be finite and nonnegative")
        self.base_url = base_url.strip().rstrip("/")
        prefix = normalize_prefix(root_prefix)
        self.prefix = "/".join(part for part in (prefix, normalize_storage_key(namespace)) if part)
        self.auth = (username, password) if username or password else None
        self.timeout = httpx.Timeout(timeout)
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._final_visibility_retry_delays = final_visibility_retry_delays
        self.publication_concurrency_limit = publication_concurrency_limit
        self._publication_timeout_seconds = publication_timeout_seconds
        self._limiter = upload_limiter((self.base_url, username), publication_concurrency_limit)
        self._publication_semaphore = self._limiter.semaphore
        self._lock_identity = f"{self.base_url}:{username}"
        self._temp_cleanup_interval_seconds = temp_cleanup_interval_seconds
        self._temp_cleanup_max_deletes = temp_cleanup_max_deletes
        self._temp_cleanup_age_seconds = temp_cleanup_age_seconds
        self._upload_chunk_size = upload_chunk_size
        self._download_chunk_size = download_chunk_size
        self._directory_cache: OrderedDict[str, float] = OrderedDict()
        self._directory_cache_lock = threading.Lock()
        # Each record binds reconciliation to exactly the bytes and mode attempted.
        self._uncertain_publications: OrderedDict[tuple, str] = OrderedDict()
        self._uncertain_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active = 0
        self._retired = False
        self._closed = False
        limits = httpx.Limits(max_connections=max(8, publication_concurrency_limit * 2), max_keepalive_connections=max(4, publication_concurrency_limit))
        self.client = Client(self.base_url, auth=self.auth, timeout=timeout, verify=verify_tls, retry=False, chunk_size=upload_chunk_size, limits=limits)
        try:
            self.http = httpx.Client(auth=self.auth, timeout=timeout, verify=verify_tls, follow_redirects=True, limits=limits)
        except BaseException:
            self.client.http.close()
            raise

    def _path(self, key: str) -> str:
        return f"{self.prefix}/{normalize_storage_key(key)}"

    def _url(self, key: str) -> str:
        return f"{self.base_url}/{quote(self._path(key), safe='/')}"

    def _acquire(self):
        with self._state_lock:
            if self._retired:
                raise StorageUnavailable("Storage backend is closing", retryable=True)
            self._active += 1

    def _release(self):
        with self._state_lock:
            self._active -= 1
            if self._retired and not self._active:
                self._close_clients()

    @contextmanager
    def _using(self):
        self._acquire()
        try:
            yield
        finally:
            self._release()

    def _close_clients(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.client.http.close()
        finally:
            self.http.close()
            self._limiter = None

    def close(self):
        """Retire immediately; close pools only after existing users finish."""
        with self._state_lock:
            self._retired = True
            if not self._active:
                self._close_clients()

    @contextmanager
    def _network(self):
        budget = _budget.get()
        # Only publications use the publication bound; reads have their HTTP pool.
        acquired = False
        try:
            if budget is not None:
                acquired = self._publication_semaphore.acquire(timeout=budget.remaining())
                if not acquired:
                    raise StorageUnavailable("WebDAV network slot timed out", stage="queue", retryable=True, error_code="storage_busy")
                budget.remaining()
            yield
        finally:
            if acquired:
                self._publication_semaphore.release()

    def _request_timeout(self) -> httpx.Timeout:
        budget = _budget.get()
        if budget is None:
            return self.timeout
        remaining = budget.remaining()
        return httpx.Timeout(**{name: min(value, remaining) if value is not None else remaining for name, value in self.timeout.as_dict().items()})

    @staticmethod
    def _check_budget():
        budget = _budget.get()
        if budget is not None:
            budget.remaining()

    def _info_once(self, path: str) -> dict:
        # Client.info uses Depth: 1, which lists every child for a collection.
        response = self.client._request("PROPFIND", path, headers={"Depth": "0"}, timeout=self._request_timeout())
        from xml.etree.ElementTree import Element

        from webdav4.multistatus import DAVProperties, parse_multistatus_response

        result = parse_multistatus_response(response)
        entry = result.responses.get(self.client.join_url(path).path.rstrip("/"))
        if entry is None:
            raise StorageUnavailable("WebDAV PROPFIND omitted the requested resource", stage="stat")
        if entry.status_code == 404:
            raise ResourceNotFound(path)
        if entry.status_code is not None and entry.status_code >= 400:
            raise StorageUnavailable("WebDAV resource metadata unavailable", stage="stat", status_code=entry.status_code, retryable=entry.status_code in {429, 500, 502, 503, 504})
        # webdav4 otherwise merges failed propstat values into valid properties.
        successful = Element("response")
        for propstat in entry.response_xml.findall("{DAV:}propstat"):
            status_line = propstat.findtext("{DAV:}status", "").split()
            if len(status_line) >= 2 and status_line[1] == "200":
                successful.append(propstat)
        info = DAVProperties(successful).as_dict()
        if info.get("type") not in {"file", "directory"}:
            raise StorageUnavailable("WebDAV resource type is missing", stage="stat")
        return info

    @staticmethod
    def _size(info: dict) -> int:
        value = info.get("size", info.get("content_length"))
        if value is None and info.get("type") == "directory":
            return 0
        try:
            size = int(value)
        except (TypeError, ValueError) as exc:
            raise StorageIntegrityError("WebDAV resource length is missing or invalid", stage="stat") from exc
        if size < 0:
            raise StorageIntegrityError("WebDAV resource length is negative", stage="stat")
        return size

    def _stat_once(self, key: str) -> ObjectStat:
        normalized = normalize_storage_key(key)
        try:
            with self._network():
                info = self._info_once(self._path(normalized))
        except Exception as exc:
            if self._status_code(exc) == 404:
                raise StorageNotFound(normalized) from exc
            raise self._error("stat", exc) from exc
        return ObjectStat(normalized, self._size(info), info.get("type", "file") != "directory", info.get("etag"))

    def stat(self, key: str) -> ObjectStat:
        with self._using():
            return self._retry_operation("stat", lambda: self._stat_once(key))

    def exists(self, key: str) -> bool:
        try:
            return self.stat(key).is_file
        except StorageNotFound:
            return False

    def _mkdir_once(self, path: str):
        with self._network():
            self.client.request("MKCOL", path, add_trailing_slash=True, timeout=self._request_timeout())

    def _mkdir_parents(self, key: str) -> None:
        parts = self._path(key).split("/")[:-1]
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            with path_locks.hold(f"mkdir:{self._lock_identity}:{path}", _budget.get()):
                with self._directory_cache_lock:
                    if self._directory_cache.get(path, 0) > time.monotonic():
                        self._directory_cache.move_to_end(path)
                        continue
                try:
                    self._retry_operation("mkdir", lambda path=path: self._mkdir_once(path))
                except Exception as exc:
                    if self._status_code(exc) not in {405, 409, 412}:
                        raise
                    try:
                        def info(path=path):
                            with self._network():
                                return self._info_once(path)
                        existing = self._retry_operation("mkdir_stat", info)
                    except Exception as info_error:
                        if self._status_code(info_error) == 404:
                            raise StorageUnavailable("WebDAV directory disappeared during creation", stage="mkdir", status_code=409) from info_error
                        raise
                    if existing.get("type") != "directory":
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
        path = PurePosixPath(normalize_storage_key(key))
        return path.with_name(f".{path.name}.uploading-{uuid.uuid4().hex}").as_posix()

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        chain = []
        current: BaseException | None = exc
        while current is not None and current not in chain:
            chain.append(current)
            status = getattr(current, "status_code", None) or getattr(getattr(current, "response", None), "status_code", None)
            if status is not None:
                return int(status)
            current = current.__cause__
        for error_type, status in (
            (ResourceNotFound, 404), (StorageNotFound, 404), (ResourceAlreadyExists, 412),
            (ResourceConflict, 409), (ForbiddenOperation, 403), (StorageConflict, 412),
            (InsufficientStorage, 507), (ResourceLocked, 423), (BadGatewayError, 502),
        ):
            if any(isinstance(error, error_type) for error in chain):
                return status
        return None

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        if isinstance(exc, StorageIntegrityError):
            return False
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
    def _error(cls, stage: str, exc: Exception) -> StorageError:
        if isinstance(exc, StorageError):
            fields = {"stage": exc.stage, "status_code": exc.status_code, "retryable": exc.retryable, "error_code": exc.error_code}
            if isinstance(exc, StoragePublicationUnknown):
                return StoragePublicationUnknown(exc.key, str(exc), **fields)
            return type(exc)(str(exc), publication_possible=exc.publication_possible, **fields)
        status = cls._status_code(exc)
        code = {401: "storage_authentication_failed", 403: "storage_permission_denied", 507: "storage_full"}.get(status, "storage_unavailable")
        return StorageUnavailable(f"WebDAV {stage} failed ({status or 'transport'})", stage=stage, status_code=status, retryable=cls._is_transient(exc), error_code=code)

    def _pause(self, exc: Exception, delay: float, *, stage: str, attempt: int) -> None:
        wait = max(0, delay) * random.uniform(0.8, 1.2)
        if self._status_code(exc) in {429, 503}:
            current = exc
            while current is not None:
                value = getattr(getattr(current, "response", None), "headers", {}).get("Retry-After")
                if value:
                    try:
                        seconds = float(value)
                    except ValueError:
                        try:
                            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            break
                    if math.isfinite(seconds):
                        wait = max(wait, min(60, max(0, seconds)))
                    break
                current = current.__cause__
        wait = min(60, wait)
        budget = _budget.get()
        if budget is not None:
            wait = min(wait, budget.remaining())
        logger.debug("WebDAV retry stage={} attempt={} status={} delay_seconds={:.3f}", stage, attempt, self._status_code(exc), wait)
        self._sleep(wait)
        self._check_budget()

    def _retry_operation(self, stage: str, operation):
        for attempt in range(len(self._retry_delays) + 1):
            self._check_budget()
            try:
                return operation()
            except (StorageNotFound, StorageConflict, StorageIntegrityError):
                raise
            except Exception as exc:
                if not self._is_transient(exc) or attempt == len(self._retry_delays):
                    raise self._error(stage, exc) from exc
                self._pause(exc, self._retry_delays[attempt], stage=stage, attempt=attempt + 1)

    def _download_once(self, key: str, target) -> None:
        with self._network(), self.http.stream("GET", self._url(key), headers={"Accept-Encoding": "identity"}, timeout=self._request_timeout()) as response:
            response.raise_for_status()
            if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise StorageUnavailable("WebDAV verification requires an unencoded full response", stage="verify_download")
            # Check every transport chunk; waiting for a large coalesced chunk
            # would let a slow trickle postpone the publication deadline.
            for chunk in response.iter_bytes():
                self._check_budget()
                for offset in range(0, len(chunk), self._download_chunk_size):
                    target.write(chunk[offset:offset + self._download_chunk_size])

    def _destination_matches(self, key: str, *, expected_size: int, expected_sha256: str, destination: ObjectStat | None = None) -> ObjectStat | None:
        destination = destination or self._stat_once(key)
        if not destination.is_file or destination.size != expected_size:
            return None
        sink = _HashSink(expected_size)
        try:
            self._download_once(key, sink)
        except Exception as exc:
            raise self._error("verify_download", exc) from exc
        if sink.size != expected_size or sink.digest.hexdigest() != expected_sha256:
            return None
        return destination

    def _verify_content(self, key: str, size: int, digest: str, *, missing_ok: bool = False) -> ObjectStat | None:
        try:
            return self._verify_content_attempts(key, size, digest, missing_ok=missing_ok)
        except StorageIntegrityError as exc:
            exc.publication_possible = True
            raise
        except StoragePublicationUnknown:
            raise
        except StorageError as exc:
            raise StoragePublicationUnknown(key, "WebDAV publication reconciliation failed", stage="verify", status_code=self._status_code(exc), retryable=exc.retryable) from exc

    def _verify_content_attempts(self, key: str, size: int, digest: str, *, missing_ok: bool = False) -> ObjectStat | None:
        delays = self._final_visibility_retry_delays
        consistently_missing = True
        for attempt in range(len(delays) + 1):
            destination = None
            try:
                destination = self._stat_once(key)
                result = self._destination_matches(key, expected_size=size, expected_sha256=digest, destination=destination)
                if result is None:
                    raise StorageIntegrityError(f"WebDAV content mismatch key={key}", stage="verify", publication_possible=True)
                return result
            except StorageIntegrityError:
                raise
            except (StorageNotFound, StorageUnavailable) as exc:
                # Only repeated metadata 404s permit an immutable retry. A GET
                # failure or an earlier uncertain read is not proof of absence.
                consistently_missing = consistently_missing and destination is None and isinstance(exc, StorageNotFound)
                transient = self._status_code(exc) == 404 or self._is_transient(exc)
                if not transient or attempt == len(delays):
                    if missing_ok and consistently_missing:
                        return None
                    raise StoragePublicationUnknown(key, "WebDAV published object cannot be verified", stage="verify", status_code=self._status_code(exc), retryable=transient) from exc
                self._pause(exc, delays[attempt], stage="verify", attempt=attempt + 1)
        raise AssertionError("unreachable")

    def _upload_once(self, key: str, stream, size: int, digest: str):
        def chunks():
            count = 0
            actual = hashlib.sha256()
            while chunk := stream.read(self._upload_chunk_size):
                self._check_budget()
                count += len(chunk)
                if count > size:
                    raise StorageIntegrityError("Upload source changed size", stage="source")
                actual.update(chunk)
                yield chunk
            if count != size or actual.hexdigest() != digest:
                raise StorageIntegrityError("Upload source changed content", stage="source")

        self.client.request("PUT", self._path(key), content=chunks(), headers={"Content-Length": str(size)}, timeout=self._request_timeout())

    def _upload_temporary(self, key: str, stream, size: int, digest: str, repaired: set[str], safe_temps: set[str]) -> str:
        for attempt in range(len(self._retry_delays) + 1):
            # A timed-out PUT may still be running remotely. Never rewrite its key.
            temporary = self._temporary_key(key)
            try:
                stream.seek(0)
                with self._network():
                    self._upload_once(temporary, stream, size, digest)
                safe_temps.add(temporary)
                return temporary
            except Exception as exc:
                logger.warning("WebDAV unconfirmed temporary retained key={} status={}", temporary, self._status_code(exc))
                directory_error = self._status_code(exc) in {404, 409}
                if directory_error:
                    directory_error = self._restore_parents(key, repaired)
                if not (directory_error or self._is_transient(exc)) or attempt == len(self._retry_delays):
                    raise self._error("temporary upload", exc) from exc
                self._pause(exc, self._retry_delays[attempt], stage="temporary_put", attempt=attempt + 1)
        raise AssertionError("unreachable")

    def _move_once(self, source: str, destination: str, overwrite: bool) -> int:
        try:
            response = self.client.request("MOVE", self._path(source), headers={"Destination": str(self.client.join_url(self._path(destination))), "Overwrite": "T" if overwrite else "F", "Depth": "infinity"}, timeout=self._request_timeout())
        except MultiStatusError as exc:
            raise StoragePublicationUnknown(destination, "WebDAV MOVE reported partial success", stage="move") from exc
        if response.status_code not in {201, 204}:
            raise StoragePublicationUnknown(destination, "WebDAV MOVE returned an unexpected status", stage="move", status_code=response.status_code)
        return response.status_code

    def _move_with_retry(self, temporary: str, key: str, size: int, digest: str, *, overwrite: bool, immutable: bool, repaired: set[str], safe_temps: set[str]) -> tuple[ObjectStat, str]:
        ambiguous = False
        for attempt in range(len(self._retry_delays) + 1):
            sent = False
            try:
                with self._network():
                    # Lock/slot timeouts before this point cannot have published.
                    self._check_budget()
                    safe_temps.discard(temporary)
                    sent = True
                    status = self._move_once(temporary, key, overwrite)
            except StoragePublicationUnknown:
                raise
            except Exception as exc:
                if not sent and not ambiguous:
                    raise
                status = self._status_code(exc)
                if status == 412 and not immutable and not ambiguous:
                    safe_temps.add(temporary)
                    raise StorageConflict(key) from exc
                if not (self._is_transient(exc) or status in {404, 409, 412}):
                    if not ambiguous:
                        safe_temps.add(temporary)
                        raise self._error("publish move", exc) from exc
                    raise StoragePublicationUnknown(key, "WebDAV move outcome cannot be confirmed", stage="move", status_code=status) from exc
                was_ambiguous = ambiguous
                ambiguous = ambiguous or status != 412
                try:
                    matched = self._destination_matches(key, expected_size=size, expected_sha256=digest)
                except (StorageNotFound, StorageUnavailable) as verification_error:
                    matched = None
                    if status == 409 and self._status_code(verification_error) == 404:
                        try:
                            self._restore_parents(key, repaired)
                        except Exception as repair_error:
                            raise StoragePublicationUnknown(key, "WebDAV move cannot be reconciled after directory repair failed", stage="move", retryable=self._is_transient(repair_error)) from repair_error
                    if isinstance(verification_error, StorageIntegrityError):
                        raise StoragePublicationUnknown(key, "WebDAV move result differs from expected content", stage="move_verify", retryable=False) from verification_error
                    if self._status_code(verification_error) != 404 and not self._is_transient(verification_error):
                        raise StoragePublicationUnknown(key, "WebDAV move result cannot be verified", stage="move_verify", status_code=self._status_code(verification_error)) from verification_error
                else:
                    if matched is not None:
                        if status == 412 and not was_ambiguous:
                            safe_temps.add(temporary)
                            return matched, "reused"
                        return matched, "published"
                    if status == 412 and not was_ambiguous:
                        safe_temps.add(temporary)
                        raise StorageConflict(f"WebDAV immutable key content mismatch: {key}") from exc
                if attempt == len(self._retry_delays):
                    raise StoragePublicationUnknown(key, f"WebDAV publish move outcome unknown ({status or 'network'})", stage="move", status_code=status, retryable=True) from exc
                try:
                    self._pause(exc, self._retry_delays[attempt], stage="move", attempt=attempt + 1)
                except Exception as pause_error:
                    raise StoragePublicationUnknown(key, "WebDAV move reconciliation budget exhausted", stage="move", retryable=True) from pause_error
                continue
            # A successful MOVE acknowledges publication; remote reads are only
            # needed to reconcile ambiguous responses or immutable conflicts.
            return ObjectStat(key, size), "created" if status == 201 and not ambiguous else "published"
        raise AssertionError("unreachable")

    def _delete_temporary_best_effort(self, key: str):
        try:
            self._retry_operation("cleanup", lambda key=key: self._remove_once(key, missing_ok=True))
        except Exception:
            logger.warning("WebDAV temporary cleanup failed key={}", key)

    def _publish(self, key: str, stream, *, size: int, expected_sha256: str, overwrite: bool, immutable: bool, validate_source=None) -> PublicationResult:
        key = normalize_storage_key(key)
        identity = (key, size, expected_sha256, overwrite, immutable)
        operation_id = uuid.uuid4().hex
        started = time.monotonic()
        safe_temps: set[str] = set()
        previous = None
        with path_locks.hold(f"file:{self._lock_identity}:{self._path(key)}", _budget.get()):
            try:
                with self._uncertain_lock:
                    previous = self._uncertain_publications.get(identity)
                final = None
                if previous is not None:
                    operation_id = previous
                    final = self._verify_content(key, size, expected_sha256, missing_ok=immutable)
                    disposition = "published"
                if final is None:
                    repaired: set[str] = set()
                    self._ensure_parents(key, repaired)
                    if validate_source is not None:
                        validate_source()
                    temporary = self._upload_temporary(key, stream, size, expected_sha256, repaired, safe_temps)
                    if validate_source is not None:
                        validate_source()
                    final, disposition = self._move_with_retry(temporary, key, size, expected_sha256, overwrite=overwrite, immutable=immutable, repaired=repaired, safe_temps=safe_temps)
                with self._uncertain_lock:
                    self._uncertain_publications.pop(identity, None)
                logger.debug("WebDAV publication succeeded operation={} key={} bytes={} disposition={} elapsed_seconds={:.3f}", operation_id, key, size, disposition, time.monotonic() - started)
                return PublicationResult(final.key, final.size, final.is_file, final.etag, operation_id=operation_id, disposition=disposition)
            except Exception as exc:
                error = exc
                if previous is not None and not getattr(exc, "publication_possible", False) and not isinstance(exc, StorageConflict):
                    if isinstance(exc, StorageIntegrityError):
                        exc.publication_possible = True
                    else:
                        error = StoragePublicationUnknown(key, "WebDAV resumed publication outcome cannot be confirmed", stage=getattr(exc, "stage", None), status_code=self._status_code(exc), retryable=self._is_transient(exc))
                if getattr(error, "publication_possible", False):
                    with self._uncertain_lock:
                        self._uncertain_publications[identity] = operation_id
                        self._uncertain_publications.move_to_end(identity)
                        while len(self._uncertain_publications) > 4096:
                            self._uncertain_publications.popitem(last=False)
                logger.warning("WebDAV publication failed operation={} key={} stage={} status={} possible={} bytes={} elapsed_seconds={:.3f}", operation_id, key, getattr(error, "stage", None), self._status_code(error), getattr(error, "publication_possible", False), size, time.monotonic() - started)
                if error is not exc:
                    raise error from exc
                raise
            finally:
                for temporary in safe_temps:
                    self._delete_temporary_best_effort(temporary)

    def put_file(self, key: str, source: Path, *, overwrite: bool = True, immutable: bool = False) -> PublicationResult:
        if immutable and overwrite:
            raise ValueError("immutable publication requires overwrite=False")
        normalize_storage_key(key)
        with self._using():
            token = _budget.set(PublicationBudget(self._publication_timeout_seconds))
            try:
                digest = hashlib.sha256()
                with source.open("rb") as handle:
                    before = os.fstat(handle.fileno())
                    for chunk in iter(lambda: handle.read(self._upload_chunk_size), b""):
                        self._check_budget()
                        digest.update(chunk)
                    after = os.fstat(handle.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise StorageIntegrityError("Upload source changed while hashing", stage="source")
                    def validate_source():
                        current = os.fstat(handle.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                            raise StorageIntegrityError("Upload source changed during publication", stage="source")
                    return self._publish(key, handle, size=before.st_size, expected_sha256=digest.hexdigest(), overwrite=overwrite, immutable=immutable, validate_source=validate_source)
            finally:
                _budget.reset(token)

    def put_zip(self, key: str, source: Path, *, size: int, sha256: str) -> PublicationResult:
        """Upload a generation ZIP once, then verify every remote byte.

        The caller retains the local archive on failure and retries the same
        bytes at the same generation key. No temporary object or MOVE is needed.
        """
        key = normalize_storage_key(key)
        if not key.endswith(".zip"):
            raise ValueError("ZIP upload requires a .zip key")
        with self._using():
            token = _budget.set(PublicationBudget(self._publication_timeout_seconds))
            try:
                with path_locks.hold(f"file:{self._lock_identity}:{self._path(key)}", _budget.get()):
                    self._mkdir_parents(key)
                    with source.open("rb") as stream, self._network():
                        self._upload_once(key, stream, size, sha256)
                    final = self._destination_matches(key, expected_size=size, expected_sha256=sha256)
                    if final is None:
                        raise StorageIntegrityError("WebDAV ZIP content mismatch", stage="verify")
                    return PublicationResult(final.key, final.size, final.is_file, final.etag)
            except Exception as exc:
                raise self._error("ZIP upload", exc) from exc
            finally:
                _budget.reset(token)

    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True, immutable: bool = False) -> PublicationResult:
        if immutable and overwrite:
            raise ValueError("immutable publication requires overwrite=False")
        normalize_storage_key(key)
        with self._using():
            token = _budget.set(PublicationBudget(self._publication_timeout_seconds))
            try:
                return self._publish(key, io.BytesIO(content), size=len(content), expected_sha256=hashlib.sha256(content).hexdigest(), overwrite=overwrite, immutable=immutable)
            finally:
                _budget.reset(token)

    def open(self, key: str):
        def download():
            target = io.BytesIO()
            try:
                self._download_once(key, target)
            except Exception as exc:
                target.close()
                if self._status_code(exc) == 404:
                    raise StorageNotFound(key) from exc
                raise
            target.seek(0)
            return target
        with self._using():
            return self._retry_operation("download", download)

    def _remove_once(self, key: str, *, missing_ok: bool):
        try:
            with self._network():
                self.client.request("DELETE", self._path(key), timeout=self._request_timeout())
        except Exception as exc:
            if self._status_code(exc) == 404:
                if missing_ok:
                    return
                raise StorageNotFound(key) from exc
            raise

    def delete(self, key: str, *, missing_ok: bool = True) -> None:
        with self._using():
            self._retry_operation("delete", lambda: self._remove_once(key, missing_ok=missing_ok))

    def list(self, prefix: str) -> list[ObjectStat]:
        normalized = normalize_storage_key(prefix)
        with self._using():
            try:
                rows = self._retry_operation("list", lambda: self.client.ls(self._path(normalized), detail=True))
            except Exception as exc:
                if self._status_code(exc) == 404:
                    return []
                raise
        result = []
        for row in rows:
            if row.get("type") == "directory":
                continue
            name = str(row.get("name", "")).rstrip("/").rsplit("/", 1)[-1]
            if name:
                result.append(ObjectStat(f"{normalized}/{name}", self._size(row), True, row.get("etag")))
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

    def cleanup_expired_uploads(self, prefix: str, *, older_than: datetime, max_deletes: int = 16, uploads_paused: bool = False) -> list[str]:
        """Explicit maintenance only: callers must pause writers across processes."""
        if not uploads_paused:
            raise ValueError("pause all WebDAV writers before expired upload cleanup")
        if max_deletes <= 0:
            return []
        normalized = normalize_storage_key(prefix)
        cutoff = older_than.replace(tzinfo=timezone.utc) if older_than.tzinfo is None else older_than.astimezone(timezone.utc)
        with self._using():
            try:
                rows = self._retry_operation("cleanup_list", lambda: self.client.ls(self._path(normalized), detail=True))
            except Exception as exc:
                if self._status_code(exc) == 404:
                    return []
                raise
            deleted = []
            for row in rows:
                name = str(row.get("name", "")).rstrip("/").rsplit("/", 1)[-1]
                modified = self._modified_at(row.get("modified") or row.get("last_modified"))
                if row.get("type") == "directory" or not _UPLOAD_TEMP_NAME.fullmatch(name) or modified is None or modified >= cutoff:
                    continue
                key = f"{normalized}/{name}"
                try:
                    self._retry_operation("cleanup", lambda key=key: self._remove_once(key, missing_ok=True))
                except StorageUnavailable:
                    continue
                deleted.append(key)
                if len(deleted) >= max_deletes:
                    break
            return deleted

    def local_path(self, key: str):
        return None

    def range_response(self, key: str, range_header: str | None, content_type: str):
        from fastapi.responses import StreamingResponse
        from starlette.background import BackgroundTask

        self._acquire()
        response = None
        released = False
        guard = threading.Lock()

        def close():
            nonlocal released
            with guard:
                if not released:
                    released = True
                    try:
                        if response is not None:
                            response.close()
                    finally:
                        self._release()
        try:
            headers = {"Range": range_header} if range_header else {}
            request = self.http.build_request("GET", self._url(key), headers=headers)
            response = self.http.send(request, stream=True)
            if response.status_code == 404:
                raise StorageNotFound(key)
            if response.status_code not in {200, 206, 416}:
                raise StorageUnavailable(f"WebDAV GET failed ({response.status_code})")
            outgoing = {name: value for name, value in response.headers.items() if name.lower() in {"accept-ranges", "content-length", "content-range", "etag", "last-modified"}}
            if response.status_code == 416:
                body = response.read()
                close()
                return StreamingResponse(iter([body]), status_code=416, headers=outgoing, media_type=content_type)
            def chunks():
                try:
                    yield from response.iter_bytes()
                finally:
                    close()
            return StreamingResponse(chunks(), status_code=response.status_code, headers=outgoing, media_type=response.headers.get("content-type", content_type), background=BackgroundTask(close))
        except BaseException:
            close()
            raise
