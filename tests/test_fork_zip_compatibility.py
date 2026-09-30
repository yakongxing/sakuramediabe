import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import OperationalError
from PIL import Image as PILImage

from src.common.image_store import image_pack_path, read_image_bytes, write_pack
from src.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import ThumbnailArtifact
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.service.catalog.movie_asset_pack_backfill_service import (
    MovieAssetPackBackfillService,
)
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.playback.media_thumbnail_pack_backfill_service import (
    MediaThumbnailPackBackfillService,
)
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.batches import ThumbnailBatchStore
from src.storage import asset_storage
from src.storage.types import (
    ObjectStat,
    PublicationResult,
    StorageNotFound,
    StorageUnavailable,
)


class RemoteStorage:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail = False

    def local_path(self, key):
        return None

    def stat(self, key):
        if key not in self.objects:
            raise StorageNotFound(key)
        return ObjectStat(key, len(self.objects[key]))

    def open(self, key):
        self.stat(key)
        return io.BytesIO(self.objects[key])

    def put_file(self, key, source, **kwargs):
        assert kwargs == {"overwrite": False, "immutable": True}
        self.uploads.append(key)
        if self.fail and key.endswith("/6.webp"):
            raise StorageUnavailable("offline")
        self.objects[key] = source.read_bytes()
        return PublicationResult(key, len(self.objects[key]), disposition="created")

    def delete(self, *args, **kwargs):
        pytest.fail("remote rollback attempted")


@pytest.fixture
def remote(isolated_local_storage, monkeypatch):
    storage = RemoteStorage()
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.storage, "webdav_base_url", "https://dav.example.test/dav")
    for module in ("src.common.image_store", "src.service.playback.thumbnails.artifacts", "src.service.catalog.image_cleanup_service"):
        monkeypatch.setattr(f"{module}.asset_storage", lambda: storage)
    return storage


@pytest.fixture
def thumbnails(test_db, tmp_path):
    movie = Movie.create(movie_number="ZIP-001", javdb_id="zip-1", title="movie")
    library = MediaLibrary.create(name="zip", provider_key="fake", provider_config={})
    media = Media.create(movie=movie, library=library, file_name="video.mp4")
    source = tmp_path / "source.webp"
    PILImage.new("RGB", (32, 18)).save(source, "WEBP")
    return media, [(ThumbnailArtifact(offset, "source.webp"), source) for offset in (3, 6)]


def test_remote_reads_ignore_local_zip_and_loose_cache(remote, monkeypatch):
    key = "movies/aa/ZIP-001/media/1/thumbnails/3.webp"
    path = Path(settings.media.import_image_root_path) / key
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stale loose")
    write_pack(path.parent.with_suffix(".zip"), [("3.webp", b"stale packed")])
    remote.objects[key] = b"current remote"
    monkeypatch.setattr("src.common.image_store.zipfile.ZipFile", lambda *args, **kwargs: pytest.fail("opened local ZIP"))
    assert image_pack_path(key) is None
    assert read_image_bytes(key) == b"current remote"
    remote.objects.clear()
    with pytest.raises(FileNotFoundError):
        read_image_bytes(key)


@pytest.mark.parametrize("service,candidate,process", [
    (MovieAssetPackBackfillService, "_candidate_movie_numbers", "_process_movie"),
    (MediaThumbnailPackBackfillService, "_candidate_media_ids", "_process_media"),
])
def test_remote_backfills_refuse_before_scanning(remote, monkeypatch, service, candidate, process):
    monkeypatch.setattr(service, candidate, lambda: pytest.fail("scanned database"))
    with pytest.raises(RuntimeError, match="image_pack_requires_local_storage"):
        service.backfill(reporter=SimpleNamespace(emit=lambda **kwargs: pytest.fail("emitted progress")))
    with pytest.raises(RuntimeError, match="image_pack_requires_local_storage"):
        getattr(service, process)(1, {})
    assert remote.uploads == []
    assert not Path(settings.media.import_image_root_path).exists()


def test_remote_catalog_pack_hooks_do_not_touch_local_storage(remote, monkeypatch):
    monkeypatch.setattr(MovieAssetPackService, "_pack_lock", lambda *args: pytest.fail("local lock"))
    assert MovieAssetPackService.rebuild_movie_asset_pack("movies/aa/ZIP-001") is False
    MovieAssetPackService.remove_movie_asset_pack("movies/aa/ZIP-001")
    assert not Path(settings.media.import_image_root_path).exists()


def test_remote_new_batches_remain_loose_and_resume(remote, thumbnails, monkeypatch):
    media, artifacts = thumbnails
    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 1)
    remote.fail = True
    with pytest.raises(StorageUnavailable):
        ThumbnailArtifactService.persist(media, artifacts)
    batch = ThumbnailBatchStore(media).load()
    assert not batch.packed
    assert len(remote.objects) == 1
    remote.fail = False
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(remote.uploads) == 3
    assert all(key.endswith(".webp") for key in remote.uploads)
    assert not list(Path(settings.media.thumbnail_staging_root_path).rglob("*.zip"))


def test_local_new_batches_use_isolated_zip(isolated_local_storage, thumbnails):
    media, artifacts = thumbnails
    assert ThumbnailArtifactService.persist(media, artifacts) == 2
    rows = list(MediaThumbnail.select().order_by(MediaThumbnail.offset))
    packs = {image_pack_path(row.image.origin) for row in rows}
    assert len(packs) == 1
    pack = packs.pop()
    assert pack.is_file()
    assert len(pack.stem) == 32
    with zipfile.ZipFile(pack) as archive:
        assert archive.namelist() == ["3.webp", "6.webp"]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
    for row in rows:
        assert not (Path(settings.media.import_image_root_path) / row.image.origin).exists()
        assert read_image_bytes(row.image.origin) == artifacts[0][1].read_bytes()
    assert ThumbnailBatchStore(media).load() is None


def test_local_zip_database_failure_keeps_batch_for_commit_retry(isolated_local_storage, thumbnails, monkeypatch):
    media, artifacts = thumbnails
    with monkeypatch.context() as patch:
        patch.setattr(MediaThumbnail, "create", lambda **kwargs: (_ for _ in ()).throw(OperationalError("offline")))
        with pytest.raises(OperationalError):
            ThumbnailArtifactService.persist(media, artifacts)
    batch = ThumbnailBatchStore(media).load()
    assert batch.packed and batch.workspace.exists()
    assert not Image.select().exists()
    monkeypatch.setattr(asset_storage(), "put_file", lambda *args, **kwargs: pytest.fail("republished ZIP"))
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert not batch.workspace.exists()


def test_legacy_local_batch_keeps_loose_format(isolated_local_storage, thumbnails):
    media, artifacts = thumbnails
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        batch.manifest["version"] = 1
        del batch.manifest["format"]
        entry = batch.entries[0]
        asset_storage().put_file(batch.key(ThumbnailArtifactService.thumbnail_prefix(media), entry), batch.source(entry), overwrite=False)
        batch.checkpoint(entry, "uploaded")
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert not list(Path(settings.media.import_image_root_path).rglob("*.zip"))
    assert len(list(Path(settings.media.import_image_root_path).rglob("*.webp"))) == 2


def test_local_zip_cleanup_keeps_other_referenced_entries(isolated_local_storage, thumbnails):
    media, artifacts = thumbnails
    ThumbnailArtifactService.persist(media, artifacts)
    removed, retained = list(MediaThumbnail.select().order_by(MediaThumbnail.offset))
    obsolete = removed.image
    key = obsolete.origin
    removed.delete_instance()
    obsolete.delete_instance()
    ImageCleanupService.delete_obsolete_image_files({key})
    assert read_image_bytes(retained.image.origin) == artifacts[0][1].read_bytes()
    with zipfile.ZipFile(image_pack_path(key)) as archive:
        assert archive.namelist() == ["6.webp"]


def test_movie_pack_does_not_delete_unpublished_or_nonimage_files(test_db, isolated_local_storage):
    root = Path(settings.media.import_image_root_path)
    movie_dir = root / "movies/aa/ZIP-001"
    movie_dir.mkdir(parents=True)
    (movie_dir / "cover.webp").write_bytes(b"cover")
    (movie_dir / "pending.webp").write_bytes(b"not committed")
    (movie_dir / "notes.srt").write_bytes(b"subtitle")
    Image.create(origin="movies/aa/ZIP-001/cover.webp")
    assert MovieAssetPackService.rebuild_movie_asset_pack("movies/aa/ZIP-001")
    assert (movie_dir / "pending.webp").read_bytes() == b"not committed"
    assert (movie_dir / "notes.srt").read_bytes() == b"subtitle"
    assert read_image_bytes("movies/aa/ZIP-001/cover.webp") == b"cover"
