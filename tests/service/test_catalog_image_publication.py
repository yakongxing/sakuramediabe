from types import SimpleNamespace

import pytest
from PIL import Image as PillowImage

from src.model import Image
from src.service.catalog import movie_image_service as module
from src.service.catalog.movie_image_service import ImagePersistTask, MovieImageService
from src.storage.types import StoragePublicationUnknown


def test_metadata_compensation_preserves_unknown_publication(test_db, tmp_path, monkeypatch):
    source = tmp_path / "source.webp"
    PillowImage.new("RGB", (8, 8)).save(source, format="WEBP")
    tasks = [
        ImagePersistTask("plot", str(source), f"movies/{name}.webp", tmp_path / f"{name}.webp")
        for name in ("unknown", "confirmed")
    ]
    objects = {}
    removed = []

    class Storage:
        supports_direct_immutable_put = True

        def put_file(self, key, source, **kwargs):
            objects[key] = source.read_bytes()
            if "/unknown-" in key:
                raise StoragePublicationUnknown(key, "visibility unknown")

        def delete(self, key, **kwargs):
            removed.append(key)
            objects.pop(key, None)

    storage = Storage()
    service = MovieImageService()
    monkeypatch.setattr(module, "asset_storage", lambda: storage)
    monkeypatch.setattr("src.service.catalog.image_cleanup_service.asset_storage", lambda: storage)
    monkeypatch.setattr(module, "media_image_root_path", lambda: tmp_path)
    monkeypatch.setattr(service, "build_movie_import_image_tasks", lambda *args: (None, [], []))
    monkeypatch.setattr(service, "collect_image_tasks", lambda *args: tasks)
    monkeypatch.setattr(
        service, "resolve_thin_cover_from_prepared_images",
        lambda *args: SimpleNamespace(generated_prepared_file=None),
    )

    with pytest.raises(StoragePublicationUnknown), service.prepare_metadata_images(
        "TEST-001", None, [], local=True,
    ):
        pytest.fail("a partially published batch must not reach the database writer")

    assert len(objects) == 1
    assert "/unknown-" in next(iter(objects))
    assert len(removed) == 1 and "/confirmed-" in removed[0]
    assert not Image.select().exists()
    assert not list(tmp_path.glob("metadata-*"))
