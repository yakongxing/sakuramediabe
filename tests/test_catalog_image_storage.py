from pathlib import Path

from src.service.catalog.movie_image_service import ImagePersistTask, MovieImageService, PreparedImageFile


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

        def put_file(self, key, source):
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
