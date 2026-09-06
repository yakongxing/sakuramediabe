import hashlib
import io
import threading
import time
from pathlib import Path

import pytest

from src.service.catalog.movie_image_service import (
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
)


def test_finalize_prepared_image_files_skips_upload_when_size_unchanged(monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as module
    from src.storage.types import ObjectStat

    class FakeStorage:
        def __init__(self):
            self.put_calls = []

        def exists(self, key):
            return True

        def stat(self, key):
            return ObjectStat(key=key, size=5)

        def open(self, key):
            return io.BytesIO(b"image")

        def put_file(self, key, source, *, overwrite=True):
            self.put_calls.append((key, source))

    storage = FakeStorage()
    monkeypatch.setattr(module, "asset_storage", lambda: storage)
    temp_root = tmp_path / "refresh"
    temp_root.mkdir()
    temp_path = temp_root / "plot-0.jpg"
    temp_path.write_bytes(b"image")
    prepared = PreparedImageFile(
        image_task=ImagePersistTask(
            image_type="plot",
            image_url="https://example.invalid/plot-0.jpg",
            relative_path="movies/ab/ABC-001/plot-0.jpg",
            absolute_path=Path("/unused/plot-0.jpg"),
            plot_index=0,
        ),
        temp_path=temp_path,
        temp_root=temp_root,
    )

    MovieImageService().finalize_prepared_image_files([prepared])

    assert storage.put_calls == []
    assert not temp_root.exists()


def test_finalize_prepared_image_files_uploads_same_size_different_content(monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as module
    from src.storage.types import ObjectStat

    class FakeStorage:
        def __init__(self): self.put_calls = []
        def stat(self, key): return ObjectStat(key=key, size=5)
        def open(self, key): return io.BytesIO(b"other")
        def put_file(self, key, source, *, overwrite=True): self.put_calls.append((key, source.read_bytes()))

    storage = FakeStorage()
    monkeypatch.setattr(module, "asset_storage", lambda: storage)
    temp_root = tmp_path / "refresh"
    temp_root.mkdir()
    temp_path = temp_root / "cover.jpg"
    temp_path.write_bytes(b"image")
    prepared = PreparedImageFile(
        ImagePersistTask("cover", "https://example.invalid/cover.jpg", "movies/a/cover.jpg", Path("/unused")),
        temp_path,
        temp_root,
    )

    MovieImageService().finalize_prepared_image_files([prepared])

    assert storage.put_calls == [("movies/a/cover.jpg", b"image")]


def test_version_prepared_image_keys_uses_full_sha256_digest(tmp_path):
    content = b"immutable image content"
    temp_root = tmp_path / "refresh"
    temp_root.mkdir()
    temp_path = temp_root / "cover.jpg"
    temp_path.write_bytes(content)
    prepared = PreparedImageFile(
        ImagePersistTask(
            "cover",
            "https://example.invalid/cover.jpg",
            "movies/a/cover.jpg",
            Path("/unused"),
        ),
        temp_path,
        temp_root,
    )

    MovieImageService.version_prepared_image_keys([prepared])

    digest = hashlib.sha256(content).hexdigest()
    assert prepared.image_task.relative_path == f"movies/a/cover-{digest}.jpg"


def test_delete_obsolete_image_files_uses_storage_backend(monkeypatch):
    from src.service.catalog import image_cleanup_service as module

    class FakeStorage:
        def __init__(self):
            self.deleted = []

        def delete(self, key, *, missing_ok=True):
            self.deleted.append((key, missing_ok))

    storage = FakeStorage()
    monkeypatch.setattr(module, "asset_storage", lambda: storage, raising=False)

    module.ImageCleanupService.delete_obsolete_image_files({"movies/ab/ABC-001/plot-0.jpg"})

    assert storage.deleted == [("movies/ab/ABC-001/plot-0.jpg", True)]


def test_image_service_does_not_add_backend_publication_semaphore(monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as module
    from src.storage.types import ObjectStat

    class FakeStorage:
        publication_concurrency_limit = 2

        def __init__(self):
            self.active = 0
            self.maximum_active = 0
            self.lock = threading.Lock()

        def exists(self, key): return False
        def local_path(self, key): return None
        def put_file(self, key, source):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return ObjectStat(key, source.stat().st_size)

    storage = FakeStorage()
    monkeypatch.setattr(module, "asset_storage", lambda: storage)
    service = MovieImageService(image_downloader=lambda url, path: path.write_bytes(b"image"))
    tasks = [
        ImagePersistTask("actor", f"https://example.invalid/{index}.jpg", f"actors/{index}.jpg", tmp_path / f"{index}.jpg")
        for index in range(8)
    ]

    service.download_image_tasks(tasks)

    assert 2 < storage.maximum_active <= service.IMAGE_DOWNLOAD_MAX_WORKERS


def test_optional_image_publication_failure_is_not_swallowed(monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as module
    from src.storage.types import StorageUnavailable

    class FakeStorage:
        publication_concurrency_limit = 1
        def exists(self, key): return False
        def local_path(self, key): return None
        def put_file(self, key, source):
            raise StorageUnavailable("WebDAV publish failed")

    monkeypatch.setattr(module, "asset_storage", lambda: FakeStorage())
    service = MovieImageService(image_downloader=lambda url, path: path.write_bytes(b"image"))
    task = ImagePersistTask(
        "actor", "https://example.invalid/avatar.jpg", "actors/a.jpg", tmp_path / "a.jpg"
    )

    with pytest.raises(StorageUnavailable, match="publish failed"):
        service.download_image_tasks([task])
