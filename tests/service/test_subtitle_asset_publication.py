import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.service.catalog import subtitle_asset_service as service
from src.storage import webdav
from src.storage.local import LocalStorageBackend
from src.storage.types import StorageUnavailable


@pytest.fixture
def subtitle_import(monkeypatch):
    movie = SimpleNamespace(id=1, movie_number="CAWD-335")
    monkeypatch.setattr(service, "find_movie_by_number", lambda number: movie)
    monkeypatch.setattr(service.SubtitleAssetService, "movie_subtitle_hashes", lambda movie: set())
    rows = Mock()
    rows.create.return_value = SimpleNamespace(id=7)
    monkeypatch.setattr(service, "Subtitle", rows)
    return movie, rows


@pytest.mark.parametrize("from_file", [False, True])
def test_webdav_subtitle_import_avoids_move_and_recovers_unregistered_upload(
    monkeypatch, tmp_path, subtitle_import, from_file,
):
    movie, rows = subtitle_import
    objects = {}
    uploads = []

    class Client:
        def __init__(self, *args, **kwargs): pass
        def mkdir(self, path): pass
        def info(self, path):
            if path not in objects:
                raise webdav.ResourceNotFound(path)
            return {"size": len(objects[path]), "type": "file"}
        def upload_fileobj(self, stream, path, *, overwrite, size):
            assert overwrite is False
            uploads.append(path)
            if path in objects:
                raise webdav.ResourceAlreadyExists(path)
            objects[path] = stream.read()
        def move(self, *args, **kwargs):
            pytest.fail("subtitle publication must not require WebDAV MOVE")
        def ls(self, *args, **kwargs):
            pytest.fail("content-addressed subtitles must not list remote directories")

    monkeypatch.setattr(webdav, "Client", Client)
    backend = webdav.WebDAVStorageBackend("https://dav.example", "assets")
    monkeypatch.setattr(backend, "_download_once", lambda key, target: target.write(objects[backend._path(key)]))
    monkeypatch.setattr(service, "asset_storage", lambda: backend)
    content = b"1\n00:00:01,000 --> 00:00:02,000\nsubtitle\n"
    source = tmp_path / "subtitle.SRT"
    source.write_bytes(content)

    def run():
        if from_file:
            return service.SubtitleAssetService.register_subtitle_file(movie, source)
        return service.SubtitleAssetService.import_subtitle_content(movie.movie_number, content, source.name)

    rows.create.side_effect = RuntimeError("database registration failed")
    with pytest.raises(RuntimeError, match="database registration failed"):
        run()
    rows.create.side_effect = None
    run()

    key = rows.create.call_args.kwargs["file_path"]
    assert key.endswith(f"/CAWD-335-{hashlib.sha256(content).hexdigest()}.srt")
    assert uploads == [f"assets/{key}"] * 2
    assert objects == {f"assets/{key}": content}


def test_failed_subtitle_publication_does_not_register_row(monkeypatch, subtitle_import):
    movie, rows = subtitle_import
    backend = Mock(supports_direct_immutable_put=True)
    backend.put_bytes.side_effect = StorageUnavailable("upload failed")
    monkeypatch.setattr(service, "asset_storage", lambda: backend)
    with pytest.raises(StorageUnavailable):
        service.SubtitleAssetService.import_subtitle_content(movie.movie_number, b"subtitle", "one.srt")
    rows.create.assert_not_called()


@pytest.mark.parametrize("from_file", [False, True])
def test_local_subtitle_import_preserves_numbered_names(monkeypatch, tmp_path, subtitle_import, from_file):
    movie, rows = subtitle_import
    backend = LocalStorageBackend(tmp_path / "assets")
    monkeypatch.setattr(service, "asset_storage", lambda: backend)
    source = tmp_path / "subtitle.srt"
    source.write_bytes(b"subtitle")
    if from_file:
        service.SubtitleAssetService.register_subtitle_file(movie, source)
    else:
        service.SubtitleAssetService.import_subtitle_content(movie.movie_number, b"subtitle", source.name)
    key = rows.create.call_args.kwargs["file_path"]
    assert key.endswith("/CAWD-335-1.srt")
    with backend.open(key) as stream:
        assert stream.read() == b"subtitle"
