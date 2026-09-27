"""按番号为指定媒体立即生成缩略图的手动任务。

宿主的定时缩略图任务按媒体 ID 顺序批量处理全部待生成媒体；本插件让用户
按番号立即处理一部影片。影片有多个媒体时不猜测目标，而是列出候选、发送
提醒，由用户填写 ``media_id`` 后重新执行。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration
from src.plugins.types import (
    MovieSnapshot,
    PluginMediaSnapshot,
    PluginThumbnailGenerationResult,
    PluginThumbnailStatus,
)
from src.scheduler.contracts import JobDefinition

PLUGIN_ID = "targeted_thumbnails"
PLUGIN_VERSION = "1.0.0"
DISPLAY_NAME = "指定番号生成缩略图"
TASK_KEY = "targeted_thumbnail_generation"
# 插件任务的展示名取 cli_help。
TASK_NAME = "指定番号生成媒体缩略图"
SELECTION_EVENT_TYPE = "targeted_thumbnail_selection_required"
# Media 主键是 PostgreSQL serial（int32）。
MAX_MEDIA_ID = 2**31 - 1

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
_FAILURE_REASONS = {
    "not_found": "媒体已不存在",
    "invalid": "媒体文件已被巡检标记为失效，无法生成缩略图",
    "busy": "媒体正被其他任务处理（如定时缩略图生成或存储迁移），请稍后重试",
    "deferred": "媒体来源暂未就绪，已交由定时缩略图任务稍后重试",
    "backend_unavailable": "媒体库的缩略图后端暂不可用，请检查媒体库后重试",
    "retryable_failed": "缩略图生成失败，定时缩略图任务会自动重试",
    "terminal_failed": "缩略图生成失败，已停止自动重试",
}


class TargetedThumbnailError(RuntimeError):
    """任务无法完成；消息会原样作为任务失败原因展示给用户。"""


class TargetedThumbnailParams(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        title=TASK_NAME,
    )

    movie_number: str = Field(
        min_length=1,
        max_length=255,
        title="番号",
        description="要生成缩略图的影片番号；大小写不敏感，- 与 _ 可互换。",
    )
    media_id: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MEDIA_ID,
        title="媒体 ID",
        description=(
            "番号有多个媒体时必填。先留空执行一次，"
            "再从任务结果或提醒通知里选择 media_id 重新执行。"
        ),
    )

    @field_validator("media_id", mode="before")
    @classmethod
    def _blank_media_id_means_unset(cls, value: Any) -> Any:
        # 表单留空时可能提交空字符串，与不填同义。
        if isinstance(value, str) and not value.strip():
            return None
        return value


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(_SIZE_UNITS) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{size_bytes} B"
    return f"{value:.1f} {_SIZE_UNITS[unit_index]}"


def _format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _thumbnail_label(
    media: PluginMediaSnapshot,
    status: PluginThumbnailStatus | None,
) -> str:
    if not media.valid:
        return "文件失效，无法生成"
    if status is None:
        return "缩略图状态未知"
    if status.thumbnail_count > 0:
        return f"已有 {status.thumbnail_count} 张缩略图"
    if status.state == "retry_wait":
        return "等待自动重试"
    if status.state == "terminal":
        return f"已停止自动重试（{status.last_error_code or '未知错误'}）"
    return "待生成"


def _candidate_line(
    media: PluginMediaSnapshot,
    status: PluginThumbnailStatus | None,
) -> str:
    parts = [f"media_id={media.media_id}", media.library_name, media.file_name]
    if media.resolution:
        parts.append(media.resolution)
    if media.file_size_bytes > 0:
        parts.append(_format_size(media.file_size_bytes))
    if media.duration_seconds > 0:
        parts.append(_format_duration(media.duration_seconds))
    parts.append(_thumbnail_label(media, status))
    return " · ".join(parts)


def _candidate(
    media: PluginMediaSnapshot,
    status: PluginThumbnailStatus | None,
) -> dict[str, Any]:
    return {
        "media_id": media.media_id,
        "library_name": media.library_name,
        "file_name": media.file_name,
        "resolution": media.resolution,
        "file_size_bytes": media.file_size_bytes,
        "duration_seconds": media.duration_seconds,
        "valid": media.valid,
        "thumbnail_state": status.state if status is not None else None,
        "thumbnail_count": status.thumbnail_count if status is not None else 0,
        "description": _candidate_line(media, status),
    }


def _task_run_id(reporter: Any) -> int | None:
    value = getattr(reporter, "task_run_id", None)
    return value if isinstance(value, int) else None


def _request_selection(
    context: PluginContext,
    reporter: Any,
    movie: MovieSnapshot,
    candidate_lines: list[str],
) -> dict[str, Any]:
    movie_number = movie.values["movie_number"]
    prompt = (
        f"番号 {movie_number} 有 {len(candidate_lines)} 个媒体，"
        "请选择一个 media_id 后重新执行本任务"
    )
    context.notifications.create(
        category="reminder",
        title=f"{movie_number} 需要选择生成缩略图的媒体",
        content="\n".join([f"{prompt}：", *candidate_lines]),
        event_type=SELECTION_EVENT_TYPE,
        related_task_run_id=_task_run_id(reporter),
        related_resource_type="movie",
        related_resource_id=movie.movie_id,
    )
    # 进度文本与结果文本也带上候选，用户在任务列表里即可看到可选 media_id。
    message = f"{prompt}：{'；'.join(candidate_lines)}"
    reporter.emit(current=1, total=1, text=message)
    return {"status": "selection_required", "message": message}


def _failure_message(label: str, result: PluginThumbnailGenerationResult) -> str:
    reason = _FAILURE_REASONS.get(result.outcome, f"缩略图生成未完成（{result.outcome}）")
    suffix = f"（错误码 {result.error_code}）" if result.error_code else ""
    return f"{label}：{reason}{suffix}"


def _generate(
    context: PluginContext,
    reporter: Any,
    media: PluginMediaSnapshot,
    status: PluginThumbnailStatus | None,
) -> dict[str, Any]:
    label = f"媒体 {media.media_id}（{media.file_name}）"
    reporter.emit(
        current=0,
        total=1,
        text=f"{label} · 开始生成缩略图",
        summary_patch={"media_id": media.media_id},
    )
    result = context.thumbnails.generate(
        media.media_id,
        progress_callback=lambda text: reporter.emit(
            current=0, total=1, text=f"{label} · {text}"
        ),
    )
    if result.outcome == "succeeded":
        message = f"{label} 已生成 {result.generated_count} 张缩略图"
        reporter.emit(current=1, total=1, text=message)
        return {
            "status": "succeeded",
            "generated_thumbnails": result.generated_count,
            "message": message,
        }
    if result.outcome == "already_exists":
        # 生成前已有缩略图时，宿主不会重建；这里只读取数量用于提示。
        refreshed = context.thumbnails.status_for_media([media.media_id]).get(
            media.media_id, status
        )
        count = refreshed.thumbnail_count if refreshed is not None else 0
        message = f"{label} 已有 {count} 张缩略图，无需重复生成"
        reporter.emit(current=1, total=1, text=message)
        return {"status": "already_exists", "message": message}
    raise TargetedThumbnailError(_failure_message(label, result))


def run_targeted_thumbnails(
    context: PluginContext,
    reporter: Any,
    raw_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    params = TargetedThumbnailParams.model_validate(dict(raw_params or {}))
    reporter.emit(current=0, total=1, text=f"正在查找番号 {params.movie_number}")

    movies = context.movies.find_by_numbers([params.movie_number])
    if not movies:
        raise TargetedThumbnailError(f"未找到番号 {params.movie_number} 对应的影片")
    movie = movies[0]
    movie_number = movie.values["movie_number"]

    media_items = context.media.list_for_movie(movie.movie_id)
    statuses = (
        context.thumbnails.status_for_media([item.media_id for item in media_items])
        if media_items
        else {}
    )
    candidates = [_candidate(item, statuses.get(item.media_id)) for item in media_items]
    reporter.emit(
        summary_patch={
            "movie_number": movie_number,
            "media_count": len(media_items),
            "candidates": candidates,
        }
    )
    if not media_items:
        raise TargetedThumbnailError(f"影片 {movie_number} 没有媒体，无法生成缩略图")
    candidate_lines = [candidate["description"] for candidate in candidates]

    if params.media_id is not None:
        selected = next(
            (item for item in media_items if item.media_id == params.media_id), None
        )
        if selected is None:
            raise TargetedThumbnailError(
                "\n".join(
                    [
                        f"媒体 {params.media_id} 不属于影片 {movie_number}，可选媒体：",
                        *candidate_lines,
                    ]
                )
            )
    elif len(media_items) > 1:
        return _request_selection(context, reporter, movie, candidate_lines)
    else:
        selected = media_items[0]

    return _generate(context, reporter, selected, statuses.get(selected.media_id))


def register(context: PluginContext) -> PluginRegistration:
    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        version=PLUGIN_VERSION,
        host_api_version=HOST_API_VERSION,
        jobs=(
            JobDefinition(
                task_key=TASK_KEY,
                log_name="targeted-thumbnail-generation",
                cli_name="generate-targeted-thumbnails",
                cli_help=TASK_NAME,
                manual_only=True,
                params_schema=TargetedThumbnailParams,
                handler=lambda reporter, params: run_targeted_thumbnails(
                    context, reporter, params
                ),
            ),
        ),
    )
