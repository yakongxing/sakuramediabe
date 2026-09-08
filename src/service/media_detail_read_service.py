"""Shared read-side assembly for media embedded in movie/video details."""

from dataclasses import dataclass
from typing import Any

from peewee import JOIN

from src.common import build_signed_media_url
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
)


@dataclass(frozen=True)
class MediaDetailBatch:
    """Media rows plus their API projection and request-local provider cache."""

    media: list[Media]
    resources: list[MovieMediaResource]
    provider_bundles: dict[str, Any]


class MediaDetailReadService:
    """Load detail media in a fixed number of database round trips."""

    @classmethod
    def for_movie(cls, movie) -> MediaDetailBatch:
        return cls._load(Media.movie == movie)

    @classmethod
    def for_video(cls, video) -> MediaDetailBatch:
        return cls._load(Media.video_item == video)

    @staticmethod
    def _point_resources(media_ids: list[int]) -> dict[int, list[MovieMediaPointResource]]:
        points_by_media_id: dict[int, list[MovieMediaPointResource]] = {}
        if not media_ids:
            return points_by_media_id

        point_query = (
            MediaPoint.select(MediaPoint, MediaThumbnail, Image)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
            .where(MediaPoint.media.in_(media_ids))
            .order_by(MediaPoint.media, MediaPoint.id)
        )
        for point in point_query:
            points_by_media_id.setdefault(point.media_id, []).append(
                MovieMediaPointResource(
                    point_id=point.id,
                    thumbnail_id=point.thumbnail_id,
                    offset_seconds=point.offset_seconds,
                    image=ImageResource.from_attributes_model(point.thumbnail.image),
                )
            )
        return points_by_media_id

    @classmethod
    def _load(cls, owner_condition) -> MediaDetailBatch:
        # MediaProgress is one-to-one (unique media index), so this join cannot
        # multiply media rows. Points remain a separate batched query because
        # they are one-to-many.
        media_items = list(
            Media.select(
                Media,
                MediaLibrary,
                MediaProgress,
            )
            .join(MediaLibrary, JOIN.LEFT_OUTER)
            .switch(Media)
            .join(
                MediaProgress,
                JOIN.LEFT_OUTER,
                on=(MediaProgress.media == Media.id),
                attr="progress_record",
            )
            .where(owner_condition)
            .order_by(Media.id)
        )
        media_ids = [media.id for media in media_items]
        points_by_media_id = cls._point_resources(media_ids)

        provider_bundles: dict[str, Any] = {}
        resources: list[MovieMediaResource] = []
        for media in media_items:
            progress = getattr(media, "progress_record", None)
            media.progress = (
                None
                if progress is None
                else MovieMediaProgressResource(
                    last_position_seconds=progress.position_seconds,
                    last_watched_at=progress.last_watched_at,
                )
            )
            media.points = points_by_media_id.get(media.id, [])

            provider_key = media.library.provider_key
            bundle = provider_bundles.get(provider_key)
            if bundle is None:
                bundle = MEDIA_PROVIDER_REGISTRY.require(provider_key)
                provider_bundles[provider_key] = bundle
            media.play_url = build_signed_media_url(
                media.id, delivery=bundle.playback_deliveries[0]
            )
            media.provider_key = provider_key
            media.playback_deliveries = list(bundle.playback_deliveries)
            resources.append(MovieMediaResource.from_attributes_model(media))

        return MediaDetailBatch(
            media=media_items,
            resources=resources,
            provider_bundles=provider_bundles,
        )
