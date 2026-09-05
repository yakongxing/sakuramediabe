from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


class StorageError(RuntimeError): pass
class StorageNotFound(StorageError): pass
class StorageUnavailable(StorageError): pass


@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    is_file: bool = True
    etag: str | None = None


class StorageBackend(Protocol):
    def stat(self, key: str) -> ObjectStat: ...
    def exists(self, key: str) -> bool: ...
    def put_file(self, key: str, source: Path, *, overwrite: bool = True) -> ObjectStat: ...
    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True) -> ObjectStat: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str, *, missing_ok: bool = True) -> None: ...
    def list(self, prefix: str) -> list[ObjectStat]: ...
    def local_path(self, key: str) -> Path | None: ...
