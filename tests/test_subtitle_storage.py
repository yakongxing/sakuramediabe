from unittest.mock import Mock

import pytest

from src.config.config import Storage, settings
from src.storage import factory
from src.storage.local import LocalStorageBackend
from src.storage.subtitles import LocalSubtitleStorage
from src.storage.types import StorageUnavailable

KEY = "movies/94/CAWD-335/subtitles/CAWD-335-1.srt"


def test_subtitles_can_use_local_without_switching_other_storage(monkeypatch, tmp_path):
    remote = Mock()
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.storage, "subtitles_backend", "local")
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    monkeypatch.setattr(factory, "storage_for", lambda namespace: remote)
    assert factory.asset_storage() is remote
    assert factory.clip_storage() is remote
    assert isinstance(factory.subtitle_storage(), LocalSubtitleStorage)


def test_new_subtitle_write_and_read_do_not_use_webdav(tmp_path):
    remote = Mock()
    storage = LocalSubtitleStorage(tmp_path, remote)
    storage.put_bytes(KEY, b"subtitle", overwrite=False)
    with storage.open(KEY) as stream:
        assert stream.read() == b"subtitle"
    assert storage.stat(KEY).size == 8
    assert storage.local_path(KEY).read_bytes() == b"subtitle"
    assert remote.mock_calls == []


def test_old_remote_subtitle_is_copied_locally_and_remote_is_retained(tmp_path):
    remote = LocalStorageBackend(tmp_path / "remote")
    remote.put_bytes(KEY, b"legacy")
    storage = LocalSubtitleStorage(tmp_path / "local", remote)
    assert storage.local_path(KEY).read_bytes() == b"legacy"
    assert remote.local_path(KEY).read_bytes() == b"legacy"
    remote.delete(KEY)
    with storage.open(KEY) as stream:
        assert stream.read() == b"legacy"


def test_discovery_merges_local_and_old_remote_subtitles(tmp_path):
    remote = LocalStorageBackend(tmp_path / "remote")
    remote.put_bytes(KEY, b"old")
    storage = LocalSubtitleStorage(tmp_path / "local", remote)
    storage.put_bytes(KEY, b"local")
    second = KEY.replace("-1.srt", "-2.srt")
    storage.put_bytes(second, b"second")
    items = storage.list(KEY.rsplit("/", 1)[0])
    assert {item.key: item.size for item in items} == {KEY: 5, second: 6}


def test_local_subtitle_discovery_survives_unavailable_webdav(tmp_path):
    remote = Mock()
    remote.list.side_effect = StorageUnavailable("offline")
    storage = LocalSubtitleStorage(tmp_path, remote)
    storage.put_bytes(KEY, b"local")
    assert [item.key for item in storage.list(KEY.rsplit("/", 1)[0])] == [KEY]


def test_local_subtitles_reject_symlinks_outside_root(tmp_path):
    storage = LocalSubtitleStorage(tmp_path / "local", Mock())
    path = storage.local.local_path(KEY)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "private.srt"
    outside.write_bytes(b"private")
    path.symlink_to(outside)
    with pytest.raises(StorageUnavailable):
        storage.local_path(KEY)


def test_subtitle_backend_validation():
    assert Storage(subtitles_backend=" LOCAL ").subtitles_backend == "local"
    with pytest.raises(ValueError, match="subtitles_backend"):
        Storage(subtitles_backend="typo")
