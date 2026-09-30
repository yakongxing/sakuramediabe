"""按需为单条媒体生成缩略图：显式请求绕过批量候选筛选与退避，复用同一套状态收口。"""

from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ThumbnailArtifact,
    ThumbnailGeneration,
)
from src.service.playback.operation_locks import MediaOperationBusy
from src.service.playback.thumbnails import task_service
from src.service.playback.thumbnails.progress import ThumbnailTaskProgress
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


class _FakeStorage:
    """写出真实 WebP 产物，让校验与落盘走完整宿主链路。"""

    def __init__(self, *, offsets=(0, 10, 20), error: Exception | None = None):
        self.offsets = offsets
        self.error = error
        self.calls = []

    def generate_thumbnails(self, *, media, workspace, progress_callback=None):
        self.calls.append(media.media_id)
        if self.error is not None:
            raise self.error
        if progress_callback is not None:
            progress_callback(f"已生成 {len(self.offsets)}/{len(self.offsets)} 张")
        artifacts = []
        for offset in self.offsets:
            PILImage.new("RGB", (16, 9), color=(offset, 0, 0)).save(
                workspace / f"{offset}.webp", "WEBP"
            )
            artifacts.append(ThumbnailArtifact(offset_seconds=offset, relative_path=f"{offset}.webp"))
        return ThumbnailGeneration(expected_count=len(self.offsets), artifacts=tuple(artifacts))


@pytest.fixture()
def image_root(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(root))
    return root


@pytest.fixture()
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: fake)
    return fake


def _media(**overrides) -> Media:
    library = MediaLibrary.create(name="requested-library", provider_key="fake", provider_config={})
    movie = Movie.create(movie_number="REQ-001", javdb_id="req-1", title="requested")
    values = {"movie": movie, "library": library, "file_name": "REQ-001.mp4"}
    values.update(overrides)
    return Media.create(**values)


def test_requested_generation_resets_terminal_media_and_persists_thumbnails(
    test_db, image_root, storage
):
    media = _media(
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
        thumbnail_deferred_count=3,
        thumbnail_next_retry_at="2026-03-12 10:00:00",
        thumbnail_last_error_code="thumbnail_generation_empty",
        thumbnail_terminal_at="2026-03-12 10:00:00",
    )
    texts = []

    outcome = MediaThumbnailTaskService.generate_requested_media(
        media.id, progress_callback=texts.append
    )

    assert outcome.state == "succeeded"
    assert outcome.generated_count == 3
    assert storage.calls == [media.id]
    refreshed = Media.get_by_id(media.id)
    assert refreshed.thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED
    assert refreshed.thumbnail_attempt_count == 0
    assert refreshed.thumbnail_deferred_count == 0
    assert refreshed.thumbnail_last_error_code is None
    assert refreshed.thumbnail_terminal_at is None
    thumbnails = list(
        MediaThumbnail.select(MediaThumbnail, Image)
        .join(Image)
        .where(MediaThumbnail.media == media.id)
        .order_by(MediaThumbnail.offset)
    )
    assert [thumbnail.offset for thumbnail in thumbnails] == [0, 10, 20]
    from src.common.image_store import read_image_bytes

    assert all(read_image_bytes(item.image.origin) for item in thumbnails)
    assert len(list(image_root.rglob("*.zip"))) == 1
    assert not list(image_root.rglob("*.webp"))
    # 首条进度立即发出；提供方进度按心跳节流，快速完成时可能不会转发。
    assert texts[0] == "正在准备视频"


def test_requested_generation_forwards_throttled_provider_progress_via_heartbeat(
    test_db, image_root, monkeypatch
):
    monkeypatch.setattr(ThumbnailTaskProgress, "INTERVAL_SECONDS", 0.02)
    delivered = Event()
    texts = []

    def callback(text):
        texts.append(text)
        if text.startswith("正在抽帧"):
            delivered.set()

    class SlowStorage(_FakeStorage):
        def generate_thumbnails(self, *, media, workspace, progress_callback=None):
            progress_callback("正在抽帧")
            assert delivered.wait(2), "heartbeat did not forward provider progress"
            return super().generate_thumbnails(media=media, workspace=workspace)

    storage = SlowStorage()
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: storage)
    media = _media()

    outcome = MediaThumbnailTaskService.generate_requested_media(
        media.id, progress_callback=callback
    )

    assert outcome.state == "succeeded"
    assert texts[0] == "正在准备视频"
    assert any(text.startswith("正在抽帧") for text in texts)
    # 返回后心跳已停止，不会再覆盖调用方写入的最终进度。
    count = len(texts)
    Event().wait(0.08)
    assert len(texts) == count


def test_requested_generation_skips_media_that_already_has_thumbnails(
    test_db, image_root, storage
):
    media = _media(thumbnail_generation_state=Media.THUMBNAIL_STATE_PENDING)
    image = Image.create(origin="a.webp", small="a.webp", medium="a.webp", large="a.webp")
    MediaThumbnail.create(media=media, image=image, offset=0)

    outcome = MediaThumbnailTaskService.generate_requested_media(media.id)

    assert outcome.state == "already_exists"
    assert storage.calls == []
    assert Media.get_by_id(media.id).thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED


def test_requested_generation_rejects_missing_and_invalid_media_without_changes(
    test_db, image_root, storage
):
    media = _media(
        valid=False,
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
    )

    assert MediaThumbnailTaskService.generate_requested_media(media.id + 100).state == "not_found"
    assert MediaThumbnailTaskService.generate_requested_media(media.id).state == "invalid"
    assert storage.calls == []
    refreshed = Media.get_by_id(media.id)
    assert refreshed.thumbnail_generation_state == Media.THUMBNAIL_STATE_TERMINAL
    assert refreshed.thumbnail_attempt_count == 2


def test_requested_generation_failure_starts_a_fresh_retry_budget(
    test_db, image_root, monkeypatch
):
    media = _media(
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
    )
    failing = _FakeStorage(error=RuntimeError("ffmpeg_exit_1: broken stream"))
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: failing)

    outcome = MediaThumbnailTaskService.generate_requested_media(media.id)

    assert outcome.state == "retryable_failed"
    assert outcome.error_code == "ffmpeg_exit_1"
    refreshed = Media.get_by_id(media.id)
    assert refreshed.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT
    assert refreshed.thumbnail_attempt_count == 1
    assert refreshed.thumbnail_next_retry_at is not None
    assert MediaThumbnail.select().where(MediaThumbnail.media == media.id).count() == 0


def test_requested_generation_reports_busy_media_without_changes(
    test_db, image_root, storage, monkeypatch
):
    media = _media(thumbnail_generation_state=Media.THUMBNAIL_STATE_RETRY_WAIT)

    @contextmanager
    def busy_lock(_namespace, _resource_id):
        raise MediaOperationBusy()
        yield

    monkeypatch.setattr(task_service, "media_operation_lock", busy_lock)

    outcome = MediaThumbnailTaskService.generate_requested_media(media.id)

    assert outcome.state == "busy"
    assert storage.calls == []
    assert Media.get_by_id(media.id).thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT


def test_bulk_generation_still_skips_media_with_existing_thumbnails(test_db, image_root, storage):
    """批量链路改为复用单媒体生成体后，已有缩略图的媒体仍只做状态修正。"""
    media = _media(thumbnail_generation_state=Media.THUMBNAIL_STATE_PENDING)
    image = Image.create(origin="b.webp", small="b.webp", medium="b.webp", large="b.webp")
    MediaThumbnail.create(media=media, image=image, offset=0)

    outcome = MediaThumbnailTaskService._generate_one(media.id)

    assert outcome.state == "skipped"
    assert storage.calls == []
    assert Media.get_by_id(media.id).thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED


def test_bulk_generation_uses_shared_generation_body(test_db, image_root, storage):
    media = _media()

    result = MediaThumbnailTaskService.generate_pending_thumbnails(
        reporter=SimpleNamespace(emit=lambda **_payload: None)
    )

    assert result["successful_media"] == 1
    assert result["generated_thumbnails"] == 3
    assert storage.calls == [media.id]


pytestmark = pytest.mark.usefixtures("isolated_local_storage")
