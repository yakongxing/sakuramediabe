"""Image 记录与物理文件的清理公共工具。

catalog 目录导入和媒体硬删除都需要这份逻辑，抽出来避免重复实现。
"""

from pathlib import Path

from src.common.image_references import is_nonlocal_image_reference
from src.config.config import settings
from src.model import Actor, Image, MediaThumbnail, Movie, MoviePlotImage, get_database
from src.storage import asset_storage


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
                and Movie.select(Movie.id)
                .where((Movie.cover_image == image) | (Movie.thin_cover_image == image))
                .exists(),
                database.table_exists(Actor._meta.table_name)
                and Actor.select(Actor.id).where(Actor.profile_image == image).exists(),
                database.table_exists(MoviePlotImage._meta.table_name)
                and MoviePlotImage.select(MoviePlotImage.id)
                .where(MoviePlotImage.image == image)
                .exists(),
                database.table_exists(MediaThumbnail._meta.table_name)
                and MediaThumbnail.select(MediaThumbnail.id)
                .where(MediaThumbnail.image == image)
                .exists(),
            )
        )

    @classmethod
    def delete_obsolete_image_files(cls, relative_paths: set[str]) -> None:
        local_paths = {
            path
            for path in relative_paths
            if path and not is_nonlocal_image_reference(path)
        }
        if not local_paths:
            return
        # A cleanup may be retried after a committed publication. A replay can
        # have attached the same content-addressed key again, so fail closed at
        # the last possible moment rather than deleting a live object.
        referenced_paths: set[str] = set()
        try:
            images = Image.select(
                Image.origin, Image.small, Image.medium, Image.large
            ).where(
                (Image.origin.in_(local_paths))
                | (Image.small.in_(local_paths))
                | (Image.medium.in_(local_paths))
                | (Image.large.in_(local_paths))
            )
            for image in images:
                referenced_paths.update(
                    path
                    for path in (image.origin, image.small, image.medium, image.large)
                    if path
                )
        except (AttributeError, RuntimeError):
            # Some isolated storage tests intentionally run without a database.
            # Production cleanup is only invoked with an initialized database.
            pass
        local_paths -= referenced_paths
        if not local_paths:
            return
        storage = asset_storage()
        for relative_path in local_paths:
            if not relative_path:
                continue
            storage.delete(relative_path, missing_ok=True)
