import hashlib
import io
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
    StorageIntegrityError,
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
        self.attempted.append(key)
        if self.failure:
            if isinstance(self.failure, StoragePublicationUnknown):
                self.objects[key] = source.read_bytes()
                raise StoragePublicationUnknown(key, str(self.failure))
            raise self.failure
        if not overwrite and key in self.objects and self.objects[key] != source.read_bytes():
            raise StorageConflict(key)
        self.objects[key] = source.read_bytes()
        return PublicationResult(key, len(self.objects[key]), disposition="created")

    def put_zip(self, key, source, *, size, sha256):
        result = self.put_file(key, source)
        with self.open(key) as stream:
            content = stream.read()
        if self.stat(key).size != size or hashlib.sha256(content).hexdigest() != sha256:
            raise StorageIntegrityError("ZIP mismatch")
        return result

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
    from src.config import settings
    monkeypatch.setattr(settings.storage, "backend", "webdav")
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
    key = f"{ThumbnailArtifactService.thumbnail_prefix(media)}/generation.zip"
    storage.failure = StoragePublicationUnknown(key, "unknown") if unknown else StorageUnavailable("offline")
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert caught.value.available_count == 0 and caught.value.failed_count == 2
    assert caught.value.publication_possible is unknown
    assert Image.select().count() == 0
    assert not MediaThumbnail.select().exists()
    key = storage.attempted[0]
    assert len(storage.objects) == (1 if unknown else 0)
    old_keys = set(storage.attempted)
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    assert old_keys == set(storage.objects)
    assert len(storage.attempted) == 2


def test_database_failure_rolls_back_entire_generation(thumbnail_batch, monkeypatch):
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
    assert Image.select().count() == 0
    assert not MediaThumbnail.select().exists()
    assert len(storage.objects) == 1


def test_cleanup_failure_preserves_upload_error(thumbnail_batch, monkeypatch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StorageUnavailable("upload failed")

    def delete(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(storage, "delete", delete)
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert str(caught.value.__cause__) == "upload failed"
    assert Image.select().count() == 0


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
        from src.config import settings
        monkeypatch.setattr(settings.storage, "backend", "local")
        storage = LocalStorageBackend(tmp_path / "assets")
        monkeypatch.setattr("src.service.playback.thumbnails.artifacts.asset_storage", lambda: storage)
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    rows = ThumbnailArtifactService.list_media_thumbnails(media.id)
    assert [row.offset_seconds for row in rows] == [3, 6]
    assert all((row.width, row.height) == (320, 180) for row in rows)
    expected_status = MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED if video else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
    assert all(row.image_search_index_status == expected_status for row in MediaThumbnail.select())
    assert (tmp_path / "assets").exists() is not remote


def test_cleanup_keeps_referenced_remote_pack(thumbnail_batch):
    from src.common.image_store import thumbnail_generation_pack_key
    from src.service.catalog.image_cleanup_service import ImageCleanupService
    media, artifacts, storage = thumbnail_batch
    ThumbnailArtifactService.persist(media, artifacts)
    rows = list(MediaThumbnail.select())
    pack = thumbnail_generation_pack_key(rows[0].image.origin)
    for index, row in enumerate(rows):
        image = row.image
        row.delete_instance()
        keys = ImageCleanupService.delete_image_record_if_unused(image)
        ImageCleanupService.delete_obsolete_image_files(keys)
        assert (pack in storage.objects) is (index == 0)
        if index == 0:
            assert ThumbnailArtifactService.read_dimensions(rows[1].image.origin) == (320, 180)


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

    from starlette.requests import Request

    from src.api.routers.files import images as image_router
    from src.common.file_signatures import build_signed_image_url
    media, artifacts, storage = thumbnail_batch
    ThumbnailArtifactService.persist(media, artifacts)
    key = MediaThumbnail.get().image.origin
    url = urlsplit(build_signed_image_url(key))
    query = parse_qs(url.query)
    monkeypatch.setattr(storage, "local_path", lambda key: None, raising=False)
    monkeypatch.setattr(image_router, "asset_storage", lambda: storage)
    request = Request({"type": "http", "method": "GET", "path": url.path, "headers": []})
    response = image_router.get_image_file(request, key, expires=int(query["expires"][0]), signature=query["signature"][0])
    assert response.status_code == 200
    assert response.body == artifacts[0][1].read_bytes()
    assert response.headers["content-type"] == "image/webp"
    assert "max-age=" in response.headers["cache-control"]


def test_zip_publishes_all_rows_after_upload(thumbnail_batch, monkeypatch):
    from src.service.playback.thumbnails.batches import ThumbnailBatchStore
    media, artifacts, storage = thumbnail_batch
    original = storage.put_file
    def put_file(key, source, **kwargs):
        assert key.endswith(".zip")
        assert not MediaThumbnail.select().exists()
        return original(key, source, **kwargs)
    monkeypatch.setattr(storage, "put_file", put_file)
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        events = []
        assert ThumbnailArtifactService.persist_batch(media, batch, progress_callback=events.append) == 2
    assert len(storage.attempted) == 1
    assert len(events) == 2
    assert MediaThumbnail.select().count() == 2


def test_zip_failure_leaves_entire_generation_pending(thumbnail_batch):
    from src.service.playback.thumbnails.batches import ThumbnailBatchStore
    media, artifacts, storage = thumbnail_batch
    storage.failure = StorageUnavailable("offline", stage="move", status_code=500)
    with pytest.raises(ThumbnailPublicationIncomplete) as caught:
        ThumbnailArtifactService.persist(media, artifacts)
    assert caught.value.available_count == 0 and caught.value.failed_count == 2
    assert not MediaThumbnail.select().exists()
    batch = ThumbnailBatchStore(media).load()
    assert all(entry["state"] == "pending" for entry in batch.entries)
    assert batch.pack_file.is_file()
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempted) == 2


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
    assert len(storage.objects) == (0 if failure_check == 2 else 1)


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
    assert MediaThumbnail.select().count() == 2
    assert len(storage.objects) == 1


def test_unknown_publication_reuploads_saved_zip(thumbnail_batch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StoragePublicationUnknown("pending", "unknown")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    old_keys = set(storage.attempted)
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    assert set(storage.objects) == old_keys
    assert len(storage.attempted) == 2


@pytest.mark.parametrize("read_error", [StorageNotFound("GET missing"), StorageUnavailable("GET unavailable")])
def test_failed_full_read_keeps_zip_for_next_upload(thumbnail_batch, monkeypatch, read_error):
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
    assert not MediaThumbnail.select().exists()
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempted) == 3


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
    assert len(storage.objects) == (0 if failure_check == 2 else 1)


def test_connection_is_checked_before_upload_before_writes_and_before_commit(thumbnail_batch):
    from src.model import get_database

    media, artifacts, _ = thumbnail_batch
    checks = []
    ThumbnailArtifactService.persist(
        media, artifacts,
        check_connection=lambda: checks.append(get_database().in_transaction()),
    )
    assert checks == [False, False, False, True, False]


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
    assert len(storage.objects) == 1


def test_retry_replaces_failed_generation_zip(thumbnail_batch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StoragePublicationUnknown("unknown", "response lost")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    key = storage.attempted[0]
    storage.objects[key] = b"different content"
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert storage.objects[key] != b"different content"
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
