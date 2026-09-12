"""Local subtitle writes with read-only recovery of existing WebDAV subtitles."""

import tempfile
from pathlib import Path

from .local import LocalStorageBackend
from .types import StorageError, StorageNotFound, StorageUnavailable


class LocalSubtitleStorage:
    def __init__(self, root: Path, previous):
        self.local = LocalStorageBackend(root)
        self.previous = previous

    def _recover(self, key: str) -> Path:
        path = self.local.local_path(key)
        # Never follow a symlink outside the configured local storage root.
        try:
            path.resolve().relative_to(self.local.root)
        except ValueError as exc:
            raise StorageUnavailable("subtitle path escapes local storage root") from exc
        if not path.exists():
            with self.previous.open(key) as source, tempfile.NamedTemporaryFile() as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                target.flush()
                try:
                    self.local.put_file(key, Path(target.name), overwrite=False)
                except FileExistsError:
                    pass  # Another reader already recovered this key.
        return path

    def local_path(self, key):
        try:
            return self._recover(key)
        except StorageNotFound:
            return self.local.local_path(key)

    def stat(self, key):
        self._recover(key)
        return self.local.stat(key)

    def exists(self, key):
        try:
            return self.stat(key).is_file
        except StorageNotFound:
            return False

    def open(self, key):
        self._recover(key)
        return self.local.open(key)

    def put_file(self, key, source, **kwargs):
        return self.local.put_file(key, source, **kwargs)

    def put_bytes(self, key, content, **kwargs):
        return self.local.put_bytes(key, content, **kwargs)

    def list(self, prefix):
        # Include old remote names during discovery and sequence allocation.
        try:
            items = {item.key: item for item in self.previous.list(prefix)}
        except StorageError:
            items = {}
        items.update({item.key: item for item in self.local.list(prefix)})
        return list(items.values())
