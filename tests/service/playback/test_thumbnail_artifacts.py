import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie, VideoItem
from src.plugins.provider_protocol import ThumbnailArtifact
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.storage.local import LocalStorageBackend
from src.storage.types import StoragePublicationUnknown, StorageUnavailable


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

    def put_file(self, key, source):
        if self.failure and key.endswith("/6.webp"):
            if isinstance(self.failure, StoragePublicationUnknown):
                self.objects[key] = source.read_bytes()
            raise self.failure
        self.objects[key] = source.read_bytes()

    def open(self, key):
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
        assert row.image.origin.endswith(f"/media/{media.id}/thumbnails/{row.offset}.webp")
        assert row.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
        assert ThumbnailArtifactService.read_dimensions(row.image.origin) == (320, 180)
    assert storage.objects["unrelated.webp"] == b"keep"
    assert artifacts[0][1].exists()


@pytest.mark.parametrize("unknown", [False, True])
def test_upload_failure_cleans_batch_and_can_retry(thumbnail_batch, unknown):
    media, artifacts, storage = thumbnail_batch
    key = f"{ThumbnailArtifactService.thumbnail_prefix(media)}/6.webp"
    storage.failure = StoragePublicationUnknown(key, "unknown") if unknown else StorageUnavailable("offline")
    with pytest.raises(StorageUnavailable):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not Image.select().exists()
    assert not MediaThumbnail.select().exists()
    assert storage.objects == ({key: artifacts[1][1].read_bytes()} if unknown else {})
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, artifacts) == 2


def test_database_failure_rolls_back_and_cleans_objects(thumbnail_batch, monkeypatch):
    media, artifacts, storage = thumbnail_batch
    original_create = MediaThumbnail.create

    def create(**kwargs):
        if kwargs["offset"] == 6:
            raise RuntimeError("database failed")
        return original_create(**kwargs)

    monkeypatch.setattr(MediaThumbnail, "create", create)
    with pytest.raises(RuntimeError, match="database failed"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not Image.select().exists()
    assert not MediaThumbnail.select().exists()
    assert storage.objects == {}


def test_cleanup_failure_preserves_upload_error(thumbnail_batch, monkeypatch):
    media, artifacts, storage = thumbnail_batch
    storage.failure = StorageUnavailable("upload failed")

    def delete(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(storage, "delete", delete)
    with pytest.raises(StorageUnavailable, match="upload failed"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not Image.select().exists()


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


def test_batch_waits_for_later_success_before_compensation(thumbnail_batch, monkeypatch):
    from src.config import settings

    media, artifacts, storage = thumbnail_batch
    later_started = threading.Event()
    first_failed = threading.Event()
    later_finished = threading.Event()
    original_delete = storage.delete

    def put_file(key, source):
        if key.endswith("/3.webp"):
            assert later_started.wait(5)
            first_failed.set()
            raise StorageUnavailable("first failed")
        later_started.set()
        assert first_failed.wait(5)
        storage.objects[key] = source.read_bytes()
        later_finished.set()

    def delete(key, **kwargs):
        assert later_finished.is_set()
        original_delete(key, **kwargs)

    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 2)
    monkeypatch.setattr(storage, "put_file", put_file)
    monkeypatch.setattr(storage, "delete", delete)
    with pytest.raises(StorageUnavailable, match="first failed"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert later_finished.is_set()
    assert not storage.objects
    assert not Image.select().exists()
    assert not MediaThumbnail.select().exists()


def test_database_writes_start_only_after_all_uploads(thumbnail_batch, monkeypatch):
    media, artifacts, storage = thumbnail_batch
    original_create = Image.create

    def create(**kwargs):
        assert len(storage.objects) == 2
        return original_create(**kwargs)

    monkeypatch.setattr(Image, "create", create)
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
