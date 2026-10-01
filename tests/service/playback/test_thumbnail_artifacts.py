import io
import threading
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie, VideoItem
from src.plugins.provider_protocol import ThumbnailArtifact
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.contracts import ThumbnailPublicationIncomplete
from src.storage.local import LocalStorageBackend
from src.storage.types import (
    ObjectStat,
    PublicationResult,
    StorageConflict,
    StorageNotFound,
    StoragePublicationUnknown,
    StorageUnavailable,
)


@pytest.mark.parametrize(
    "reference",
    [
        "https://images.example.test/thumbnail.webp?token=a%2Fb",
        " https://images.example.test/thumbnail.webp",
        "\udfff",
        "file:///tmp/thumbnail.webp",
        "https://[not-an-ipv6]/thumbnail.webp",
    ],
)
def test_read_dimensions_rejects_nonlocal_references_before_filesystem_io(
    reference, monkeypatch
):
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.asset_storage",
        lambda: pytest.fail("constructed a local image path"),
    )
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.PILImage.open",
        lambda *_args, **_kwargs: pytest.fail("opened a nonlocal image reference"),
    )

    with pytest.raises(ValueError, match="thumbnail_image_reference_nonlocal"):
        ThumbnailArtifactService.read_dimensions(reference)


def test_read_dimensions_opens_internal_thumbnail_key_unchanged(tmp_path, monkeypatch):
    thumbnail = tmp_path / "videos/7/media/11/thumbnails/3.webp"
    thumbnail.parent.mkdir(parents=True)
    PILImage.new("RGB", (320, 180)).save(thumbnail, format="WEBP")
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.asset_storage",
        lambda: LocalStorageBackend(tmp_path),
    )

    assert ThumbnailArtifactService.read_dimensions(
        "videos/7/media/11/thumbnails/3.webp"
    ) == (320, 180)


class RemoteStorage:
    def __init__(self):
        self.objects = {}
        self.failure = None
        self.attempted = []

    def put_file(self, key, source, *, overwrite=True, immutable=False):
        assert overwrite is False
        self.attempted.append(key)
        if self.failure and key.endswith("/6.webp"):
            if isinstance(self.failure, StoragePublicationUnknown):
                self.objects[key] = source.read_bytes()
                raise StoragePublicationUnknown(key, str(self.failure))
            raise self.failure
        if key in self.objects and self.objects[key] != source.read_bytes():
            raise StorageConflict(key)
        self.objects[key] = source.read_bytes()
        return PublicationResult(key, len(self.objects[key]), disposition="created")

    def stat(self, key):
        if key not in self.objects:
            raise StorageNotFound(key)
        return ObjectStat(key, len(self.objects[key]))

    def open(self, key):
        self.stat(key)
        return io.BytesIO(self.objects[key])

    def delete(self, key, *, missing_ok=True):
        self.objects.pop(key, None)


@pytest.fixture
def thumbnail_batch(test_db, tmp_path, monkeypatch):
    movie = Movie.create(movie_number="THUMB-001", javdb_id="thumb-1", title="movie")
    library = MediaLibrary.create(name="thumbnail", provider_key="test", provider_config={})
    media = Media.create(movie=movie, library=library, file_name="test.mp4")
    source = tmp_path / "source.webp"
    PILImage.new("RGB", (320, 180)).save(source, format="WEBP")
    artifacts = [(ThumbnailArtifact(offset, "source.webp"), source) for offset in (3, 6)]
    storage = RemoteStorage()
    monkeypatch.setattr("src.service.playback.thumbnails.artifacts.asset_storage", lambda: storage)
    monkeypatch.setattr("src.service.catalog.image_cleanup_service.asset_storage", lambda: storage)
    return media, artifacts, storage


def test_remote_publication_and_dimensions(thumbnail_batch):
    media, artifacts, storage = thumbnail_batch
    storage.objects["unrelated.webp"] = b"keep"
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    rows = list(MediaThumbnail.select().order_by(MediaThumbnail.offset))
    assert [row.offset for row in rows] == [3, 6]
    for row in rows:
        path = PurePosixPath(row.image.origin)
        assert path.parent.parent.as_posix() == ThumbnailArtifactService.thumbnail_prefix(media)
        assert len(path.parent.name) == 32
        assert path.name == f"{row.offset}.webp"
        assert row.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
        assert ThumbnailArtifactService.read_dimensions(row.image.origin) == (320, 180)
    assert storage.objects["unrelated.webp"] == b"keep"
    assert artifacts[0][1].exists()


@pytest.mark.parametrize("unknown", [False, True])
def test_upload_failure_retains_batch_and_can_retry(thumbnail_batch, unknown):
    media, artifacts, storage = thumbnail_batch
    key = f"{ThumbnailArtifactService.thumbnail_prefix(media)}/6.webp"
    storage.failure = StoragePublicationUnknown(key, "unknown") if unknown else StorageUnavailable("offline")
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert caught.value.available_count == caught.value.failed_count == 1
    assert caught.value.publication_possible is unknown
    assert Image.select().count() == 1
    assert [row.offset for row in MediaThumbnail.select()] == [3]
    key = next(key for key in storage.attempted if key.endswith("/6.webp"))
    assert len(storage.objects) == (2 if unknown else 1)
    old_keys = set(storage.attempted)
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    assert old_keys == {image.origin for image in Image.select()}
    assert len(storage.attempted) == (2 if unknown else 3)


def test_database_failure_keeps_previously_committed_thumbnails(thumbnail_batch, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 1)
    media, artifacts, storage = thumbnail_batch
    original_create = MediaThumbnail.create

    def create(**kwargs):
        if kwargs["offset"] == 6:
            raise RuntimeError("database failed")
        return original_create(**kwargs)

    monkeypatch.setattr(MediaThumbnail, "create", create)
    with pytest.raises(RuntimeError, match="database failed"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert Image.select().count() == 1
    assert [row.offset for row in MediaThumbnail.select()] == [3]
    assert len(storage.objects) == 2


def test_cleanup_failure_preserves_upload_error(thumbnail_batch, monkeypatch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StorageUnavailable("upload failed")

    def delete(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(storage, "delete", delete)
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert str(caught.value.__cause__) == "upload failed"
    assert Image.select().count() == 1


def test_video_prefix():
    assert ThumbnailArtifactService.thumbnail_prefix(
        SimpleNamespace(movie_number=None, video_item_id=7, id=11)
    ) == "videos/7/media/11/thumbnails"


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("video", [False, True])
def test_persist_backend_and_media_kinds(thumbnail_batch, monkeypatch, tmp_path, remote, video):
    media, artifacts, storage = thumbnail_batch
    if video:
        media.movie = None
        media.video_item = VideoItem.create(title="video")
        media.save()
    if not remote:
        storage = LocalStorageBackend(tmp_path / "assets")
        monkeypatch.setattr("src.service.playback.thumbnails.artifacts.asset_storage", lambda: storage)
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    rows = ThumbnailArtifactService.list_media_thumbnails(media.id)
    assert [row.offset_seconds for row in rows] == [3, 6]
    assert all((row.width, row.height) == (320, 180) for row in rows)
    expected_status = MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED if video else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
    assert all(row.image_search_index_status == expected_status for row in MediaThumbnail.select())
    assert (tmp_path / "assets").exists() is not remote


def test_cleanup_keeps_referenced_remote_image(thumbnail_batch):
    from src.service.catalog.image_cleanup_service import ImageCleanupService

    media, artifacts, storage = thumbnail_batch
    ThumbnailArtifactService.persist(media, artifacts)
    row = MediaThumbnail.get()
    key = row.image.origin
    ImageCleanupService.delete_obsolete_image_files({key})
    assert key in storage.objects
    image = row.image
    row.delete_instance()
    keys = ImageCleanupService.delete_image_record_if_unused(image)
    ImageCleanupService.delete_obsolete_image_files(keys)
    assert key not in storage.objects


def test_media_delete_removes_remote_thumbnails(thumbnail_batch, monkeypatch):
    from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
    from src.service.playback import media_service

    media, artifacts, storage = thumbnail_batch
    ThumbnailArtifactService.persist(media, artifacts)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for",
        lambda _handle: SimpleNamespace(delete_media=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        media_service, "get_qdrant_thumbnail_store",
        lambda: SimpleNamespace(delete_by_media_id=lambda _id: None),
    )
    media_service.MediaService.delete_media(media.id)
    assert not storage.objects
    assert not Image.select().exists()
    assert not MediaThumbnail.select().exists()


def test_published_thumbnail_signed_access(thumbnail_batch, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from fastapi import Response
    from starlette.requests import Request

    from src.api.routers.files import images as image_router
    from src.common.file_signatures import build_signed_image_url

    media, artifacts, storage = thumbnail_batch
    ThumbnailArtifactService.persist(media, artifacts)
    key = MediaThumbnail.get().image.origin
    url = urlsplit(build_signed_image_url(key))
    query = parse_qs(url.query)

    def range_response(requested_key, range_header, content_type):
        assert requested_key == key
        return Response(content=storage.objects[requested_key], media_type="image/webp")

    monkeypatch.setattr(image_router, "asset_storage", lambda: SimpleNamespace(
        local_path=lambda _key: None, range_response=range_response,
    ))
    request = Request({"type": "http", "method": "GET", "path": url.path, "headers": []})
    response = image_router.get_image_file(
        request, key, expires=int(query["expires"][0]), signature=query["signature"][0],
    )
    assert response.status_code == 200
    assert response.body == artifacts[0][1].read_bytes()


def test_batch_waits_for_later_success_and_checkpoints_it(thumbnail_batch, monkeypatch):
    from src.config import settings

    media, artifacts, storage = thumbnail_batch
    later_started = threading.Event()
    first_failed = threading.Event()
    later_finished = threading.Event()
    original_delete = storage.delete

    def put_file(key, source, *, overwrite, immutable):
        assert overwrite is False
        if key.endswith("/3.webp"):
            assert later_started.wait(5)
            first_failed.set()
            raise StorageUnavailable("first failed")
        later_started.set()
        assert first_failed.wait(5)
        storage.objects[key] = source.read_bytes()
        later_finished.set()
        return PublicationResult(key, len(storage.objects[key]), disposition="created")

    def delete(key, **kwargs):
        assert later_finished.is_set()
        original_delete(key, **kwargs)

    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 2)
    monkeypatch.setattr(storage, "put_file", put_file)
    monkeypatch.setattr(storage, "delete", delete)
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert str(caught.value.__cause__) == "first failed"
    assert later_finished.is_set()
    assert len(storage.objects) == 1
    assert Image.select().count() == 1
    assert [row.offset for row in MediaThumbnail.select()] == [6]


def test_successful_thumbnail_is_usable_while_other_upload_is_running(thumbnail_batch, monkeypatch):
    from src.config import settings
    from src.model import get_database

    media, artifacts, storage = thumbnail_batch
    visible = threading.Event()
    later_finished = threading.Event()
    original_put = storage.put_file
    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 2)

    def put_file(key, source, **kwargs):
        if key.endswith("/6.webp"):
            assert visible.wait(5), "first thumbnail was not committed before the next upload finished"
            later_finished.set()
        return original_put(key, source, **kwargs)

    def progress(_text):
        if visible.is_set():
            return
        thumbnail = MediaThumbnail.get_or_none(MediaThumbnail.offset == 3)
        if thumbnail is not None:
            assert not get_database().in_transaction()
            assert not later_finished.is_set()
            assert ThumbnailArtifactService.read_dimensions(thumbnail.image.origin) == (320, 180)
            visible.set()

    monkeypatch.setattr(storage, "put_file", put_file)
    from src.service.playback.thumbnails.batches import ThumbnailBatchStore

    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        assert ThumbnailArtifactService.persist_batch(media, batch, progress_callback=progress) == 2
    assert visible.is_set() and later_finished.is_set()


def test_failed_middle_file_does_not_block_later_uploads(thumbnail_batch, monkeypatch):
    from src.config import settings
    from src.service.playback.thumbnails.batches import ThumbnailBatchStore

    media, artifacts, storage = thumbnail_batch
    source = artifacts[0][1]
    artifacts += [(ThumbnailArtifact(offset, "source.webp"), source) for offset in (9, 12)]
    storage.failure = StorageUnavailable("offline", stage="move", status_code=500)
    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 1)
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert caught.value.available_count == 3 and caught.value.failed_count == 1
    assert [PurePosixPath(key).name for key in storage.attempted] == ["3.webp", "6.webp", "9.webp", "12.webp"]
    assert [row.offset for row in MediaThumbnail.select().order_by(MediaThumbnail.offset)] == [3, 9, 12]
    store = ThumbnailBatchStore(media)
    batch = store.load()
    failed = next(entry for entry in batch.entries if entry["offset"] == 6)
    assert failed["last_error"]["status_code"] == 500
    assert failed["state"] == "uploading"
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, []) == 4
    assert len(storage.attempted) == 5 and storage.attempted[-1].endswith("/6.webp")
    assert MediaThumbnail.select().count() == Image.select().count() == 4
    assert not store.manifest_path.exists()


@pytest.mark.parametrize("failure_check", [2, 3])
def test_lost_media_lock_prevents_thumbnail_commit(thumbnail_batch, failure_check):
    media, artifacts, storage = thumbnail_batch
    checks = 0

    def check_connection():
        nonlocal checks
        checks += 1
        if checks == failure_check:
            raise RuntimeError("media operation lock lost")

    with pytest.raises(RuntimeError, match="lock lost"):
        ThumbnailArtifactService.persist(media, artifacts, check_connection=check_connection)
    assert checks == failure_check
    assert not Image.select().exists()
    assert not MediaThumbnail.select().exists()
    assert len(storage.objects) == 2


def test_commit_response_loss_retains_published_thumbnails(thumbnail_batch, monkeypatch, test_db):
    from contextlib import contextmanager

    from peewee import OperationalError

    from src.service.playback.thumbnails import artifacts as module

    media, artifacts, storage = thumbnail_batch

    @contextmanager
    def atomic():
        with test_db.atomic():
            yield
        raise OperationalError("commit response lost")

    monkeypatch.setattr(module, "get_database", lambda: SimpleNamespace(atomic=atomic))
    with pytest.raises(OperationalError, match="commit response lost"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert MediaThumbnail.select().count() == 1
    assert {image.origin for image in Image.select()}.issubset(storage.objects)


def test_unknown_publication_is_reconciled_without_reupload(thumbnail_batch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StoragePublicationUnknown("pending", "unknown")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    old_keys = set(storage.attempted)
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    assert {image.origin for image in Image.select()} == old_keys
    assert len(storage.attempted) == 2


@pytest.mark.parametrize("read_error", [StorageNotFound("GET missing"), StorageUnavailable("GET unavailable")])
def test_existing_unknown_object_read_failure_never_triggers_reupload(thumbnail_batch, monkeypatch, read_error):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StoragePublicationUnknown("pending", "unknown")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    storage.failure = None
    with monkeypatch.context() as patch:
        def unreadable(_key):
            raise read_error

        patch.setattr(storage, "open", unreadable)
        with pytest.raises(ThumbnailPublicationIncomplete) as caught:
            ThumbnailArtifactService.persist(media, [])
        assert caught.value.__cause__ is read_error
    assert len(storage.attempted) == 2
    assert [row.offset for row in MediaThumbnail.select()] == [3]
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempted) == 2


def test_lost_media_lock_does_not_update_thumbnail_task_state(monkeypatch):
    from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService

    def generate(*args, **kwargs):
        raise StorageUnavailable("upload unavailable")

    def lost():
        raise RuntimeError("media operation lock lost")

    monkeypatch.setattr(MediaThumbnailTaskService, "_generate_artifacts", generate)
    monkeypatch.setattr(MediaThumbnailTaskService, "_has_thumbnails", lambda *_: pytest.fail("stale worker queried state"))
    monkeypatch.setattr(MediaThumbnailTaskService, "_mark_failure", lambda *_: pytest.fail("stale worker wrote state"))
    with pytest.raises(RuntimeError, match="lock lost"):
        MediaThumbnailTaskService._generate_loaded_media(SimpleNamespace(id=1), check_connection=lost)


@pytest.mark.parametrize("failure_check", [2, 3])
def test_lost_lock_prevents_thumbnail_commit(thumbnail_batch, failure_check):
    media, artifacts, storage = thumbnail_batch
    calls = []

    def check_connection():
        calls.append(True)
        if len(calls) == failure_check:
            raise RuntimeError("media_operation_connection_lost")

    with pytest.raises(RuntimeError, match="connection_lost"):
        ThumbnailArtifactService.persist(media, artifacts, check_connection=check_connection)
    assert not MediaThumbnail.select().exists()
    assert not Image.select().exists()
    assert len(storage.objects) == 2


def test_connection_is_checked_before_upload_before_writes_and_before_commit(thumbnail_batch):
    from src.model import get_database

    media, artifacts, _ = thumbnail_batch
    checks = []
    ThumbnailArtifactService.persist(
        media, artifacts,
        check_connection=lambda: checks.append(get_database().in_transaction()),
    )
    assert checks == [False, False, True, False, True, False]


def test_database_connection_failure_retains_published_objects(thumbnail_batch, monkeypatch):
    from peewee import OperationalError

    media, artifacts, storage = thumbnail_batch

    def create(**kwargs):
        raise OperationalError("connection lost during database operation")

    monkeypatch.setattr(MediaThumbnail, "create", create)
    with pytest.raises(OperationalError):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not MediaThumbnail.select().exists()
    assert not Image.select().exists()
    assert len(storage.objects) == 2


def test_unknown_publication_with_wrong_content_is_not_overwritten(thumbnail_batch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StoragePublicationUnknown("unknown", "response lost")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    key = next(key for key in storage.attempted if key.endswith("/6.webp"))
    storage.objects[key] = b"different content"
    storage.failure = None
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert isinstance(caught.value.__cause__, StorageConflict)
    assert [row.offset for row in MediaThumbnail.select()] == [3]
    assert storage.objects[key] == b"different content"
    assert len(storage.attempted) == 2


@pytest.mark.parametrize("generation_failed", [False, True])
def test_worker_that_lost_lock_cannot_update_task_state(monkeypatch, generation_failed):
    from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService

    def generate(media, progress_callback, *, check_connection):
        if generation_failed:
            raise StorageUnavailable("upload failed")
        return 2

    def lost_connection():
        raise RuntimeError("media_operation_connection_lost")

    monkeypatch.setattr(MediaThumbnailTaskService, "_generate_artifacts", generate)
    monkeypatch.setattr(MediaThumbnailTaskService, "_mark_succeeded", lambda *args: pytest.fail("stale success write"))
    monkeypatch.setattr(MediaThumbnailTaskService, "_mark_failure", lambda *args: pytest.fail("stale failure write"))
    monkeypatch.setattr(MediaThumbnailTaskService, "_has_thumbnails", lambda *args: pytest.fail("stale database read"))
    with pytest.raises(RuntimeError, match="connection_lost"):
        MediaThumbnailTaskService._generate_loaded_media(
            SimpleNamespace(id=1), check_connection=lost_connection,
        )
