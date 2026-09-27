"""插件缩略图契约：批量读取缩略图状态，并按需生成单条媒体的缩略图。"""

from dataclasses import FrozenInstanceError

import pytest

from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.plugins import (
    PluginContext,
    PluginThumbnailGenerationResult,
    PluginThumbnailStatus,
)
from src.service.playback.thumbnails.task_service import (
    MediaThumbnailTaskService,
    ThumbnailGenerationOutcome,
)


def _media(movie: Movie, library: MediaLibrary, file_name: str, **overrides) -> Media:
    return Media.create(movie=movie, library=library, file_name=file_name, **overrides)


def test_status_for_media_reports_state_and_actual_thumbnail_count(test_db, tmp_path):
    library = MediaLibrary.create(name="thumb-status", provider_key="local", provider_config={})
    movie = Movie.create(movie_number="TST-001", javdb_id="tst-1", title="status")
    generated = _media(movie, library, "a.mp4", thumbnail_generation_state="succeeded")
    terminal = _media(
        movie,
        library,
        "b.mp4",
        thumbnail_generation_state="terminal",
        thumbnail_last_error_code="video_file_missing",
    )
    for offset in (0, 10):
        image = Image.create(
            origin=f"{offset}.webp", small=f"{offset}.webp", medium=f"{offset}.webp", large=f"{offset}.webp"
        )
        MediaThumbnail.create(media=generated, image=image, offset=offset)
    api = PluginContext("thumb_demo", {}, tmp_path).thumbnails

    statuses = api.status_for_media([terminal.id, generated.id, terminal.id, 999_999])

    assert set(statuses) == {generated.id, terminal.id}
    assert statuses[generated.id] == PluginThumbnailStatus(
        media_id=generated.id, state="succeeded", thumbnail_count=2, last_error_code=None
    )
    assert statuses[terminal.id] == PluginThumbnailStatus(
        media_id=terminal.id,
        state="terminal",
        thumbnail_count=0,
        last_error_code="video_file_missing",
    )
    assert api.status_for_media([]) == {}
    with pytest.raises(FrozenInstanceError):
        statuses[generated.id].state = "pending"
    with pytest.raises(ValueError):
        api.status_for_media([0])


def test_generate_delegates_to_host_generation_and_maps_outcome(tmp_path, monkeypatch):
    calls = []

    def generate_requested_media(media_id, *, progress_callback=None):
        calls.append(media_id)
        progress_callback("正在准备视频")
        return ThumbnailGenerationOutcome("succeeded", generated_count=12)

    monkeypatch.setattr(
        MediaThumbnailTaskService, "generate_requested_media", generate_requested_media
    )
    texts = []
    api = PluginContext("thumb_demo", {}, tmp_path).thumbnails

    result = api.generate(7, progress_callback=texts.append)

    assert result == PluginThumbnailGenerationResult(
        media_id=7, outcome="succeeded", generated_count=12, error_code=None
    )
    assert calls == [7]
    assert texts == ["正在准备视频"]


def test_generate_validates_arguments_before_touching_media(tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr(MediaThumbnailTaskService, "generate_requested_media", unexpected)
    api = PluginContext("thumb_demo", {}, tmp_path).thumbnails

    with pytest.raises(ValueError):
        api.generate(0)
    with pytest.raises(ValueError):
        api.generate(True)
    with pytest.raises(TypeError):
        api.generate(1, progress_callback="not callable")
