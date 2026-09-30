from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from inspect import signature
from typing import Any

from loguru import logger
from peewee import fn

from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary, MediaThumbnail
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
    ThumbnailBackendUnavailable,
    ThumbnailGenerationDeferred,
)
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import media_handle_for
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.batches import ThumbnailBatchStore
from src.service.playback.thumbnails.contracts import ThumbnailDeferred
from src.service.playback.thumbnails.progress import ThumbnailTaskProgress


@dataclass(frozen=True)
class ThumbnailGenerationOutcome:
    state: str
    generated_count: int = 0
    error_code: str | None = None


class _TextProgressSink:
    """让 ThumbnailTaskProgress 的心跳只向调用方转发进度文本。"""

    def __init__(self, callback):
        self._callback = callback

    def emit(self, *, text: str, **_payload) -> None:
        self._callback(text)


class MediaThumbnailTaskService:
    """Generate one complete thumbnail set per Media through its provider."""

    TASK_KEY = "media_thumbnail_generation"
    MAX_FAILURE_ATTEMPTS = 2
    MAX_DEFERRED_ATTEMPTS = 3
    DEFERRED_BACKOFF_BASE_SECONDS = 15 * 60
    FAILURE_RETRY_BACKOFF_BASE_SECONDS = 15 * 60
    FAILURE_RETRY_BACKOFF_MAX_SECONDS = 24 * 60 * 60
    TERMINAL_ERROR_CODES = frozenset(
        {
            "thumbnail_generation_empty",
            "thumbnail_generation_insufficient_count",
            "thumbnail_generation_unparseable_filenames",
            "thumbnail_offset_invalid",
            "thumbnail_artifact_empty",
            "thumbnail_artifact_not_webp",
            "thumbnail_artifact_invalid",
        }
    )

    @staticmethod
    def _thumbnail_exists_query():
        return MediaThumbnail.select(MediaThumbnail.id).where(MediaThumbnail.media == Media.id)

    @classmethod
    def _missing_thumbnail_condition(cls):
        return ~fn.EXISTS(cls._thumbnail_exists_query())

    @classmethod
    def _candidate_query(cls):
        now = utc_now_for_db()
        normal_state = (
            Media.thumbnail_generation_state.in_(
                (Media.THUMBNAIL_STATE_PENDING, Media.THUMBNAIL_STATE_SUCCEEDED)
            )
            | (
                (Media.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT)
                & (
                    Media.thumbnail_next_retry_at.is_null(True)
                    | (Media.thumbnail_next_retry_at <= now)
                )
            )
        )
        return (
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(Media.valid == True, cls._missing_thumbnail_condition(), normal_state)
            .order_by(Media.id)
        )

    @classmethod
    def _candidate_entries(cls) -> list[tuple[int, tuple[str, int]]]:
        return [
            (int(media_id), (str(provider_key), int(library_id)))
            for media_id, provider_key, library_id in (
                cls._candidate_query()
                .select(Media.id, MediaLibrary.provider_key, MediaLibrary.id)
                .tuples()
            )
        ]

    @classmethod
    def _count_state(cls, state: str) -> int:
        return (
            Media.select(Media.id)
            .where(
                cls._missing_thumbnail_condition(),
                Media.thumbnail_generation_state == state,
            )
            .count()
        )

    @classmethod
    def count_pending_media(cls) -> int:
        return cls._candidate_query().count()

    @classmethod
    def count_retry_wait_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_RETRY_WAIT)

    @classmethod
    def count_terminal_failed_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_TERMINAL)

    @classmethod
    def reset_terminal_media(cls, media_ids: list[int]) -> int:
        ensure_database_ready()
        return Media.update(
            thumbnail_generation_state=Media.THUMBNAIL_STATE_PENDING,
            thumbnail_attempt_count=0,
            thumbnail_deferred_count=0,
            thumbnail_next_retry_at=None,
            thumbnail_last_error_code=None,
            thumbnail_last_error=None,
            thumbnail_terminal_at=None,
            updated_at=utc_now_for_db(),
        ).where(
            Media.id.in_(media_ids),
            Media.valid == True,
            Media.thumbnail_generation_state == Media.THUMBNAIL_STATE_TERMINAL,
            cls._missing_thumbnail_condition(),
        ).execute()

    @staticmethod
    def minimum_acceptable_count(expected_count: int) -> int:
        return max(1, int(expected_count * 0.85))

    @classmethod
    def _generate_artifacts(cls, media: Media, progress_callback=None, *, check_connection=None) -> int:
        with ThumbnailBatchStore(media).locked() as store:
            batch = store.load()
            if batch is not None:
                if progress_callback:
                    progress_callback("正在恢复本地缩略图上传批次")
                return ThumbnailArtifactService.persist_batch(
                    media, batch, check_connection=check_connection, progress_callback=progress_callback,
                )
            if check_connection is not None:
                check_connection()
            handle = media_handle_for(media)
            workspace = store.new_workspace()
            provider_workspace = workspace / "generated"
            provider_workspace.mkdir()
            try:
                storage = MEDIA_PROVIDER_REGISTRY.storage_for(handle.library)
                # 已安装的旧提供方尚未接收缩略图进度回调。
                progress_kwargs = (
                    {"progress_callback": progress_callback}
                    if "progress_callback" in signature(storage.generate_thumbnails).parameters
                    else {}
                )
                generation = storage.generate_thumbnails(
                    media=handle, workspace=provider_workspace, **progress_kwargs,
                )
            except ThumbnailGenerationDeferred as exc:
                raise ThumbnailDeferred(
                    str(exc),
                    error_code=exc.error_code,
                    max_deferred_attempts=exc.max_deferred_attempts,
                    deferred_backoff_base_seconds=exc.deferred_backoff_base_seconds,
                ) from exc
            except ProviderOperationError as exc:
                if exc.code != "unavailable" or not exc.retryable:
                    raise
                raise ThumbnailDeferred(
                    "媒体提供方暂不可用",
                    error_code=exc.code,
                    max_deferred_attempts=cls.MAX_DEFERRED_ATTEMPTS,
                    deferred_backoff_base_seconds=cls.DEFERRED_BACKOFF_BASE_SECONDS,
                ) from exc
            if progress_callback:
                progress_callback("正在校验并保存缩略图")
            expected_count = int(generation.expected_count)
            if expected_count < 0:
                raise RuntimeError("thumbnail_expected_count_invalid")
            valid_artifacts = []
            offsets: set[int] = set()
            for artifact in generation.artifacts:
                if artifact.offset_seconds in offsets:
                    continue
                try:
                    source = ThumbnailArtifactService.validate_artifact(provider_workspace, artifact)
                except ValueError as exc:
                    logger.warning(
                        "Invalid thumbnail artifact media_id={} path={} detail={}",
                        media.id,
                        artifact.relative_path,
                        exc,
                    )
                    continue
                offsets.add(artifact.offset_seconds)
                valid_artifacts.append((artifact, source))
            minimum_count = cls.minimum_acceptable_count(expected_count)
            if len(valid_artifacts) < minimum_count:
                raise RuntimeError(
                    "thumbnail_generation_insufficient_count "
                    f"expected={expected_count} minimum={minimum_count} "
                    f"actual={len(valid_artifacts)}"
                )
            batch = ThumbnailArtifactService.prepare_batch(store, valid_artifacts, workspace)
            return ThumbnailArtifactService.persist_batch(
                media, batch, check_connection=check_connection, progress_callback=progress_callback,
            )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        error_code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
        if isinstance(error_code, str) and error_code.strip():
            return error_code.strip()[:64]
        detail = str(exc).strip()
        if detail:
            return detail.split(maxsplit=1)[0].split(":", maxsplit=1)[0][:64]
        return type(exc).__name__.lower()[:64]

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        return (str(exc).strip() or type(exc).__name__)[:4000]

    @classmethod
    def _write_state(
        cls,
        media: Media,
        *,
        state: str,
        attempt_count: int,
        deferred_count: int,
        next_retry_at,
        error_code: str | None,
        error_detail: str | None,
        terminal_at,
    ) -> None:
        Media.update(
            thumbnail_generation_state=state,
            thumbnail_attempt_count=attempt_count,
            thumbnail_deferred_count=deferred_count,
            thumbnail_next_retry_at=next_retry_at,
            thumbnail_last_error_code=error_code,
            thumbnail_last_error=error_detail,
            thumbnail_terminal_at=terminal_at,
            updated_at=utc_now_for_db(),
        ).where(Media.id == media.id).execute()

    @classmethod
    def _mark_succeeded(cls, media: Media) -> None:
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_SUCCEEDED,
            attempt_count=0,
            deferred_count=0,
            next_retry_at=None,
            error_code=None,
            error_detail=None,
            terminal_at=None,
        )

    @classmethod
    def _mark_deferred(cls, media: Media, exc: ThumbnailDeferred) -> bool:
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0)
        deferred_count = int(media.thumbnail_deferred_count or 0) + 1
        if deferred_count > exc.max_deferred_attempts:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=deferred_count,
                next_retry_at=None,
                error_code=cls._error_code(exc),
                error_detail=cls._error_detail(exc),
                terminal_at=now,
            )
            return True
        backoff_seconds = min(
            exc.deferred_backoff_base_seconds * deferred_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=deferred_count,
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=cls._error_code(exc),
            error_detail=cls._error_detail(exc),
            terminal_at=None,
        )
        return False

    @classmethod
    def _mark_failure(cls, media: Media, exc: Exception) -> bool:
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0) + 1
        error_code = cls._error_code(exc)
        retryable = getattr(exc, "retryable", True)
        is_terminal = (
            not retryable
            or error_code in cls.TERMINAL_ERROR_CODES
            or attempt_count >= cls.MAX_FAILURE_ATTEMPTS
        )
        if is_terminal:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=int(media.thumbnail_deferred_count or 0),
                next_retry_at=None,
                error_code=error_code,
                error_detail=cls._error_detail(exc),
                terminal_at=now,
            )
            return True
        backoff_seconds = min(
            cls.FAILURE_RETRY_BACKOFF_BASE_SECONDS * attempt_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=int(media.thumbnail_deferred_count or 0),
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=error_code,
            error_detail=cls._error_detail(exc),
            terminal_at=None,
        )
        return False

    @classmethod
    def _generate_one(cls, media_id: int, progress_callback=None) -> ThumbnailGenerationOutcome:
        ensure_database_ready()
        try:
            with media_operation_lock(MEDIA_LOCK, media_id) as check_connection:
                return cls._generate_one_locked(
                    media_id, progress_callback, check_connection=check_connection,
                )
        except MediaOperationBusy:
            return ThumbnailGenerationOutcome("skipped")

    @staticmethod
    def _has_thumbnails(media: Media) -> bool:
        return MediaThumbnail.select().where(MediaThumbnail.media == media).exists()

    @classmethod
    def _reset_for_request(cls, media: Media) -> None:
        """显式请求等同人工重试：清零失败与延后计数、撤销退避，然后立即生成。"""
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_PENDING,
            attempt_count=0,
            deferred_count=0,
            next_retry_at=None,
            error_code=None,
            error_detail=None,
            terminal_at=None,
        )
        # 失败收口按实例上的计数累加，必须与刚写入的状态保持一致。
        media.thumbnail_generation_state = Media.THUMBNAIL_STATE_PENDING
        media.thumbnail_attempt_count = 0
        media.thumbnail_deferred_count = 0

    @classmethod
    def generate_requested_media(cls, media_id: int, *, progress_callback=None) -> ThumbnailGenerationOutcome:
        """按显式请求立即为单条媒体生成缩略图，不经过批量候选筛选。

        忽略退避时间与终态；已有缩略图时不会重建。生成失败后沿用常规的重试/终态
        策略，由定时任务继续接手。``progress_callback(text)`` 接收进度文本，耗时步骤
        期间会由心跳线程重复回调并追加等待秒数。
        """
        ensure_database_ready()
        try:
            with media_operation_lock(MEDIA_LOCK, media_id) as check_connection:
                media = Media.get_or_none(Media.id == media_id)
                if media is None:
                    return ThumbnailGenerationOutcome("not_found")
                if not media.valid:
                    return ThumbnailGenerationOutcome("invalid")
                if cls._has_thumbnails(media):
                    ThumbnailArtifactService.cleanup_committed(media, check_connection=check_connection)
                    cls._mark_succeeded(media)
                    return ThumbnailGenerationOutcome("already_exists")
                cls._reset_for_request(media)
                if progress_callback is None:
                    return cls._generate_loaded_media(media, check_connection=check_connection)
                with ThumbnailTaskProgress(_TextProgressSink(progress_callback)) as progress:
                    progress.emit(text="正在准备视频")
                    return cls._generate_loaded_media(
                        media,
                        lambda action: progress.emit(text=action, force=False),
                        check_connection=check_connection,
                    )
        except MediaOperationBusy:
            return ThumbnailGenerationOutcome("busy")

    @classmethod
    def _generate_one_locked(
        cls, media_id: int, progress_callback=None, *, check_connection=None,
    ) -> ThumbnailGenerationOutcome:
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid:
            return ThumbnailGenerationOutcome("skipped")
        if cls._has_thumbnails(media):
            ThumbnailArtifactService.cleanup_committed(media, check_connection=check_connection)
            cls._mark_succeeded(media)
            return ThumbnailGenerationOutcome("skipped")
        return cls._generate_loaded_media(
            media, progress_callback, check_connection=check_connection,
        )

    @classmethod
    def _generate_loaded_media(
        cls, media: Media, progress_callback=None, *, check_connection=None,
    ) -> ThumbnailGenerationOutcome:
        """调用方已持有媒体锁并确认媒体有效且尚无缩略图。"""
        media_id = media.id
        try:
            generated_count = cls._generate_artifacts(
                media, progress_callback, check_connection=check_connection,
            )
        except MediaOperationBusy:
            raise
        except ThumbnailBackendUnavailable as exc:
            logger.warning(
                "Media thumbnail backend unavailable media_id={} code={} detail={}",
                media_id,
                exc.error_code,
                exc,
            )
            return ThumbnailGenerationOutcome("backend_unavailable", error_code=exc.error_code)
        except ProviderUnavailableError:
            if check_connection is not None:
                check_connection()
            deferred = ThumbnailDeferred(
                "媒体提供方暂不可用",
                error_code="provider_not_installed",
                max_deferred_attempts=cls.MAX_DEFERRED_ATTEMPTS,
                deferred_backoff_base_seconds=cls.DEFERRED_BACKOFF_BASE_SECONDS,
            )
            terminal = cls._mark_deferred(media, deferred)
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "deferred",
                error_code=deferred.error_code,
            )
        except ThumbnailDeferred as exc:
            if check_connection is not None:
                check_connection()
            terminal = cls._mark_deferred(media, exc)
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "deferred",
                error_code=cls._error_code(exc),
            )
        except Exception as exc:
            # An old worker that lost its advisory-lock session cannot write state.
            if check_connection is not None:
                check_connection()
            if cls._has_thumbnails(media):
                cls._mark_succeeded(media)
                return ThumbnailGenerationOutcome("succeeded")
            terminal = cls._mark_failure(media, exc)
            logger.warning(
                "Media thumbnail generation failed media_id={} code={} terminal={} detail={}",
                media_id,
                cls._error_code(exc),
                terminal,
                exc,
            )
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "retryable_failed",
                error_code=cls._error_code(exc),
            )
        if check_connection is not None:
            check_connection()
        cls._mark_succeeded(media)
        return ThumbnailGenerationOutcome("succeeded", generated_count=generated_count)

    @classmethod
    def generate_pending_thumbnails(cls, *, reporter) -> dict[str, Any]:
        with ThumbnailTaskProgress(reporter) as progress:
            return cls._generate_pending_thumbnails(progress)

    @classmethod
    def _generate_pending_thumbnails(cls, reporter) -> dict[str, Any]:
        started_at = time.time()
        reporter.emit(
            current=0, total=0,
            text="阶段 1/2 · 查找待处理媒体 · 正在查询候选",
        )
        entries = cls._candidate_entries()
        reporter.emit(
            current=len(entries), total=len(entries),
            text=f"阶段 1/2 · 查找待处理媒体 · 查询完成 · 待处理 {len(entries)} 部",
        )
        logger.info("Media thumbnail generation started pending_media={}", len(entries))
        step = max(len(entries) // 20, 1)
        stats: dict[str, Any] = {
            "pending_media": len(entries),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "deferred_media": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
            "failed_media_ids": [],
            "terminal_failed_media_ids": [],
            "backend_failed_lanes": 0,
            "backend_deferred_media": 0,
            "backend_failure_codes": [],
            "skipped_media": 0,
        }
        paused_lanes: set[tuple[str, int]] = set()

        def emit_progress(completed: int, action: str, *, force=True) -> None:
            reporter.emit(
                force=force,
                current=completed,
                total=len(entries),
                text=(
                    f"阶段 2/2 · 生成媒体缩略图 · {action}"
                    f" · 已处理 {completed}/{len(entries)} 部"
                    f" · 成功 {stats['successful_media']}"
                    f" · 延后 {stats['deferred_media'] + stats['backend_deferred_media']}"
                    f" · 失败 {stats['retryable_failed_media'] + stats['terminal_failed_media']}"
                    f" · 跳过 {stats['skipped_media']}"
                ),
                summary_patch=stats,
            )

        emit_progress(0, "开始处理" if entries else "任务完成 · 无待处理媒体")
        for completed, (media_id, lane) in enumerate(entries, start=1):
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Media thumbnail generation progress completed={}/{} successful={} deferred={} failed={}",
                    completed,
                    len(entries),
                    stats["successful_media"],
                    stats["deferred_media"] + stats["backend_deferred_media"],
                    stats["retryable_failed_media"] + stats["terminal_failed_media"],
                )
            if lane in paused_lanes:
                stats["backend_deferred_media"] += 1
                emit_progress(completed, f"媒体 {media_id} 所属媒体库暂不可用，已延后")
                continue
            emit_progress(completed - 1, f"媒体 {media_id} · 正在准备视频")
            outcome = cls._generate_one(
                media_id,
                progress_callback=lambda action, completed=completed, media_id=media_id: emit_progress(
                    completed - 1, f"媒体 {media_id} · {action}", force=False,
                ),
            )
            if outcome.state == "backend_unavailable":
                paused_lanes.add(lane)
                stats["backend_failed_lanes"] += 1
                stats["backend_deferred_media"] += 1
                if outcome.error_code:
                    stats["backend_failure_codes"].append(outcome.error_code)
            elif outcome.state == "succeeded":
                stats["successful_media"] += 1
                stats["generated_thumbnails"] += outcome.generated_count
            elif outcome.state == "deferred":
                stats["deferred_media"] += 1
            elif outcome.state == "retryable_failed":
                stats["retryable_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
            elif outcome.state == "terminal_failed":
                stats["terminal_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
                stats["terminal_failed_media_ids"].append(media_id)
            else:
                stats["skipped_media"] += 1
            emit_progress(completed, f"媒体 {media_id} 处理结束")
        if entries:
            emit_progress(len(entries), "任务完成")
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} "
            "generated_thumbnails={} terminal_failed_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["terminal_failed_media"],
            int((time.time() - started_at) * 1000),
        )
        return stats


__all__ = ["MediaThumbnailTaskService", "ThumbnailGenerationOutcome"]
