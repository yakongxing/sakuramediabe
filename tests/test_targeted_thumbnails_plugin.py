"""指定番号生成缩略图插件：按番号定位媒体，多媒体时要求用户选择。"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage
from pydantic import ValidationError

from src.config.config import settings
from src.model import (
    BackgroundTaskRun,
    Image,
    Media,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    SystemNotification,
)
from src.plugins.loader import check_plugin_dir
from src.plugins.manifest import load_manifest_from_file
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ThumbnailArtifact,
    ThumbnailGeneration,
)
from src.service.playback.thumbnails.task_service import (
    MediaThumbnailTaskService,
    ThumbnailGenerationOutcome,
)
from src.service.system import ActivityService

PLUGIN_SOURCE = Path(__file__).resolve().parents[1] / "plugin_packages" / "targeted_thumbnails"


@pytest.fixture()
def job(tmp_path):
    # 从副本加载，避免在仓库内的插件目录写入 data/ 与 __pycache__。
    plugin_dir = tmp_path / "plugins" / "targeted_thumbnails"
    shutil.copytree(PLUGIN_SOURCE, plugin_dir)
    registration = check_plugin_dir(plugin_dir=plugin_dir)
    assert len(registration.jobs) == 1
    return registration.jobs[0]


@pytest.fixture()
def generated_offsets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    calls = []

    def generate_thumbnails(*, media, workspace, progress_callback=None):
        calls.append(media.media_id)
        for offset in (0, 30):
            PILImage.new("RGB", (16, 9)).save(workspace / f"{offset}.webp", "WEBP")
        return ThumbnailGeneration(
            expected_count=2,
            artifacts=(ThumbnailArtifact(0, "0.webp"), ThumbnailArtifact(30, "30.webp")),
        )

    storage = SimpleNamespace(generate_thumbnails=generate_thumbnails)
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: storage)
    return calls


class _Reporter:
    def __init__(self, task_run_id=None):
        self.task_run_id = task_run_id
        self.events = []

    def emit(self, **payload):
        self.events.append(payload)


def _movie_with_media(*file_names, movie_number="TGT-001", library_name="主媒体库"):
    library = MediaLibrary.create(name=library_name, provider_key="fake", provider_config={})
    movie = Movie.create(movie_number=movie_number, javdb_id=movie_number.lower(), title="target")
    media = [
        Media.create(
            movie=movie,
            library=library,
            file_name=file_name,
            resolution="1920x1080",
            file_size_bytes=3 * 1024**3,
            duration_seconds=7384,
        )
        for file_name in file_names
    ]
    return movie, media


def test_plugin_registers_manual_job_with_parameter_schema(job):
    manifest = load_manifest_from_file(PLUGIN_SOURCE)

    assert manifest.plugin_id == "targeted_thumbnails"
    assert job.plugin_id == "targeted_thumbnails"
    assert job.task_key == "targeted_thumbnail_generation"
    assert job.manual_only is True
    assert job.default_cron is None
    schema = job.params_schema.model_json_schema()
    assert schema["required"] == ["movie_number"]
    assert set(schema["properties"]) == {"movie_number", "media_id"}


def test_parameters_normalize_blank_media_id_and_reject_unknown_fields(job):
    params = job.params_schema.model_validate({"movie_number": "  abc-001 ", "media_id": ""})
    assert params.model_dump() == {"movie_number": "abc-001", "media_id": None}
    assert job.params_schema.model_validate({"movie_number": "A", "media_id": "12"}).media_id == 12

    for payload in ({}, {"movie_number": " "}, {"movie_number": "A", "media_ids": [1]}):
        with pytest.raises(ValidationError):
            job.params_schema.model_validate(payload)


def test_single_media_is_generated_without_selection(test_db, job, generated_offsets):
    _movie, (media,) = _movie_with_media("TGT-001.mp4")
    reporter = _Reporter()

    result = job.handler(reporter, {"movie_number": "tgt_001"})

    assert result["status"] == "succeeded"
    assert result["generated_thumbnails"] == 2
    assert generated_offsets == [media.id]
    assert MediaThumbnail.select().where(MediaThumbnail.media == media.id).count() == 2
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 1
    summary = {}
    for event in reporter.events:
        summary.update(event.get("summary_patch") or {})
    assert summary["movie_number"] == "TGT-001"
    assert summary["media_id"] == media.id
    assert summary["candidates"][0]["description"].startswith(f"media_id={media.id} · 主媒体库")


def test_multiple_media_require_user_selection_before_generation(
    test_db, job, generated_offsets, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings.scheduler, "log_dir", str(tmp_path / "logs"))
    movie, (first, second) = _movie_with_media("TGT-001-1080p.mp4", "TGT-001-4k.mkv")
    image = Image.create(origin="x.webp", small="x.webp", medium="x.webp", large="x.webp")
    MediaThumbnail.create(media=first, image=image, offset=0)
    task_run = ActivityService.create_task_run(
        task_key=job.task_key, task_name=job.cli_help, trigger_type="manual"
    )

    result = ActivityService.run_task(
        func=job.build_executor({"movie_number": "TGT-001", "media_id": None}),
        task_run_id=task_run.id,
        log_task_name=job.log_name,
    )

    assert result["status"] == "selection_required"
    assert generated_offsets == []
    task_run = BackgroundTaskRun.get_by_id(task_run.id)
    assert task_run.state == "completed"
    assert "status=selection_required" in task_run.result_text
    assert f"media_id={second.id}" in task_run.result_text
    assert f"media_id={second.id}" in task_run.progress_text
    assert [item["media_id"] for item in task_run.result_summary["candidates"]] == [
        first.id,
        second.id,
    ]
    notification = SystemNotification.get(SystemNotification.category == "reminder")
    assert notification.related_task_run_id == task_run.id
    assert notification.related_resource_type == "movie"
    assert notification.related_resource_id == movie.id
    assert f"media_id={first.id} · 主媒体库 · TGT-001-1080p.mp4" in notification.content
    assert "1920x1080 · 3.0 GB · 2:03:04 · 已有 1 张缩略图" in notification.content
    assert f"media_id={second.id}" in notification.content
    assert "待生成" in notification.content


def test_selected_media_is_generated_and_foreign_media_is_rejected(
    test_db, job, generated_offsets
):
    _movie, (first, second) = _movie_with_media("TGT-001-a.mp4", "TGT-001-b.mp4")
    _other_movie, (foreign,) = _movie_with_media(
        "OTH-001.mp4", movie_number="OTH-001", library_name="其他媒体库"
    )

    result = job.handler(_Reporter(), {"movie_number": "TGT-001", "media_id": second.id})

    assert result["status"] == "succeeded"
    assert generated_offsets == [second.id]
    with pytest.raises(RuntimeError, match=f"媒体 {foreign.id} 不属于影片 TGT-001") as caught:
        job.handler(_Reporter(), {"movie_number": "TGT-001", "media_id": foreign.id})
    assert f"media_id={first.id}" in str(caught.value)
    assert generated_offsets == [second.id]


def test_missing_movie_and_movie_without_media_fail_with_clear_reason(
    test_db, job, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings.scheduler, "log_dir", str(tmp_path / "logs"))
    Movie.create(movie_number="EMPTY-001", javdb_id="empty-1", title="empty")
    task_run = ActivityService.create_task_run(
        task_key=job.task_key, task_name=job.cli_help, trigger_type="manual"
    )

    with pytest.raises(RuntimeError, match="未找到番号 NONE-404 对应的影片"):
        ActivityService.run_task(
            func=job.build_executor({"movie_number": "NONE-404"}),
            task_run_id=task_run.id,
            log_task_name=job.log_name,
        )
    failed = BackgroundTaskRun.get_by_id(task_run.id)
    assert failed.state == "failed"
    assert failed.error_message == "未找到番号 NONE-404 对应的影片"

    with pytest.raises(RuntimeError, match="影片 EMPTY-001 没有媒体"):
        job.handler(_Reporter(), {"movie_number": "empty-001"})


def test_already_generated_and_failed_outcomes(test_db, job, monkeypatch):
    _movie, (media,) = _movie_with_media("TGT-001.mp4")
    image = Image.create(origin="y.webp", small="y.webp", medium="y.webp", large="y.webp")
    MediaThumbnail.create(media=media, image=image, offset=0)

    result = job.handler(_Reporter(), {"movie_number": "TGT-001"})

    assert result["status"] == "already_exists"
    assert "已有 1 张缩略图" in result["message"]

    monkeypatch.setattr(
        MediaThumbnailTaskService,
        "generate_requested_media",
        lambda media_id, *, progress_callback=None: ThumbnailGenerationOutcome(
            "terminal_failed", error_code="thumbnail_generation_empty"
        ),
    )
    with pytest.raises(RuntimeError, match="已停止自动重试（错误码 thumbnail_generation_empty）"):
        job.handler(_Reporter(), {"movie_number": "TGT-001"})
