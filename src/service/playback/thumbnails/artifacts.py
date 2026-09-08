from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from loguru import logger
from PIL import Image as PILImage

from src.common.image_references import is_nonlocal_image_reference
from src.common.media_paths import (
    MOVIE_MEDIA_SUBDIR,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.config import settings
from src.model import Image, Media, MediaThumbnail, get_database
from src.plugins.provider_protocol import ThumbnailArtifact
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.storage import asset_storage
from src.storage.types import StoragePublicationUnknown


class ThumbnailArtifactService:
    @staticmethod
    def thumbnail_prefix(media: Media) -> str:
        namespace = (
            PurePosixPath(
                movie_asset_relative_dir(normalize_asset_dir_name(media.movie_number))
            )
            if media.movie_number
            else PurePosixPath("videos") / str(media.video_item_id)
        )
        return (namespace / MOVIE_MEDIA_SUBDIR / str(media.id) / "thumbnails").as_posix()

    @staticmethod
    def _workspace_file(workspace: Path, relative_path: str) -> Path:
        normalized = (relative_path or "").strip().replace("\\", "/")
        if not normalized or normalized.startswith("/"):
            raise ValueError("thumbnail_artifact_path_invalid")
        parts = normalized.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("thumbnail_artifact_path_invalid")
        candidate = (workspace / PurePosixPath(*parts)).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ValueError("thumbnail_artifact_path_invalid") from exc
        return candidate

    @classmethod
    def validate_artifact(
        cls,
        workspace: Path,
        artifact: ThumbnailArtifact,
    ) -> Path:
        if artifact.offset_seconds < 0:
            raise ValueError("thumbnail_offset_invalid")
        if not artifact.relative_path.lower().endswith(".webp"):
            raise ValueError("thumbnail_artifact_not_webp")
        source = cls._workspace_file(workspace, artifact.relative_path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("thumbnail_artifact_empty")
        try:
            with PILImage.open(source) as image:
                if image.format != "WEBP":
                    raise ValueError("thumbnail_artifact_not_webp")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("thumbnail_artifact_invalid") from exc
        return source

    @classmethod
    def persist(
        cls,
        media: Media,
        artifacts: list[tuple[ThumbnailArtifact, Path]],
    ) -> int:
        prefix = cls.thumbnail_prefix(media)
        storage = asset_storage()
        initial_index_status = (
            MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED
        )
        published: list[tuple[ThumbnailArtifact, str]] = []
        cleanup_keys: set[str] = set()
        try:
            # Publish outside the transaction; readers only see complete batches.
            errors: list[Exception] = []
            if artifacts:
                with ThreadPoolExecutor(
                    max_workers=min(settings.storage.webdav_publication_max_workers, len(artifacts)),
                    thread_name_prefix="thumbnail-publication",
                ) as executor:
                    futures = {
                        executor.submit(storage.put_file, f"{prefix}/{artifact.offset_seconds}.webp", source): artifact
                        for artifact, source in artifacts
                    }
                    for future in as_completed(futures):
                        artifact = futures[future]
                        key = f"{prefix}/{artifact.offset_seconds}.webp"
                        try:
                            future.result()
                        except StoragePublicationUnknown as exc:
                            logger.warning("Thumbnail publication unknown media_id={} key={}", media.id, key)
                            errors.append(exc)
                        except Exception as exc:
                            errors.append(exc)
                        else:
                            cleanup_keys.add(key)
                            published.append((artifact, key))
                if errors:
                    raise errors[0]
            published.sort(key=lambda item: item[0].offset_seconds)
            with get_database().atomic():
                for artifact, relative_path in published:
                    image = Image.create(
                        origin=relative_path,
                        small=relative_path,
                        medium=relative_path,
                        large=relative_path,
                    )
                    MediaThumbnail.create(
                        media=media,
                        image=image,
                        offset=artifact.offset_seconds,
                        image_search_index_status=initial_index_status,
                    )
        except Exception:
            for key in cleanup_keys:
                try:
                    ImageCleanupService.delete_obsolete_image_files({key})
                except Exception as exc:
                    logger.warning(
                        "Thumbnail cleanup failed media_id={} key={} detail={}",
                        media.id, key, exc,
                    )
            raise
        return len(published)

    @staticmethod
    def read_dimensions(image_origin: str) -> tuple[int | None, int | None]:
        if is_nonlocal_image_reference(image_origin):
            raise ValueError("thumbnail_image_reference_nonlocal")
        with asset_storage().open(image_origin) as stream, PILImage.open(stream) as image:
            return image.size

    @classmethod
    def list_media_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media_id)
            .order_by(MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        width, height = None, None
        if thumbnails:
            try:
                width, height = cls.read_dimensions(thumbnails[0].image.origin)
            except Exception as exc:
                logger.warning(
                    "Resolve media thumbnail dimensions failed media_id={} detail={}",
                    media_id,
                    exc,
                )
        return [
            MediaThumbnailResource(
                thumbnail_id=thumbnail.id,
                media_id=thumbnail.media_id,
                offset_seconds=thumbnail.offset,
                image=ImageResource.from_attributes_model(thumbnail.image),
                width=width,
                height=height,
            )
            for thumbnail in thumbnails
        ]
