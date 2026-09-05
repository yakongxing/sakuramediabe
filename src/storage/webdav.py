import io
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import httpx
from webdav4.client import Client, ResourceAlreadyExists, ResourceNotFound

from .keys import normalize_prefix, normalize_storage_key
from .types import ObjectStat, StorageNotFound, StorageUnavailable


_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _lock_for_path(path: str) -> threading.Lock:
    with _locks_guard:
        return _path_locks[path]


class WebDAVStorageBackend:
    def __init__(self, base_url: str, namespace: str, *, username: str = "", password: str = "", root_prefix: str = "", verify_tls: bool = True, timeout: httpx.Timeout | float = 60.0):
        self.base_url = base_url.rstrip("/")
        prefix = normalize_prefix(root_prefix)
        self.prefix = "/".join(part for part in (prefix, normalize_storage_key(namespace)) if part)
        self.auth = (username, password) if username or password else None
        self.timeout = timeout
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

    def _publish_fileobj(self, key: str, file_obj, *, size: int, overwrite: bool) -> ObjectStat:
        normalized = normalize_storage_key(key)
        if not overwrite and self.exists(normalized): raise FileExistsError(normalized)
        final_path = self._path(normalized)
        tmp_key = self._temporary_key(normalized)
        tmp_path = self._path(tmp_key)
        with _lock_for_path(f"file:{final_path}"):
            self._mkdir_parents(normalized)
            try:
                self.client.upload_fileobj(file_obj, tmp_path, overwrite=True, size=size)
                tmp_stat = self.stat(tmp_key)
                if tmp_stat.size != size:
                    raise StorageUnavailable(
                        f"WebDAV upload size mismatch key={normalized} expected={size} actual={tmp_stat.size}"
                    )
                if overwrite:
                    self.delete(normalized, missing_ok=True)
                self.client.move(tmp_path, final_path, overwrite=overwrite)
                return self.stat(normalized)
            except Exception:
                self.delete(tmp_key, missing_ok=True)
                raise

    def put_file(self, key: str, source: Path, *, overwrite: bool = True) -> ObjectStat:
        with source.open("rb") as handle:
            return self._publish_fileobj(key, handle, size=source.stat().st_size, overwrite=overwrite)

    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True) -> ObjectStat:
        return self._publish_fileobj(key, io.BytesIO(content), size=len(content), overwrite=overwrite)

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
