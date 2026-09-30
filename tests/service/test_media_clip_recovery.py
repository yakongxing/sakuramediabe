import io
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import (
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    get_database,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY, ClipArtifact
from src.schema.playback.clips import MediaClipCreateRequest
from src.service.playback import media_clip_service
from src.service.playback.media_clip_service import MediaClipService
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.storage.types import ObjectStat, StorageNotFound, StoragePublicationUnknown


def test_invalid_clip_placeholder_is_removed_and_regenerated(
    test_db,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    library = MediaLibrary.create(name="clip-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="CLIP-001", javdb_id="clip-1", title="clip")
    media = Media.create(movie=movie, library=library, file_name="clip.mp4")
    first_image = Image.create(origin="clip-first.webp")
    second_image = Image.create(origin="clip-second.webp")
    start_thumbnail = MediaThumbnail.create(media=media, image=first_image, offset=0)
    end_thumbnail = MediaThumbnail.create(media=media, image=second_image, offset=10)
    stale = MediaClip.create(
        media=media,
        movie_number=movie.movie_number,
        start_offset_seconds=0,
        end_offset_seconds=10,
        file_path="",
        file_size_bytes=0,
        duration_seconds=0,
    )

    class Storage:
        def create_clip(self, *, workspace, **_kwargs):
            (workspace / "clip.mp4").write_bytes(b"valid clip")
            return ClipArtifact(relative_path="clip.mp4")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    monkeypatch.setattr(
        media_clip_service.MediaMetadataProbeService,
        "probe_file",
        lambda _path: SimpleNamespace(duration_seconds=10),
    )

    resource, created = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(
            start_thumbnail_id=start_thumbnail.id,
            end_thumbnail_id=end_thumbnail.id,
            title="clip",
        ),
    )

    assert created is True
    assert resource.clip_id != stale.id
    assert MediaClip.get_or_none(MediaClip.id == stale.id) is None
    assert MediaClipService.list_media_clips().total == 1


class ClipStorage:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_file(self, key, source, *, overwrite):
        assert overwrite is False
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = source.read_bytes()
        return SimpleNamespace(key=key, size=len(self.objects[key]), created=True)

    def stat(self, key):
        if key not in self.objects:
            raise StorageNotFound(key)
        return ObjectStat(key, len(self.objects[key]))

    def open(self, key):
        return io.BytesIO(self.objects[key])

    def delete(self, key, *, missing_ok=True):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def clip_publication(test_db, monkeypatch):
    library = MediaLibrary.create(name="clips", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="CLIP-001", javdb_id="clip-1", title="clip")
    media = Media.create(movie=movie, library=library, file_name="clip.mp4")
    thumbnails = []
    for offset in (0, 10):
        key = f"clip-{offset}.webp"
        image = Image.create(origin=key, small=key, medium=key, large=key)
        thumbnails.append(MediaThumbnail.create(media=media, image=image, offset=offset))
    payload = MediaClipCreateRequest(
        start_thumbnail_id=thumbnails[0].id, end_thumbnail_id=thumbnails[1].id,
        title="clip",
    )

    class Provider:
        def create_clip(self, *, workspace, **_kwargs):
            (workspace / "clip.mp4").write_bytes(b"valid clip")
            return ClipArtifact(relative_path="clip.mp4")

    storage = ClipStorage()
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Provider())
    monkeypatch.setattr(media_clip_service, "clip_storage", lambda: storage)
    monkeypatch.setattr(
        media_clip_service.MediaMetadataProbeService, "probe_file",
        lambda _path: SimpleNamespace(duration_seconds=10),
    )
    return media, payload, storage


def test_unknown_clip_upload_preserves_final_object(clip_publication, monkeypatch):
    media, payload, storage = clip_publication
    original = storage.put_file

    def unknown(key, *args, **kwargs):
        original(key, *args, **kwargs)
        raise StoragePublicationUnknown(key, "response lost")

    monkeypatch.setattr(storage, "put_file", unknown)
    with pytest.raises(ApiError) as error:
        MediaClipService.create_clip(media.id, payload)
    assert error.value.status_code == 503
    assert len(storage.objects) == 1
    assert not storage.deleted
    assert not MediaClip.select().exists()


def test_failed_clip_database_update_compensates_confirmed_new_object(clip_publication, monkeypatch):
    media, payload, storage = clip_publication
    original_save = MediaClip.save

    def failed_save(clip, *args, **kwargs):
        if clip.file_path:
            raise RuntimeError("database update failed")
        return original_save(clip, *args, **kwargs)

    monkeypatch.setattr(MediaClip, "save", failed_save)
    with pytest.raises(ApiError):
        MediaClipService.create_clip(media.id, payload)
    assert len(storage.deleted) == 1
    assert not storage.objects
    assert not MediaClip.select().exists()


def test_clip_commit_acknowledgement_loss_keeps_registered_object(clip_publication, monkeypatch):
    media, payload, storage = clip_publication
    database = get_database()
    original_atomic = database.atomic

    @contextmanager
    def lost_acknowledgement(*args, **kwargs):
        with original_atomic(*args, **kwargs):
            yield
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(database, "atomic", lost_acknowledgement)
    with pytest.raises(ApiError):
        MediaClipService.create_clip(media.id, payload)
    clip = MediaClip.get()
    assert clip.file_path in storage.objects
    assert clip.file_size_bytes == len(b"valid clip")
    assert not storage.deleted
    resource, created = MediaClipService.create_clip(media.id, payload)
    assert created is False and resource.clip_id == clip.id


def test_clip_lock_connection_loss_prevents_registration_and_cleanup(clip_publication, monkeypatch):
    media, payload, storage = clip_publication
    original_put = storage.put_file

    def lose_connection(*args, **kwargs):
        result = original_put(*args, **kwargs)
        get_database().close()
        return result

    monkeypatch.setattr(storage, "put_file", lose_connection)
    with pytest.raises(ApiError):
        MediaClipService.create_clip(media.id, payload)
    assert MediaClip.get().file_path == ""
    assert len(storage.objects) == 1
    assert not storage.deleted


def _placeholder(media):
    return MediaClip.create(
        media=media, movie_number=media.movie_number,
        start_offset_seconds=0, end_offset_seconds=10,
        file_path="", file_size_bytes=0, duration_seconds=0,
    )


def test_busy_placeholder_is_not_removed_by_listing_or_delete(clip_publication):
    media, _, storage = clip_publication
    clip = _placeholder(media)
    ready, release = Event(), Event()
    errors = []

    def hold_lock():
        try:
            with get_database().connection_context(), media_operation_lock(MEDIA_LOCK, media.id):
                ready.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    thread = Thread(target=hold_lock)
    thread.start()
    try:
        assert ready.wait(10)
        assert MediaClipService.valid_clips([clip]) == []
        assert MediaClip.get_by_id(clip.id).file_path == ""
        with pytest.raises(MediaOperationBusy):
            MediaClipService.delete_clip(clip.id)
        assert not storage.deleted
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


def test_invalid_snapshot_is_refreshed_after_acquiring_lock(clip_publication):
    media, _, storage = clip_publication
    snapshot = _placeholder(media)
    key = MediaClipService._clip_relative_path(media.movie_number, snapshot.id)
    storage.objects[key] = b"valid clip"
    MediaClip.update(file_path=key, file_size_bytes=10, duration_seconds=10).where(MediaClip.id == snapshot.id).execute()
    result = MediaClipService.valid_clips([snapshot])
    assert len(result) == 1 and result[0].file_path == key
    assert not storage.deleted


def test_stale_placeholder_cleanup_does_not_guess_unknown_remote_key(clip_publication):
    media, _, storage = clip_publication
    clip = _placeholder(media)
    key = MediaClipService._clip_relative_path(media.movie_number, clip.id)
    storage.objects[key] = b"possibly still uploading"
    assert MediaClipService.valid_clips([clip]) == []
    assert not MediaClip.select().exists()
    assert storage.objects[key] == b"possibly still uploading"
    assert not storage.deleted


def test_orphan_clip_delete_uses_independent_lock(clip_publication):
    media, payload, storage = clip_publication
    resource, _ = MediaClipService.create_clip(media.id, payload)
    media.delete_instance()
    assert MediaClip.get_by_id(resource.clip_id).media_id is None
    MediaClipService.delete_clip(resource.clip_id)
    assert not MediaClip.select().exists()
    assert not storage.objects
