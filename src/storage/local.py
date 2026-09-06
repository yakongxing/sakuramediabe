import os
import shutil
import tempfile
from pathlib import Path

from .keys import normalize_storage_key
from .types import ObjectStat, StorageNotFound


class LocalStorageBackend:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def local_path(self, key: str) -> Path:
        return self.root / Path(*normalize_storage_key(key).split("/"))

    def stat(self, key: str) -> ObjectStat:
        path = self.local_path(key)
        try:
            value = path.stat()
        except FileNotFoundError as exc:
            raise StorageNotFound(key) from exc
        return ObjectStat(normalize_storage_key(key), value.st_size, path.is_file())

    def exists(self, key: str) -> bool:
        try: self.stat(key)
        except StorageNotFound: return False
        return True

    def put_file(self, key: str, source: Path, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        target = self.local_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and target.exists(): raise FileExistsError(key)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            if overwrite:
                os.replace(temporary, target)
            else:
                # Atomic create-if-absent; the earlier exists check is only a
                # fast path and must not permit a concurrent overwrite.
                os.link(temporary, target)
                os.unlink(temporary)
        finally:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
        return self.stat(key)

    def put_bytes(self, key: str, content: bytes, *, overwrite: bool = True, immutable: bool = False) -> ObjectStat:
        with tempfile.NamedTemporaryFile() as source:
            source.write(content); source.flush()
            return self.put_file(key, Path(source.name), overwrite=overwrite, immutable=immutable)

    def open(self, key: str):
        try: return self.local_path(key).open("rb")
        except FileNotFoundError as exc: raise StorageNotFound(key) from exc

    def delete(self, key: str, *, missing_ok: bool = True) -> None:
        try: self.local_path(key).unlink()
        except FileNotFoundError:
            if not missing_ok: raise StorageNotFound(key)

    def list(self, prefix: str) -> list[ObjectStat]:
        directory = self.local_path(prefix)
        if not directory.is_dir(): return []
        return [ObjectStat(f"{normalize_storage_key(prefix)}/{p.name}", p.stat().st_size, True) for p in directory.iterdir() if p.is_file()]
