from pathlib import Path

import pytest

from src.storage.keys import normalize_storage_key
from src.storage.local import LocalStorageBackend


@pytest.mark.parametrize("key", ["", "/absolute", "a//b", "a/../b", "a\\b", "a/%2e%2e/b", "a/%252e%252e/b", "a\x00b"])
def test_storage_key_rejects_unsafe_values(key):
    with pytest.raises(ValueError):
        normalize_storage_key(key)


def test_storage_key_preserves_safe_posix_key():
    assert normalize_storage_key("movies/ab/ABP-001/cover.jpg") == "movies/ab/ABP-001/cover.jpg"


def test_local_backend_round_trip(tmp_path):
    backend = LocalStorageBackend(tmp_path / "assets")
    backend.put_bytes("movies/a.jpg", b"image")
    assert backend.stat("movies/a.jpg").size == 5
    with backend.open("movies/a.jpg") as handle:
        assert handle.read() == b"image"
    backend.delete("movies/a.jpg")
    assert not backend.exists("movies/a.jpg")


def test_webdav_backend_maps_namespace(monkeypatch):
    from src.storage import webdav as module

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def info(self, path):
            assert path == "tenant/assets/movies/a.jpg"
            return {"size": 7, "type": "file", "etag": "x"}

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", root_prefix="tenant")
    assert backend.stat("movies/a.jpg").size == 7


def test_webdav_put_file_publishes_through_temporary_key(monkeypatch, tmp_path):
    from src.storage import webdav as module

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass

        def info(self, path):
            if path.startswith("tenant/assets/movies/.cover.jpg.uploading-"):
                return {"size": 5, "type": "file", "etag": "tmp"}
            if path == "tenant/assets/movies/cover.jpg":
                return {"size": 5, "type": "file", "etag": "final"}
            raise module.ResourceNotFound(path)

        def mkdir(self, path):
            calls.append(("mkdir", path))
            raise module.ResourceAlreadyExists(path)

        def upload_fileobj(self, file_obj, to_path, *, overwrite=False, size=None, **kwargs):
            calls.append(("upload", to_path, overwrite, size, file_obj.read()))

        def remove(self, path):
            calls.append(("remove", path))

        def move(self, src_path, dst_path, *, overwrite=False):
            calls.append(("move", src_path, dst_path, overwrite))

    monkeypatch.setattr(module, "Client", FakeClient)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: object())
    backend = module.WebDAVStorageBackend("https://dav.example/root", "assets", root_prefix="tenant")
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"image")

    stat = backend.put_file("movies/cover.jpg", source)

    assert stat.size == 5
    uploads = [call for call in calls if call[0] == "upload"]
    assert len(uploads) == 1
    assert uploads[0][1].startswith("tenant/assets/movies/.cover.jpg.uploading-")
    assert uploads[0][2] is True
    assert uploads[0][3] == 5
    assert uploads[0][4] == b"image"
    assert ("remove", "tenant/assets/movies/cover.jpg") in calls
    moves = [call for call in calls if call[0] == "move"]
    assert moves == [("move", uploads[0][1], "tenant/assets/movies/cover.jpg", True)]
    assert all(call[1] != "tenant/assets/movies/cover.jpg" for call in uploads)
