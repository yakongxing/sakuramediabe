from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


class StorageError(RuntimeError):
    def __init__(
        self, message: str = "", *, stage: str | None = None,
        status_code: int | None = None, retryable: bool = False,
        publication_possible: bool = False,
    ):
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.retryable = retryable
        self.publication_possible = publication_possible


class StorageNotFound(StorageError): pass
class StorageUnavailable(StorageError): pass


class StoragePublicationUnknown(StorageUnavailable):
    """A publish may have committed, but the backend could not confirm visibility."""

    def __init__(self, key: str, message: str, **kwargs):
        super().__init__(message, publication_possible=True, **kwargs)
        self.key = key


@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    is_file: bool = True
    etag: str | None = None


class StorageBackend(Protocol):
    def stat(self, key: str) -> ObjectStat: ...
    def exists(self, key: str) -> bool: ...
    def put_file(self, key: str, source: Path, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat: ...
    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str, *, missing_ok: bool = True) -> None: ...
    def list(self, prefix: str) -> list[ObjectStat]: ...
    def local_path(self, key: str) -> Path | None: ...
