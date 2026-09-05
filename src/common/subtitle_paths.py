from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.common.media_paths import MOVIE_SUBTITLE_EXTENSIONS, movie_subtitle_dir
from src.common.media_paths import media_image_root_path, movie_asset_relative_dir, normalize_asset_dir_name
from src.storage.keys import normalize_storage_key


def normalize_subtitle_path(file_path: str | Path) -> Path:
    absolute_path = Path(file_path).expanduser()
    if not absolute_path.is_absolute():
        absolute_path = (Path.cwd() / absolute_path).resolve()
    else:
        absolute_path = absolute_path.resolve()
    if absolute_path.suffix.lower() not in MOVIE_SUBTITLE_EXTENSIONS:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return absolute_path


def _is_path_within_root(file_path: Path, root_path: Path) -> bool:
    try:
        file_path.relative_to(root_path)
    except ValueError:
        return False
    return True


def ensure_movie_subtitle_path(movie, file_path: str | Path) -> Path:
    """校验字幕绝对路径位于该影片的标准字幕目录内。"""
    absolute_path = normalize_subtitle_path(file_path)
    if _is_path_within_root(absolute_path, movie_subtitle_dir(movie.movie_number).resolve()):
        return absolute_path
    raise ApiError(403, "file_path_invalid", "文件路径非法")


def movie_subtitle_storage_key(movie, file_path: str | Path) -> str:
    """Accept a new relative key or translate a legacy absolute subtitle path."""
    raw = str(file_path)
    path = Path(raw).expanduser()
    if path.is_absolute():
        try:
            raw = path.resolve().relative_to(media_image_root_path().resolve()).as_posix()
        except ValueError as exc:
            raise ApiError(403, "file_path_invalid", "文件路径非法") from exc
    try:
        key = normalize_storage_key(raw)
    except ValueError as exc:
        raise ApiError(403, "file_path_invalid", "文件路径非法") from exc
    prefix = movie_asset_relative_dir(normalize_asset_dir_name(movie.movie_number)) / "subtitles"
    key_path = Path(*key.split("/"))
    if key_path.suffix.lower() not in MOVIE_SUBTITLE_EXTENSIONS or not key.startswith(f"{prefix.as_posix()}/"):
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return key
