from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.common import build_signed_subtitle_url
from src.common.media_paths import MOVIE_SUBTITLE_EXTENSIONS, movie_asset_relative_dir, normalize_asset_dir_name
from src.common.service_helpers import require_record
from src.common.subtitle_paths import movie_subtitle_storage_key
from src.model import Movie, Subtitle
from src.schema.catalog.subtitles import (
    MovieSubtitleItemResource,
    MovieSubtitleListResource,
)
from src.storage import StorageNotFound, asset_storage


class MovieSubtitleService:
    @classmethod
    def get_movie_subtitles(cls, movie_number: str) -> MovieSubtitleListResource:
        movie = require_record(
            Movie,
            Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="影片不存在",
            error_details={"movie_number": movie_number},
        )
        # 读接口保持纯读，只返回当前仍然可访问的字幕项。
        items = cls._build_subtitle_items(movie)
        return MovieSubtitleListResource(
            movie_number=movie.movie_number,
            items=items,
        )

    @classmethod
    def sync_movie_subtitles(cls, movie: Movie) -> dict[str, int]:
        discovered_paths = cls._discover_subtitle_paths(movie)
        existing_items = list(cls._subtitle_query(movie))
        existing_by_path: dict[str, Subtitle] = {}
        deleted_count = 0

        # 先清理已经失效的字幕记录，避免后续列表继续暴露坏链接。
        for subtitle in existing_items:
            try:
                normalized_path = movie_subtitle_storage_key(movie, subtitle.file_path)
            except ApiError:
                subtitle.delete_instance()
                deleted_count += 1
                continue
            if not asset_storage().exists(normalized_path):
                subtitle.delete_instance()
                deleted_count += 1
                continue
            existing_by_path[normalized_path] = subtitle

        created_count = 0
        for subtitle_path in discovered_paths:
            key = str(subtitle_path)
            if key in existing_by_path:
                continue
            existing_by_path[key] = Subtitle.create(movie=movie, file_path=key)
            created_count += 1

        return {
            "created_subtitles": created_count,
            "deleted_subtitles": deleted_count,
            "total_subtitles": len(existing_by_path),
        }

    @staticmethod
    def _subtitle_query(movie: Movie):
        return (
            Subtitle.select(Subtitle)
            .where(Subtitle.movie == movie)
            .order_by(Subtitle.created_at.desc(), Subtitle.id.desc())
        )

    @classmethod
    def _build_subtitle_items(cls, movie: Movie) -> list[MovieSubtitleItemResource]:
        items: list[MovieSubtitleItemResource] = []
        for subtitle in cls._subtitle_query(movie):
            try:
                key = movie_subtitle_storage_key(movie, subtitle.file_path)
            except ApiError:
                continue
            try:
                stat = asset_storage().stat(key)
            except StorageNotFound:
                continue
            if not stat.is_file: continue
            items.append(
                MovieSubtitleItemResource(
                    subtitle_id=subtitle.id,
                    url=build_signed_subtitle_url(subtitle.id),
                    created_at=subtitle.created_at,
                    file_name=Path(key).name,
                )
            )
        return items

    @classmethod
    def _discover_subtitle_paths(cls, movie: Movie) -> list[str]:
        prefix = movie_asset_relative_dir(normalize_asset_dir_name(movie.movie_number)) / "subtitles"
        return sorted(
            (item.key for item in asset_storage().list(prefix.as_posix()) if Path(item.key).suffix.lower() in MOVIE_SUBTITLE_EXTENSIONS),
            key=str.lower,
        )
