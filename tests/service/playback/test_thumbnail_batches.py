import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from peewee import OperationalError
from PIL import Image as PILImage

from src.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ThumbnailArtifact,
    ThumbnailGeneration,
)
from src.service.playback.operation_locks import MediaOperationBusy
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.batches import ThumbnailBatch, ThumbnailBatchStore
from src.service.playback.thumbnails.contracts import ThumbnailPublicationIncomplete
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService
from src.storage.local import LocalStorageBackend
from src.storage.types import StoragePublicationUnknown, StorageUnavailable


class RecordingStorage(LocalStorageBackend):
    def __init__(self, root):
        super().__init__(root)
        self.attempts = []
        self.failure = None
        self.unknown = False

    def put_file(self, key, source, **kwargs):
        self.attempts.append(key)
        if self.failure:
            if self.unknown:
                super().put_file(key, source, **kwargs)
                raise StoragePublicationUnknown(key, "response lost")
            raise self.failure
        return super().put_file(key, source, **kwargs)

    def delete(self, *args, **kwargs):
        pytest.fail("failed thumbnail publication must not delete remote files")


@pytest.fixture
def publication(test_db, tmp_path, monkeypatch):
    movie = Movie.create(movie_number="RESUME-001", javdb_id="resume-1", title="movie")
    library = MediaLibrary.create(name="resume", provider_key="fake", provider_config={})
    media = Media.create(movie=movie, library=library, file_name="video.mp4")
    source = tmp_path / "source.webp"
    PILImage.new("RGB", (32, 18)).save(source, "WEBP")
    artifacts = [(ThumbnailArtifact(offset, "source.webp"), source) for offset in (3, 6)]
    storage = RecordingStorage(tmp_path / "remote")
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.storage, "webdav_base_url", "https://dav.example.test/dav")
    monkeypatch.setattr("src.service.playback.thumbnails.artifacts.asset_storage", lambda: storage)
    monkeypatch.setattr(settings.storage, "webdav_publication_max_workers", 1)
    return media, artifacts, storage


def test_retry_reloads_disk_progress_and_cleans_only_after_commit(publication):
    media, artifacts, storage = publication
    storage.failure = StorageUnavailable("offline", retryable=True)
    with pytest.raises(StorageUnavailable):
        ThumbnailArtifactService.persist(media, artifacts)
    store = ThumbnailBatchStore(media)
    batch = store.load()
    assert [entry["state"] for entry in batch.entries] == ["uploading", "uploading"]
    assert batch.source(batch.entries[0]).exists()
    assert not list(storage.root.rglob("*.zip"))
    assert not MediaThumbnail.select().exists()
    # Neither the previous in-memory batch nor the original source is needed.
    artifacts[0][1].unlink()
    storage.failure = None
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempts) == 2
    assert not store.manifest_path.exists()
    assert not batch.workspace.exists()
    assert MediaThumbnail.select().count() == Image.select().count() == 2


def test_requested_retry_does_not_call_provider_again(publication, monkeypatch):
    media, _, storage = publication
    calls = []

    class Provider:
        def generate_thumbnails(self, *, media, workspace, progress_callback=None):
            calls.append(media.media_id)
            for offset in (3, 6):
                PILImage.new("RGB", (32, 18)).save(workspace / f"{offset}.webp", "WEBP")
            return ThumbnailGeneration(2, tuple(ThumbnailArtifact(offset, f"{offset}.webp") for offset in (3, 6)))

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda library: Provider())
    storage.failure = StorageUnavailable("offline", retryable=True)
    first = MediaThumbnailTaskService.generate_requested_media(media.id)
    assert first.state == "terminal_failed"
    assert first.error_code == ThumbnailPublicationIncomplete.ERROR_CODE
    assert first.generated_count == 0
    assert Media.get_by_id(media.id).thumbnail_next_retry_at is None
    assert MediaThumbnailTaskService.count_pending_media() == 0
    assert MediaThumbnailTaskService.count_terminal_failed_media() == 1
    assert calls == [media.id]
    storage.failure = None
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda library: pytest.fail("regenerated"))
    events = []
    second = MediaThumbnailTaskService.generate_requested_media(media.id, progress_callback=events.append)
    assert second.state == "succeeded"
    assert len(storage.attempts) == 2
    assert events  # Fast retries may finish within the progress heartbeat throttle.


def test_bulk_zip_failure_has_no_available_images_and_waits_for_user(publication, monkeypatch):
    from types import SimpleNamespace

    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: pytest.fail("regenerated"))
    storage.failure = StorageUnavailable("offline", retryable=True)
    reporter = SimpleNamespace(emit=lambda **_kwargs: None)
    result = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)
    assert result["successful_media"] == 0 and result["terminal_failed_media"] == 1
    assert result["generated_thumbnails"] == 0
    assert MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)["pending_media"] == 0
    assert len(storage.attempts) == 1


def test_explicit_reset_resumes_zip_without_regeneration(publication, monkeypatch):
    from types import SimpleNamespace

    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: pytest.fail("regenerated"))
    storage.failure = StorageUnavailable("offline", retryable=True)
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "terminal_failed"
    assert not MediaThumbnail.select().exists()
    reporter = SimpleNamespace(emit=lambda **_kwargs: None)
    assert MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)["pending_media"] == 0
    assert len(storage.attempts) == 1
    storage.failure = None
    assert MediaThumbnailTaskService.reset_terminal_media([media.id]) == 1
    assert MediaThumbnailTaskService.count_pending_media() == 1
    assert MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)["successful_media"] == 1
    assert len(storage.attempts) == 2
    assert MediaThumbnail.get(MediaThumbnail.offset == 3).id is not None
    assert MediaThumbnail.select().count() == 2
    assert Media.get_by_id(media.id).thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED


@pytest.mark.parametrize("reset_first", [False, True])
def test_missing_partial_batch_is_not_treated_as_complete(publication, monkeypatch, reset_first):
    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: pytest.fail("regenerated"))
    storage.failure = StorageUnavailable("offline", retryable=True)
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "terminal_failed"
    store.manifest_path.unlink()
    if reset_first:
        assert MediaThumbnailTaskService.reset_terminal_media([media.id]) == 1
    result = MediaThumbnailTaskService.generate_requested_media(media.id)
    assert result.state == "terminal_failed"
    assert result.error_code == ThumbnailPublicationIncomplete.ERROR_CODE
    assert result.generated_count == 0
    assert len(storage.attempts) == 1
    assert MediaThumbnail.select().count() == 0
    assert "清单缺失" in Media.get_by_id(media.id).thumbnail_last_error


def test_partial_marker_survives_local_failure_and_missing_manifest(publication, monkeypatch):
    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: pytest.fail("regenerated"))
    storage.failure = StorageUnavailable("offline", retryable=True)
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "terminal_failed"
    batch = store.load()
    batch.source(batch.entries[1]).write_bytes(b"corrupt")
    failed = MediaThumbnailTaskService.generate_requested_media(media.id)
    assert failed.state == "terminal_failed" and failed.error_code == ThumbnailPublicationIncomplete.ERROR_CODE
    refreshed = Media.get_by_id(media.id)
    assert refreshed.thumbnail_last_error_code == ThumbnailPublicationIncomplete.ERROR_CODE
    assert "thumbnail_batch_file_invalid" in refreshed.thumbnail_last_error
    store.manifest_path.unlink()
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "terminal_failed"
    assert MediaThumbnail.select().count() == 0
    assert len(storage.attempts) == 1


def test_database_failure_retries_only_commit(publication, monkeypatch):
    media, artifacts, storage = publication
    create = MediaThumbnail.create

    def fail(**kwargs):
        raise OperationalError("database offline")

    monkeypatch.setattr(MediaThumbnail, "create", fail)
    with pytest.raises(OperationalError):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not Image.select().exists()
    assert ThumbnailBatchStore(media).load() is not None
    monkeypatch.setattr(MediaThumbnail, "create", create)
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempts) == 1


def test_commit_response_loss_is_reconciled_on_next_request(publication, monkeypatch, test_db):
    from contextlib import contextmanager
    from types import SimpleNamespace

    media, artifacts, storage = publication

    @contextmanager
    def atomic():
        with test_db.atomic():
            yield
        raise OperationalError("commit response lost")

    with monkeypatch.context() as patch:
        patch.setattr("src.service.playback.thumbnails.artifacts.get_database", lambda: SimpleNamespace(atomic=atomic))
        with pytest.raises(OperationalError):
            ThumbnailArtifactService.persist(media, artifacts)
    batch = ThumbnailBatchStore(media).load()
    assert batch is not None
    assert MediaThumbnail.select().count() == 2
    # All rows committed despite the lost response; only local cleanup remains.
    batch.source(batch.entries[0]).unlink()
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "already_exists"
    assert not batch.workspace.exists()
    assert ThumbnailBatchStore(media).load() is None
    assert len(storage.attempts) == 1
    assert MediaThumbnail.select().count() == Image.select().count() == 2


def test_cleanup_failure_keeps_success_and_can_retry_cleanup(publication, monkeypatch):
    media, artifacts, storage = publication
    with monkeypatch.context() as patch:
        patch.setattr("src.service.playback.thumbnails.batches.shutil.rmtree", lambda *args: (_ for _ in ()).throw(OSError("busy")))
        assert ThumbnailArtifactService.persist(media, artifacts) == 2
    assert ThumbnailBatchStore(media).load() is not None
    ThumbnailArtifactService.cleanup_committed(media)
    assert ThumbnailBatchStore(media).load() is None
    assert len(storage.attempts) == 1


def test_remote_success_without_checkpoint_is_reconciled(publication, monkeypatch):
    media, artifacts, storage = publication
    checkpoint = ThumbnailBatch.checkpoint_pack

    def fail_checkpoint(self, state):
        if state == "uploaded":
            raise OSError("disk full")
        checkpoint(self, state)

    with monkeypatch.context() as patch:
        patch.setattr(ThumbnailBatch, "checkpoint_pack", fail_checkpoint)
        with pytest.raises(OSError, match="disk full"):
            ThumbnailArtifactService.persist(media, artifacts)
    assert len(storage.attempts) == 1
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempts) == 1


def test_remote_uncertainty_does_not_turn_read_failure_into_missing(publication, monkeypatch):
    media, artifacts, storage = publication
    storage.failure = StorageUnavailable("offline", retryable=True)
    storage.unknown = True
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    storage.failure = None
    with monkeypatch.context() as patch:
        patch.setattr(storage, "stat", lambda key: (_ for _ in ()).throw(StorageUnavailable("read offline")))
        with pytest.raises(ThumbnailPublicationIncomplete) as caught:
            ThumbnailArtifactService.persist(media, [])
        assert str(caught.value.__cause__) == "read offline"
    assert len(storage.attempts) == 1
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempts) == 1


@pytest.mark.parametrize("damage", ["missing", "changed", "outside"])
def test_damaged_local_batch_is_retained_without_upload(publication, damage, tmp_path):
    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
    source = batch.source(batch.entries[0])
    if damage == "missing":
        source.unlink()
    elif damage == "changed":
        source.write_bytes(b"corrupt")
    else:
        source.unlink()
        source.symlink_to(artifacts[0][1])
    with pytest.raises(ValueError, match="thumbnail_batch_"):
        ThumbnailArtifactService.persist(media, [])
    assert store.manifest_path.exists()
    assert not storage.attempts


@pytest.mark.parametrize("field,value", [("generation", "../outside"), ("version", 999), ("images", []), ("media_id", 999)])
def test_invalid_manifest_fails_closed(publication, field, value):
    media, artifacts, storage = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
        data = json.loads(store.manifest_path.read_text())
        data[field] = value
        store.manifest_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="manifest_invalid"):
        ThumbnailArtifactService.persist(media, [])
    assert store.manifest_path.exists()
    assert not storage.attempts


def test_incomplete_generation_not_treated_as_ready(publication):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        unfinished = store.new_workspace()
        (unfinished / "partial.webp").write_bytes(b"partial")
        assert store.load() is None
        batch = store.prepare(artifacts)
        assert batch.workspace != unfinished
        assert unfinished.exists()


def test_manifest_replace_failure_preserves_previous_checkpoint(publication, monkeypatch):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        before = store.manifest_path.read_bytes()
        monkeypatch.setattr("src.service.playback.thumbnails.batches.os.replace", lambda *args: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError):
            batch.checkpoint(batch.entries[0], "uploaded")
        assert store.manifest_path.read_bytes() == before
        assert batch.entries[0]["state"] == "pending"
        assert set(store.directory.iterdir()) == {store.manifest_path, batch.workspace}


def test_failed_checkpoint_preserves_previous_error_metadata(publication, monkeypatch):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        entry = batch.entries[0]
        batch.checkpoint(entry, "uploading", error=StorageUnavailable("offline", status_code=503))
        before = dict(entry)
        monkeypatch.setattr(store, "save", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            batch.checkpoint(entry, "uploaded")
        assert entry == before
        assert store.load().entries[0] == before


def test_concurrent_checkpoints_keep_all_successes(publication):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda entry: batch.checkpoint(entry, "uploaded"), batch.entries))
        assert all(entry["state"] == "uploaded" for entry in store.load().entries)


def test_media_file_lock_survives_batch_cleanup(publication):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts).cleanup()
        with pytest.raises(MediaOperationBusy), ThumbnailBatchStore(media).locked():
            pytest.fail("second owner acquired the same media")
    with ThumbnailBatchStore(media).locked():
        pass


def test_source_and_destination_changes_do_not_reuse_batch(publication, monkeypatch):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        old = store.prepare(artifacts)
    media.file_hash = "new hash"
    assert ThumbnailBatchStore(media).load() is None
    media.file_hash = None
    assert ThumbnailBatchStore(media).load() is not None
    monkeypatch.setattr(settings.storage, "root_prefix", "new-target")
    assert ThumbnailBatchStore(media).load() is None
    assert old.workspace.exists()


def test_webdav_password_rotation_reuses_identity_without_persisting_credentials(publication, monkeypatch):
    media, artifacts, _ = publication
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.storage, "webdav_base_url", "https://user:secret@webdav.example.test/dav")
    monkeypatch.setattr(settings.storage, "username", "test-account")
    monkeypatch.setattr(settings.storage, "password", "first-secret")
    with ThumbnailBatchStore(media).locked() as store:
        store.prepare(artifacts)
    monkeypatch.setattr(settings.storage, "password", "rotated-secret")
    assert ThumbnailBatchStore(media).load() is not None
    text = store.manifest_path.read_text()
    for secret in ("first-secret", "rotated-secret", "test-account", "webdav.example", "https://"):
        assert secret not in text
    monkeypatch.setattr(settings.storage, "root_prefix", "different-target")
    assert ThumbnailBatchStore(media).load() is None


def test_changed_media_cannot_commit_uploaded_batch(publication):
    media, artifacts, storage = publication
    Media.update(valid=False).where(Media.id == media.id).execute()
    with pytest.raises(RuntimeError, match="media_changed"):
        ThumbnailArtifactService.persist(media, artifacts)
    assert not MediaThumbnail.select().exists()
    assert len(storage.attempts) == 1
    assert ThumbnailBatchStore(media).load() is not None


def test_outer_transaction_rollback_keeps_local_batch(publication, test_db):
    media, artifacts, storage = publication
    with pytest.raises(RuntimeError, match="outer rollback"), test_db.atomic():
        assert ThumbnailArtifactService.persist(media, artifacts) == 2
        assert ThumbnailBatchStore(media).load() is not None
        raise RuntimeError("outer rollback")
    assert not MediaThumbnail.select().exists()
    assert ThumbnailArtifactService.persist(media, []) == 2
    assert len(storage.attempts) == 1


def test_local_lock_excludes_other_process(publication):
    import subprocess
    import sys

    media, _, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        script = """
import fcntl, sys
with open(sys.argv[1], 'a+b') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
sys.exit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(store.root / f".{media.id}.lock")],
            capture_output=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr.decode()


def test_new_process_loads_durable_ready_batch(publication):
    import subprocess
    import sys

    media, artifacts, storage = publication
    storage.failure = StorageUnavailable("offline")
    with pytest.raises(StorageUnavailable):
        ThumbnailArtifactService.persist(media, artifacts)
    store = ThumbnailBatchStore(media)
    script = """
import json, sys
from pathlib import Path
from src.service.playback.thumbnails.batches import ThumbnailBatchStore
# Reconstruct only the on-disk locator, without sharing a live service or batch.
store = object.__new__(ThumbnailBatchStore)
store.directory = Path(sys.argv[1])
store.manifest_path = store.directory / 'current.json'
store.identity = sys.argv[2]
store.media_id = int(sys.argv[3])
batch = store.load()
batch.validate_files()
print(json.dumps([entry['state'] for entry in batch.entries]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.directory), store.identity, str(media.id)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == ["uploading", "uploading"]


def test_provider_output_names_cannot_overwrite_other_staged_sources(publication, monkeypatch):
    media, _, storage = publication
    originals = {}

    class Provider:
        def generate_thumbnails(self, *, media, workspace):
            images = workspace / "images"
            images.mkdir()
            for offset, name, color in ((3, "6.webp", "red"), (6, "3.webp", "blue")):
                path = images / name
                PILImage.new("RGB", (32, 18), color=color).save(path, "WEBP")
                originals[offset] = path.read_bytes()
            return ThumbnailGeneration(2, (ThumbnailArtifact(3, "images/6.webp"), ThumbnailArtifact(6, "images/3.webp")))

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda library: Provider())
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "succeeded"
    assert originals[3] != originals[6]
    for row in MediaThumbnail.select():
        from src.common.image_store import read_image_bytes
        assert read_image_bytes(row.image.origin, storage=storage) == originals[row.offset]


def test_pack_checkpoint_failure_keeps_durable_upload_state(publication, monkeypatch):
    media, artifacts, _ = publication
    with ThumbnailBatchStore(media).locked() as store:
        batch = store.prepare(artifacts)
        batch.checkpoint_pack("uploading")
        monkeypatch.setattr(store, "save", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            batch.checkpoint_pack("uploaded")
        assert all(entry["state"] == "uploading" for entry in batch.entries)
        assert all(entry["state"] == "uploading" for entry in store.load().entries)


def test_corrupt_staged_zip_is_not_published(publication):
    media, artifacts, storage = publication
    storage.failure = StorageUnavailable("offline")
    with pytest.raises(ThumbnailPublicationIncomplete):
        ThumbnailArtifactService.persist(media, artifacts)
    batch = ThumbnailBatchStore(media).load()
    (batch.workspace / "thumbnails.zip").write_bytes(b"corrupt archive")
    storage.failure = None
    with pytest.raises(ValueError, match="thumbnail_batch_pack_invalid"):
        ThumbnailArtifactService.persist(media, [])
    assert len(storage.attempts) == 1
    assert not MediaThumbnail.select().exists()
    assert batch.workspace.exists()
