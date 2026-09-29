"""Subtitle publication uses database session locks, not process-local mutexes."""

import io
from contextlib import contextmanager
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from src.common.media_paths import movie_asset_relative_dir
from src.model import Movie, Subtitle, get_database
from src.schema.catalog.subtitles import SubtitleImportStatus
from src.service.catalog import movie_subtitle_service, subtitle_asset_service
from src.service.catalog.movie_subtitle_service import MovieSubtitleService
from src.service.catalog.subtitle_asset_service import SubtitleAssetService
from src.storage.subtitles import LocalSubtitleStorage
from src.storage.types import (
    ObjectStat,
    StorageNotFound,
    StoragePublicationUnknown,
    StorageUnavailable,
)


class SubtitleStorage:
    def __init__(self):
        self.objects = {}
        self.published = []
        self.deleted = []

    def list(self, prefix):
        return [ObjectStat(key, len(value)) for key, value in self.objects.items() if key.startswith(prefix + "/")]

    def open(self, key):
        if key not in self.objects:
            raise StorageNotFound(key)
        return io.BytesIO(self.objects[key])

    def exists(self, key):
        return key in self.objects

    def put_bytes(self, key, content, *, overwrite):
        assert overwrite is False
        self.published.append(key)
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = content
        return SimpleNamespace(key=key, size=len(content), created=True)

    def put_file(self, key, source, *, overwrite):
        return self.put_bytes(key, source.read_bytes(), overwrite=overwrite)

    def delete(self, key, *, missing_ok=True):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def subtitle_publication(test_db, monkeypatch):
    movie = Movie.create(movie_number="SUB-001", javdb_id="sub-1", title="subtitle")
    storage = SubtitleStorage()
    monkeypatch.setattr(subtitle_asset_service, "subtitle_storage", lambda: storage)
    monkeypatch.setattr(movie_subtitle_service, "subtitle_storage", lambda: storage)
    return movie, storage


def subtitle_key(movie, number):
    return f"{movie_asset_relative_dir(movie.movie_number)}/subtitles/{movie.movie_number}-{number}.srt"


def import_content(movie, content=b"subtitle"):
    return SubtitleAssetService.import_subtitle_content(movie.movie_number, content, "source.srt")


def test_sequence_includes_database_paths_not_yet_visible_in_listing(subtitle_publication):
    movie, storage = subtitle_publication
    Subtitle.create(movie=movie, file_path=subtitle_key(movie, 5))
    storage.objects[subtitle_key(movie, 2)] = b"remote"
    result = import_content(movie)
    assert Subtitle.get_by_id(result.subtitle_id).file_path == subtitle_key(movie, 6)
    assert storage.objects[subtitle_key(movie, 2)] == b"remote"


def test_stale_listing_conflict_advances_sequence(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    first = subtitle_key(movie, 1)
    storage.objects[first] = b"foreign"
    monkeypatch.setattr(storage, "list", lambda _prefix: [])
    result = import_content(movie)
    assert storage.published == [first, subtitle_key(movie, 2)]
    assert storage.objects[first] == b"foreign"
    assert Subtitle.get_by_id(result.subtitle_id).file_path == subtitle_key(movie, 2)


def test_creation_conflicts_are_bounded(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    monkeypatch.setattr(storage, "list", lambda _prefix: [])
    for index in range(1, 5):
        storage.objects[subtitle_key(movie, index)] = b"foreign"
    with pytest.raises(FileExistsError):
        import_content(movie)
    assert len(storage.published) == SubtitleAssetService.CREATE_ATTEMPTS == 3
    assert not Subtitle.select().exists()
    assert not storage.deleted


def test_remote_hash_failure_aborts_instead_of_deduplicating_as_missing(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    Subtitle.create(movie=movie, file_path=subtitle_key(movie, 1))

    def unavailable(_key):
        raise StorageUnavailable("offline", retryable=True)

    monkeypatch.setattr(storage, "open", unavailable)
    with pytest.raises(StorageUnavailable, match="offline"):
        import_content(movie)
    assert not storage.published
    assert Subtitle.select().count() == 1


def test_unknown_publication_retains_remote_object(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    original = storage.put_bytes

    def unknown(key, content, **kwargs):
        original(key, content, **kwargs)
        raise StoragePublicationUnknown(key, "response lost")

    monkeypatch.setattr(storage, "put_bytes", unknown)
    with pytest.raises(StoragePublicationUnknown):
        import_content(movie)
    assert storage.objects == {subtitle_key(movie, 1): b"subtitle"}
    assert not storage.deleted
    assert not Subtitle.select().exists()


def test_registration_rollback_cleans_only_confirmed_creation(subtitle_publication, monkeypatch, tmp_path):
    movie, storage = subtitle_publication
    source = tmp_path / "source.srt"
    source.write_bytes(b"subtitle")

    def failed(**_kwargs):
        raise RuntimeError("registration failed")

    monkeypatch.setattr(Subtitle, "create", failed)
    with pytest.raises(RuntimeError, match="registration failed"):
        SubtitleAssetService.register_subtitle_file(movie, source)
    assert storage.deleted == [subtitle_key(movie, 1)]
    assert storage.objects == {}
    assert source.read_bytes() == b"subtitle"


def test_uncertain_commit_with_existing_row_keeps_publication(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    database = get_database()
    original_atomic = database.atomic

    @contextmanager
    def lost_acknowledgement(*args, **kwargs):
        with original_atomic(*args, **kwargs):
            yield
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(database, "atomic", lost_acknowledgement)
    with pytest.raises(RuntimeError, match="commit response lost"):
        import_content(movie)
    assert Subtitle.select().count() == 1
    assert not storage.deleted
    assert storage.objects[subtitle_key(movie, 1)] == b"subtitle"


def test_lock_connection_lost_after_upload_keeps_object(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    original = storage.put_bytes

    def lose_connection(*args, **kwargs):
        result = original(*args, **kwargs)
        get_database().close()
        return result

    monkeypatch.setattr(storage, "put_bytes", lose_connection)
    with pytest.raises(RuntimeError, match="connection_lost"):
        import_content(movie)
    assert not storage.deleted
    assert storage.objects[subtitle_key(movie, 1)] == b"subtitle"
    assert not Subtitle.select().exists()


def test_sync_does_not_partially_delete_when_later_storage_check_fails(subtitle_publication, monkeypatch):
    movie, storage = subtitle_publication
    for index in (1, 2):
        Subtitle.create(movie=movie, file_path=subtitle_key(movie, index))
    checks = []

    def check(key):
        checks.append(key)
        if len(checks) == 1:
            return False
        raise StorageUnavailable("offline")

    monkeypatch.setattr(storage, "exists", check)
    with pytest.raises(StorageUnavailable, match="offline"):
        MovieSubtitleService.sync_movie_subtitles(movie)
    assert len(checks) == 2
    assert Subtitle.select().count() == 2


def test_register_refreshes_stale_external_hash_cache(subtitle_publication, tmp_path):
    movie, storage = subtitle_publication
    result = import_content(movie)
    source = tmp_path / "source.srt"
    source.write_bytes(b"subtitle")
    status, reason, _ = SubtitleAssetService.register_subtitle_file(
        movie, source, existing_hashes={movie.id: set()},
    )
    assert (status, reason) == ("skipped", "duplicate_fingerprint")
    assert Subtitle.select().count() == 1
    assert Subtitle.get_by_id(result.subtitle_id)
    assert len(storage.published) == 1


@pytest.mark.parametrize("second_action", ["import", "sync"])
def test_other_session_waits_until_upload_and_registration_complete(subtitle_publication, monkeypatch, second_action):
    movie, storage = subtitle_publication
    uploading, release, contender = Event(), Event(), Event()
    outcomes, errors = {}, []
    original_put = storage.put_bytes
    original_lock = subtitle_asset_service.subtitle_operation_lock

    def blocked_put(*args, **kwargs):
        result = original_put(*args, **kwargs)
        if current_thread().name == "first":
            uploading.set()
            assert release.wait(10)
        return result

    @contextmanager
    def tracked_lock(movie_id):
        if current_thread().name == "second":
            contender.set()
        with original_lock(movie_id) as check:
            yield check

    monkeypatch.setattr(storage, "put_bytes", blocked_put)
    monkeypatch.setattr(subtitle_asset_service, "subtitle_operation_lock", tracked_lock)
    monkeypatch.setattr(movie_subtitle_service, "subtitle_operation_lock", tracked_lock)

    def run(name, operation):
        try:
            with get_database().connection_context():
                outcomes[name] = operation()
        except BaseException as exc:
            errors.append(exc)

    first = Thread(name="first", target=run, args=("first", lambda: import_content(movie, b"first")))
    operation = (
        lambda: import_content(movie, b"second")
    ) if second_action == "import" else lambda: MovieSubtitleService.sync_movie_subtitles(movie)
    second = Thread(name="second", target=run, args=("second", operation))
    first.start()
    try:
        assert uploading.wait(10)
        second.start()
        assert contender.wait(10)
        assert len(storage.published) == 1
        assert not Subtitle.select().exists()
    finally:
        release.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert outcomes["first"].status == SubtitleImportStatus.IMPORTED
    if second_action == "import":
        assert outcomes["second"].status == SubtitleImportStatus.IMPORTED
        assert Subtitle.select().count() == 2
        assert storage.objects == {subtitle_key(movie, 1): b"first", subtitle_key(movie, 2): b"second"}
    else:
        assert outcomes["second"] == {"created_subtitles": 0, "deleted_subtitles": 0, "total_subtitles": 1}


def test_local_override_compensation_never_deletes_remote_file(tmp_path):
    remote = SubtitleStorage()
    key = "movies/a/subtitles/1.srt"
    remote.objects[key] = b"old remote"
    storage = LocalSubtitleStorage(tmp_path, remote)
    storage.put_bytes(key, b"new local", overwrite=False)
    storage.delete(key)
    assert remote.objects[key] == b"old remote"
    assert not remote.deleted
    assert not (tmp_path / key).exists()


def test_local_override_write_survives_old_remote_unavailability(subtitle_publication, monkeypatch, tmp_path):
    movie, remote = subtitle_publication
    Subtitle.create(movie=movie, file_path=subtitle_key(movie, 1))

    def unavailable(_key):
        raise StorageUnavailable("old remote offline")

    monkeypatch.setattr(remote, "open", unavailable)
    storage = LocalSubtitleStorage(tmp_path, remote)
    monkeypatch.setattr(subtitle_asset_service, "subtitle_storage", lambda: storage)
    result = import_content(movie)
    assert Subtitle.get_by_id(result.subtitle_id).file_path == subtitle_key(movie, 2)
    assert (tmp_path / subtitle_key(movie, 2)).read_bytes() == b"subtitle"
