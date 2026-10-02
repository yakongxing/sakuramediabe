"""Image 记录与物理文件的清理公共工具。

catalog 目录导入和媒体硬删除都需要这份逻辑，抽出来避免重复实现。
缩略图可能存放在 ``thumbnails.zip``、影片图片可能存放在 ``assets.zip``：
包内条目按数据库引用决定保留或重建；未打包的旧布局维持逐个文件删除。
"""

import os
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from loguru import logger

from src.common.image_references import is_nonlocal_image_reference
from src.common.image_store import thumbnail_generation_pack_key, write_pack
from src.common.media_paths import MOVIE_ASSETS_PACK_NAME, image_pack_relative_path
from src.config.config import settings
from src.model import (
    Actor,
    Image,
    MediaPoint,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
    VideoItem,
    get_database,
)
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.storage import asset_storage
from src.storage.keys import normalize_storage_key


class ImageCleanupService:
    @staticmethod
    def image_root_path() -> Path:
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    @classmethod
    def delete_image_record_if_unused(cls, image: Image | None) -> set[str]:
        if image is None:
            return set()
        if cls.image_record_is_still_used(image):
            return set()
        relative_path = image.origin
        image.delete_instance()
        return {relative_path} if relative_path else set()

    @staticmethod
    def image_record_is_still_used(image: Image) -> bool:
        database = get_database()
        return any(
            (
                database.table_exists(Movie._meta.table_name)
                and Movie.select(Movie.id).where(
                    (Movie.cover_image == image) | (Movie.thin_cover_image == image)
                ).exists(),
                database.table_exists(Actor._meta.table_name)
                and Actor.select(Actor.id)
                .where(
                    (Actor.profile_image == image)
                    | (Actor.profile_image_override == image)
                )
                .exists(),
                database.table_exists(MoviePlotImage._meta.table_name)
                and MoviePlotImage.select(MoviePlotImage.id).where(MoviePlotImage.image == image).exists(),
                database.table_exists(MediaThumbnail._meta.table_name)
                and MediaThumbnail.select(MediaThumbnail.id).where(MediaThumbnail.image == image).exists(),
                database.table_exists(MediaPoint._meta.table_name)
                and MediaPoint.select(MediaPoint.id).where(MediaPoint.image == image).exists(),
                database.table_exists(VideoItem._meta.table_name)
                and VideoItem.select(VideoItem.id).where(VideoItem.cover_image == image).exists(),
            )
        )

    @classmethod
    def delete_obsolete_image_files(cls, relative_paths: set[str]) -> None:
        relative_paths = {normalize_storage_key(path) for path in relative_paths
                          if path and not is_nonlocal_image_reference(path)}
        if not relative_paths:
            return
        if get_database().in_transaction():
            logger.warning("Image file cleanup deferred until database transaction commits")
            return
        referenced = {row.origin for row in Image.select(Image.origin).where(Image.origin.in_(relative_paths))}
        relative_paths -= referenced
        if not relative_paths:
            return
        storage = asset_storage()
        if settings.storage.backend != "local" or not hasattr(storage, "local_path"):
            packs = set()
            for path in relative_paths:
                pack = thumbnail_generation_pack_key(path)
                if pack is not None:
                    packs.add(pack)
                # Also clean loose files left by earlier generations.
                storage.delete(path, missing_ok=True)
            for pack in packs:
                prefix = f"{PurePosixPath(pack).with_suffix('')}/"
                if not Image.select().where(Image.origin.startswith(prefix)).exists():
                    storage.delete(pack, missing_ok=True)
            return
        image_root = cls.image_root_path()
        pack_members: dict[PurePosixPath, list[str]] = {}
        for relative_path in sorted(relative_paths):
            if not relative_path:
                continue
            pack_relative = image_pack_relative_path(relative_path)
            if pack_relative is None:
                cls._unlink_image_file(image_root / relative_path)
                continue
            pack_members.setdefault(pack_relative, []).append(relative_path)

        for pack_relative, members in pack_members.items():
            cls._delete_or_rebuild_pack(image_root, pack_relative, members)

    @staticmethod
    def _unlink_image_file(target_path: Path) -> None:
        try:
            target_path.unlink()
        except FileNotFoundError:
            return

    @classmethod
    def _delete_or_rebuild_pack(
        cls, image_root: Path, pack_relative: PurePosixPath, members: list[str]
    ) -> None:
        pack_path = image_root / pack_relative
        if not pack_path.is_file():
            # 尚未打包的旧布局：维持逐个文件删除。
            for relative_path in members:
                cls._unlink_image_file(image_root / relative_path)
            return

        if pack_relative.name == MOVIE_ASSETS_PACK_NAME:
            # 影片图片包：以数据库活跃集为准重建；活跃集为空时由服务删除包。
            MovieAssetPackService.rebuild_movie_asset_pack(pack_relative.parent)
            # Rebuilding only removes live packed files, not a losing publisher's
            # unreferenced loose objects. Only these explicit obsolete keys are ours.
            for relative_path in members:
                cls._unlink_image_file(image_root / relative_path)
            return

        thumbnails_prefix = f"{pack_relative.parent / pack_relative.stem}/"
        remaining_origins = [
            origin
            for (origin,) in Image.select(Image.origin)
            .where(Image.origin.startswith(thumbnails_prefix))
            .tuples()
            if origin.startswith(thumbnails_prefix)
        ]
        if not remaining_origins:
            cls._unlink_image_file(pack_path)
            return
        # 还有被时刻等钉住的条目：重建包，维持"包 == 数据库活跃集合"。
        cls._rebuild_thumbnail_pack(pack_path, remaining_origins)

    @staticmethod
    def _rebuild_thumbnail_pack(pack_path: Path, origins: list[str]) -> None:
        tmp_path = pack_path.with_name(f"{pack_path.name}.tmp-{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(pack_path) as source:
                entries: list[tuple[str, bytes]] = []
                for origin in origins:
                    entry_name = PurePosixPath(origin).name
                    try:
                        entries.append((entry_name, source.read(entry_name)))
                    except KeyError:
                        logger.warning(
                            "Thumbnail pack entry missing pack={} entry={}",
                            pack_path,
                            entry_name,
                        )
                        return
            if not entries:
                # 包内没有任何存活条目属于异常状态：保留旧包，不破坏现场。
                logger.warning("Rebuild thumbnail pack skipped pack={}", pack_path)
                return
            write_pack(tmp_path, entries)
            os.replace(tmp_path, pack_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "Rebuild thumbnail pack failed pack={} detail={}", pack_path, exc
            )
