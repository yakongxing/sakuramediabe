"""字幕资产写入的稳定操作集合。

目录导入与插件共用同一实现：查影片 → 扩展名校验 → 内容指纹去重 →
落 ``movies/<shard>/<番号>/subtitles/`` → 登记 Subtitle 行。
插件通过 ``PluginContext.import_subtitle`` 调用，不直接触碰路径与登记细节。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger
from peewee import IntegrityError

from src.common.media_paths import (
    MOVIE_SUBTITLE_EXTENSIONS,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.common.service_helpers import find_movie_by_number
from src.common.subtitle_paths import movie_subtitle_storage_key
from src.model import Movie, Subtitle, get_database
from src.schema.catalog.subtitles import (
    SubtitleImportResult,
    SubtitleImportStatus,
)
from src.service.playback.operation_locks import subtitle_operation_lock
from src.storage import StorageNotFound, subtitle_storage
from src.storage.subtitles import LocalSubtitleStorage
from src.storage.types import StorageUnavailable


def _prepare_movie_subtitle_target_path(
    movie_number: str, *, extension: str = ".srt", reserved_paths=(), movie=None,
) -> str:
    normalized_extension = extension.lower()
    if normalized_extension not in MOVIE_SUBTITLE_EXTENSIONS:
        raise ValueError("invalid subtitle extension")
    prefix = movie_asset_relative_dir(normalize_asset_dir_name(movie_number)) / "subtitles"
    paths = [item.key for item in subtitle_storage().list(prefix.as_posix())]
    paths.extend(reserved_paths)
    if movie is not None:
        paths.extend(
            row.file_path for row in Subtitle.select(Subtitle.file_path).where(Subtitle.movie == movie)
        )
    maximum = 0
    head = f"{movie_number}-"
    for path in paths:
        stem = Path(path).stem
        if stem.startswith(head) and stem[len(head):].isdigit():
            maximum = max(maximum, int(stem[len(head):]))
    return f"{prefix.as_posix()}/{movie_number}-{maximum + 1}{normalized_extension}"


class SubtitleAssetService:
    """字幕资产写入/登记的唯一实现入口。"""

    CREATE_ATTEMPTS = 3

    @classmethod
    def movie_subtitle_hashes(cls, movie) -> set[str]:
        """该影片已登记字幕的内容指纹集合；存储不可用不能当作缺失。"""
        hashes: set[str] = set()
        storage = subtitle_storage()
        for subtitle in Subtitle.select().where(Subtitle.movie == movie):
            try:
                key = movie_subtitle_storage_key(movie, subtitle.file_path)
            except Exception as exc:
                logger.warning(
                    "Subtitle path invalid movie_id={} subtitle_id={} detail={}",
                    movie.id, subtitle.id, exc,
                )
                continue
            try:
                with storage.open(key) as handle:
                    hashes.add(cls._sha256_stream(handle))
            except StorageNotFound:
                continue
            except StorageUnavailable:
                if not isinstance(storage, LocalSubtitleStorage):
                    raise
                # 本地覆盖模式允许旧远端副本暂不可用，不影响新字幕写本地。
        return hashes

    @classmethod
    def _publish_subtitle(cls, movie, suffix, publish, check_connection):
        storage = subtitle_storage()
        reserved: set[str] = set()
        for attempt in range(cls.CREATE_ATTEMPTS):
            target = _prepare_movie_subtitle_target_path(
                movie.movie_number, extension=suffix, reserved_paths=reserved, movie=movie,
            )
            reserved.add(target)
            check_connection()
            try:
                publication = publish(storage, target)
            except FileExistsError:
                if attempt + 1 == cls.CREATE_ATTEMPTS:
                    raise
                continue
            try:
                check_connection()
                with get_database().atomic():
                    subtitle = Subtitle.create(movie=movie, file_path=target)
                    check_connection()
            except IntegrityError:
                # 兼容绕过本锁的登记者；相同指针只能有一条记录，文件仍保留。
                check_connection()
                existing = Subtitle.get_or_none(
                    (Subtitle.movie == movie) & (Subtitle.file_path == target)
                )
                if existing is not None:
                    return existing, target
                cls._cleanup_unregistered(storage, target, publication, check_connection)
                raise
            except Exception:
                cls._cleanup_unregistered(storage, target, publication, check_connection)
                raise
            return subtitle, target
        raise AssertionError("unreachable")

    @staticmethod
    def _cleanup_unregistered(storage, key, publication, check_connection):
        if not getattr(publication, "created", False):
            return
        try:
            check_connection()
            if Subtitle.select().where(Subtitle.file_path == key).exists():
                return
            # 仅本次严格创建且登记回滚的对象可补偿；锁/数据库不确定时保留。
            storage.delete(key, missing_ok=True)
        except Exception as exc:
            logger.warning("Subtitle compensation deferred key={} detail={}", key, exc)

    @classmethod
    def import_subtitle_content(
        cls,
        movie_number: str,
        content: bytes,
        filename: str,
        language: str | None = None,
    ) -> SubtitleImportResult:
        """按番号写入一段字幕内容（插件下载场景）。"""
        del language  # Subtitle 模型暂无语言列，参数保留供后续版本使用。
        movie = find_movie_by_number(movie_number)
        if movie is None:
            return SubtitleImportResult(
                status=SubtitleImportStatus.MOVIE_NOT_FOUND,
                reason=f"影片不存在: {movie_number}",
            )
        suffix = Path(filename or "").suffix.lower()
        if suffix not in MOVIE_SUBTITLE_EXTENSIONS:
            return SubtitleImportResult(
                status=SubtitleImportStatus.INVALID_FORMAT,
                reason=f"不支持的扩展名: {suffix or '无'}（支持 {', '.join(MOVIE_SUBTITLE_EXTENSIONS)}）",
            )
        content_hash = cls._sha256_bytes(content)
        with subtitle_operation_lock(movie.id) as check_connection:
            movie = Movie.get_or_none(Movie.id == movie.id)
            if movie is None:
                return SubtitleImportResult(
                    status=SubtitleImportStatus.MOVIE_NOT_FOUND,
                    reason=f"影片不存在: {movie_number}",
                )
            if content_hash in cls.movie_subtitle_hashes(movie):
                return SubtitleImportResult(status=SubtitleImportStatus.DUPLICATE)
            subtitle, _ = cls._publish_subtitle(
                movie, suffix,
                lambda storage, target: storage.put_bytes(target, content, overwrite=False),
                check_connection,
            )
            return SubtitleImportResult(
                status=SubtitleImportStatus.IMPORTED,
                subtitle_id=subtitle.id,
            )

    @classmethod
    def register_subtitle_file(
        cls,
        movie,
        source_path: Path,
        *,
        existing_hashes: dict[int, set[str]] | None = None,
        transfer_mode: str = "auto",
    ) -> tuple[str, str, str]:
        """登记一个本地字幕文件（目录导入场景），返回 (status, reason, detail)。"""
        del transfer_mode
        content_hash = cls._sha256_file(source_path)
        with subtitle_operation_lock(movie.id) as check_connection:
            movie = Movie.get_by_id(movie.id)
            # 外部批次缓存可能早于另一个进程的写入，必须在锁内刷新。
            hashes = cls.movie_subtitle_hashes(movie)
            if existing_hashes is not None:
                existing_hashes[movie.id] = hashes
            if content_hash in hashes:
                return "skipped", "duplicate_fingerprint", source_path.name
            _, target = cls._publish_subtitle(
                movie, source_path.suffix.lower(),
                lambda storage, key: storage.put_file(key, source_path, overwrite=False),
                check_connection,
            )
            hashes.add(content_hash)
            return "imported", "", target

    @staticmethod
    def _sha256_file(file_path: Path) -> str:
        with file_path.open("rb") as handle:
            return SubtitleAssetService._sha256_stream(handle)

    @staticmethod
    def _sha256_stream(handle) -> str:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sha256_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()
