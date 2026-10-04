from types import SimpleNamespace

import pytest
from PIL import Image as PillowImage

from src.common.image_store import read_image_bytes
from src.config import settings
from src.model import VideoItem
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.service.videos import video_cover_service as module


def test_generated_video_cover_is_readable_and_cleaned_without_webdav(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "backend", "webdav")
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video source")
    frame = SimpleNamespace(to_image=lambda: PillowImage.new("RGB", (32, 18), "blue"))
    container = SimpleNamespace(
        streams=SimpleNamespace(video=[object()]),
        decode=lambda _stream: iter([frame]), close=lambda: None,
    )
    monkeypatch.setattr(module, "av", SimpleNamespace(open=lambda _source: container))
    monkeypatch.setattr("src.service.catalog.image_cleanup_service.asset_storage", lambda: pytest.fail("cover reached WebDAV"))
    video = VideoItem.create(title="local cover")
    image = module.VideoCoverService.generate_cover(video, source)
    assert image is not None
    assert VideoItem.get_by_id(video.id).cover_image_id == image.id
    assert image.origin.startswith("local-covers/videos/")
    assert read_image_bytes(image.origin)
    assert ImageCleanupService.delete_image_record_if_unused(image) == set()
    video.cover_image = None
    video.save()
    keys = ImageCleanupService.delete_image_record_if_unused(image)
    ImageCleanupService.delete_obsolete_image_files(keys)
    with pytest.raises(FileNotFoundError):
        read_image_bytes(image.origin)
