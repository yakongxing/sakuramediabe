"""Durable thumbnail workspaces; callers hold both the media and local batch locks."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from loguru import logger

from src.config import settings
from src.service.playback.operation_locks import MediaOperationBusy


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _require(condition) -> None:
    if not condition:
        raise ValueError("thumbnail_batch_manifest_invalid")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ThumbnailBatch:
    def __init__(self, store, manifest):
        self.store = store
        self.manifest = manifest
        self.workspace = store.directory / manifest["generation"]
        self._checkpoint_lock = RLock()

    @property
    def packed(self) -> bool:
        return self.manifest.get("format", "loose") == "zip"

    @property
    def entries(self):
        return self.manifest["images"]

    def source(self, entry) -> Path:
        path = self.workspace / "images" / f"{entry['offset']}.webp"
        if not path.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("thumbnail_batch_path_invalid")
        return path

    def key(self, prefix: str, entry) -> str:
        return f"{prefix}/{self.manifest['generation']}/{entry['offset']}.webp"

    def validate_files(self) -> None:
        for entry in self.entries:
            path = self.source(entry)
            if not path.is_file() or file_digest(path) != (entry["size"], entry["sha256"]):
                raise ValueError("thumbnail_batch_file_invalid")

    def checkpoint(self, entry, state: str) -> None:
        with self._checkpoint_lock:
            previous = entry["state"]
            entry["state"] = state
            try:
                self.store.save(self.manifest)
            except BaseException:
                entry["state"] = previous
                raise

    def cleanup(self) -> None:
        # Keep the manifest if removal fails: a later committed-batch reconciliation
        # can finish cleanup even when some of the local files are already gone.
        try:
            if self.workspace.exists():
                shutil.rmtree(self.workspace)
            self.store.manifest_path.unlink(missing_ok=True)
            _sync_directory(self.store.directory)
        except OSError as exc:
            logger.warning("Thumbnail local cleanup failed media_id={} detail={}", self.store.media_id, exc)


class ThumbnailBatchStore:
    def __init__(self, media):
        self.media_id = media.id
        config = settings.storage
        if config.backend == "local":
            destination = ["local", str(Path(settings.media.import_image_root_path).expanduser().resolve())]
        else:
            url = urlsplit(config.webdav_base_url)
            destination = ["webdav", url.scheme, url.hostname, url.port, url.path.rstrip("/"),
                           config.root_prefix.strip("/"), config.username or url.username, "assets"]
        # Persist only hashes, never provider storage refs, URLs or credentials.
        source = {name: getattr(media, name) for name in (
            "id", "created_at", "library_id", "movie_number", "video_item_id", "storage_ref",
            "file_name", "file_size_bytes", "file_hash", "import_source_identity", "duration_seconds",
        )}
        self.identity = _fingerprint([source, destination])
        self.root = Path(settings.media.thumbnail_staging_root_path).expanduser().resolve()
        self.directory = self.root / str(media.id) / self.identity
        self.manifest_path = self.directory / "current.json"

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # The lock inode must survive deletion of any individual batch directory.
        with (self.root / f".{self.media_id}.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MediaOperationBusy() from exc
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                _sync_directory(self.directory.parent)
                _sync_directory(self.root)
                _sync_directory(self.root.parent)
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def new_workspace(self) -> Path:
        workspace = self.directory / uuid4().hex
        workspace.mkdir()
        return workspace

    def save(self, manifest) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(manifest, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
            _sync_directory(self.directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self) -> ThumbnailBatch | None:
        if not self.manifest_path.exists():
            return None
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            _require(manifest["version"] in (1, 2))
            _require(manifest.get("format", "loose") in {"loose", "zip"})
            _require(manifest["version"] != 1 or manifest.get("format", "loose") == "loose")
            _require(manifest["identity"] == self.identity)
            _require(manifest["media_id"] == self.media_id)
            _require(re.fullmatch(r"[0-9a-f]{32}", manifest["generation"]))
            entries = manifest["images"]
            _require(isinstance(entries, list) and entries)
            offsets = set()
            for entry in entries:
                _require(type(entry["offset"]) is int and entry["offset"] >= 0)
                _require(entry["offset"] not in offsets)
                offsets.add(entry["offset"])
                _require(type(entry["size"]) is int and entry["size"] > 0)
                _require(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]))
                _require(entry["state"] in {"pending", "uploading", "uploaded"})
            batch = ThumbnailBatch(self, manifest)
            _require(batch.workspace.resolve().parent == self.directory.resolve())
            return batch
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("thumbnail_batch_manifest_invalid") from exc

    def prepare(self, artifacts, workspace: Path | None = None, *, packed: bool = False) -> ThumbnailBatch:
        if not artifacts:
            raise ValueError("thumbnail_generation_empty")
        workspace = workspace or self.new_workspace()
        images = workspace / "images"
        if workspace.resolve().parent != self.directory.resolve():
            raise ValueError("thumbnail_batch_path_invalid")
        images.mkdir(exist_ok=True)
        if images.is_symlink() or images.resolve().parent != workspace.resolve():
            raise ValueError("thumbnail_batch_path_invalid")
        entries = []
        offsets = set()
        for artifact, source in artifacts:
            offset = artifact.offset_seconds
            if type(offset) is not int or offset < 0 or offset in offsets:
                raise ValueError("thumbnail_offset_invalid")
            offsets.add(offset)
            target = images / f"{offset}.webp"
            if target.is_symlink():
                raise ValueError("thumbnail_batch_path_invalid")
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
            size, digest = file_digest(target)
            if not size:
                raise ValueError("thumbnail_artifact_empty")
            entries.append({"offset": offset, "size": size, "sha256": digest, "state": "pending"})
        _sync_directory(images)
        _sync_directory(workspace)
        manifest = {"version": 2, "format": "zip" if packed else "loose", "identity": self.identity, "media_id": self.media_id,
                    "generation": workspace.name, "images": sorted(entries, key=lambda entry: entry["offset"])}
        self.save(manifest)
        return ThumbnailBatch(self, manifest)
