import hashlib
import os
from collections.abc import Callable
from io import BytesIO
from pathlib import Path, PurePosixPath

from loguru import logger
from PIL import Image as PILImage

from src.common.image_references import is_nonlocal_image_reference
from src.common.image_store import read_image_bytes, write_pack
from src.common.media_paths import (
    MOVIE_MEDIA_SUBDIR,
    media_image_root_path,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.config import settings
from src.model import Image, Media, MediaThumbnail, get_database
from src.plugins.provider_protocol import ThumbnailArtifact
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource
from src.service.playback.thumbnails.batches import ThumbnailBatch, ThumbnailBatchStore
from src.service.playback.thumbnails.contracts import ThumbnailPublicationIncomplete
from src.storage import asset_storage
from src.storage.batch import publish_batch
from src.storage.local import LocalStorageBackend
from src.storage.types import StorageConflict, StorageError, StorageNotFound


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

    @classmethod
    def thumbnail_directory(cls, media: Media) -> Path:
        return media_image_root_path() / cls.thumbnail_prefix(media)

    @classmethod
    def thumbnail_pack_file(cls, media: Media) -> Path:
        return cls.thumbnail_directory(media).with_suffix(".zip")

    @staticmethod
    def prepare_batch(store, artifacts, workspace=None) -> ThumbnailBatch:
        packed = settings.storage.backend == "local" and isinstance(asset_storage(), LocalStorageBackend)
        return store.prepare(artifacts, workspace, packed=packed)

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
        *,
        check_connection: Callable[[], None] | None = None,
    ) -> int:
        with ThumbnailBatchStore(media).locked() as store:
            batch = store.load() or cls.prepare_batch(store, artifacts)
            return cls.persist_batch(media, batch, check_connection=check_connection)

    @classmethod
    def _committed_offsets(cls, media: Media, batch: ThumbnailBatch) -> set[int]:
        rows = list(MediaThumbnail.select(MediaThumbnail, Image).join(Image).where(
            MediaThumbnail.media == media.id,
        ))
        prefix = cls.thumbnail_prefix(media)
        expected = {(entry["offset"], batch.key(prefix, entry)) for entry in batch.entries}
        actual = {(row.offset, row.image.origin) for row in rows}
        if not actual.issubset(expected) or len(rows) != len(actual):
            raise StorageConflict("thumbnail_batch_database_conflict")
        return {row.offset for row in rows}

    @classmethod
    def _committed(cls, media: Media, batch: ThumbnailBatch) -> bool:
        return len(cls._committed_offsets(media, batch)) == len(batch.entries)

    @classmethod
    def _commit_entries(cls, media: Media, batch: ThumbnailBatch, entries, *, check_connection=None) -> None:
        if not entries:
            return
        if check_connection is not None:
            check_connection()
        prefix = cls.thumbnail_prefix(media)
        with get_database().atomic():
            current = Media.get_by_id(media.id)
            if not current.valid or ThumbnailBatchStore(current).identity != batch.store.identity:
                raise RuntimeError("thumbnail_batch_media_changed")
            for entry in entries:
                key = batch.key(prefix, entry)
                existing = MediaThumbnail.select(MediaThumbnail, Image).join(Image).where(
                    MediaThumbnail.media == media.id, MediaThumbnail.offset == entry["offset"],
                ).first()
                if existing is not None:
                    if existing.image.origin != key:
                        raise StorageConflict("thumbnail_batch_database_conflict")
                    continue
                image = Image.create(origin=key)
                MediaThumbnail.create(
                    media=media, image=image, offset=entry["offset"],
                    image_search_index_status=(
                        MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING if media.movie_number
                        else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED
                    ),
                )
            complete = cls._committed(media, batch)
            error_code = None if complete else ThumbnailPublicationIncomplete.ERROR_CODE
            Media.update(thumbnail_last_error_code=error_code).where(Media.id == media.id).execute()
            if check_connection is not None:
                check_connection()
        media.thumbnail_last_error_code = error_code

    @classmethod
    def cleanup_committed(cls, media: Media, *, check_connection=None) -> None:
        try:
            with ThumbnailBatchStore(media).locked() as store:
                batch = store.load()
                if check_connection is not None:
                    check_connection()
                if batch is not None and cls._committed(media, batch):
                    cls._cleanup_after_commit(batch)
        except (OSError, ValueError, StorageConflict) as exc:
            logger.warning("Thumbnail committed batch cleanup deferred media_id={} detail={}", media.id, exc)

    @staticmethod
    def _remote_matches(storage, key: str, entry) -> bool:
        try:
            stat = storage.stat(key)
        except StorageNotFound:
            return False
        if not stat.is_file or stat.size != entry["size"]:
            raise StorageConflict("thumbnail_batch_remote_content_conflict")
        digest = hashlib.sha256()
        size = 0
        with storage.open(key) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise StorageConflict("thumbnail_batch_remote_content_conflict")
        return True

    @classmethod
    def persist_batch(cls, media: Media, batch: ThumbnailBatch, *, check_connection=None, progress_callback=None) -> int:
        if check_connection is not None:
            check_connection()
        committed = cls._committed_offsets(media, batch)
        if len(committed) == len(batch.entries):
            cls._cleanup_after_commit(batch)
            return len(batch.entries)
        for entry in batch.entries:
            if entry["offset"] in committed and entry["state"] != "uploaded":
                batch.checkpoint(entry, "uploaded")
        pending = [entry for entry in batch.entries if entry["state"] != "uploaded"]
        batch.validate_files(pending)
        # An upload checkpoint may survive a failed or unacknowledged DB commit.
        ready = [entry for entry in batch.entries if entry["state"] == "uploaded" and entry["offset"] not in committed]
        cls._commit_entries(media, batch, ready, check_connection=check_connection)
        committed.update(entry["offset"] for entry in ready)
        prefix = cls.thumbnail_prefix(media)
        storage = asset_storage()
        failed = 0

        def report():
            if progress_callback:
                progress_callback(f"正在上传缩略图，已有 {len(committed)}/{len(batch.entries)} 张可用，失败 {failed} 张")

        def publish(entry):
            key = batch.key(prefix, entry)
            reconcile = entry["state"] == "uploading"
            batch.checkpoint(entry, "uploading")
            if not reconcile or not cls._remote_matches(storage, key, entry):
                storage.put_file(key, batch.source(entry), overwrite=False, immutable=True)
            batch.checkpoint(entry, "uploaded")

        def completed(entry, _result, error):
            nonlocal failed
            if error is not None:
                if check_connection is not None:
                    check_connection()
                # A broken local checkpoint is not a recoverable network failure.
                if not isinstance(error, (StorageError, StorageConflict)):
                    raise error
                batch.checkpoint(entry, entry["state"], error=error)
                failed += 1
                logger.warning(
                    "Thumbnail publication failed media_id={} key={} stage={} status={} publication_possible={} detail={}",
                    media.id, batch.key(prefix, entry), getattr(error, "stage", None),
                    getattr(error, "status_code", None), getattr(error, "publication_possible", False), error,
                )
            else:
                cls._commit_entries(media, batch, [entry], check_connection=check_connection)
                committed.add(entry["offset"])
            report()

        report()
        if pending and batch.packed:
            if settings.storage.backend != "local" or not isinstance(storage, LocalStorageBackend):
                raise RuntimeError("image_pack_requires_local_storage")
            pack = batch.workspace / "thumbnails.zip"
            if not pack.is_file():
                temporary = pack.with_suffix(".zip.tmp")
                try:
                    write_pack(temporary, [(f"{entry['offset']}.webp", batch.source(entry)) for entry in batch.entries])
                    os.replace(temporary, pack)
                finally:
                    temporary.unlink(missing_ok=True)
            storage.put_file(f"{prefix}/{batch.manifest['generation']}.zip", pack, overwrite=False, immutable=True)
            for entry in pending:
                batch.checkpoint(entry, "uploaded")
            cls._commit_entries(media, batch, pending, check_connection=check_connection)
            committed.update(entry["offset"] for entry in pending)
            pending = []
            report()
        if pending:
            result = publish_batch(
                pending, publish,
                max_workers=min(settings.storage.webdav_publication_max_workers, len(pending)),
                thread_name_prefix="thumbnail-publication",
                stop_on_error=False, on_complete=completed,
            )
            try:
                result.raise_for_errors()
            except Exception as exc:
                raise ThumbnailPublicationIncomplete(
                    len(committed), len(result.errors),
                    publication_possible=any(getattr(error, "publication_possible", False) for _, error in result.errors),
                ) from exc
        if check_connection is not None:
            check_connection()
        # Each successful file is already committed; only a complete set can
        # discard the durable batch used to resume the remaining files.
        available = len(cls._committed_offsets(media, batch))
        if available != len(batch.entries):
            raise ThumbnailPublicationIncomplete(available, len(batch.entries) - available)
        cls._cleanup_after_commit(batch)
        return len(batch.entries)

    @staticmethod
    def _cleanup_after_commit(batch: ThumbnailBatch) -> None:
        # An inner atomic() is only a savepoint if a caller owns a transaction.
        if not get_database().in_transaction():
            batch.cleanup()

    @staticmethod
    def read_dimensions(image_origin: str) -> tuple[int | None, int | None]:
        if is_nonlocal_image_reference(image_origin):
            raise ValueError("thumbnail_image_reference_nonlocal")
        with PILImage.open(BytesIO(read_image_bytes(image_origin, storage=asset_storage()))) as image:
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
