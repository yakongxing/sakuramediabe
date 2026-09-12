"""视频条目（VideoItem）service：非 JAV 视频的条目增删改查与详情组装。"""


from peewee import JOIN, Case, fn

from src.api.exception.errors import ApiError
from src.common.media_formats import normalize_media_resolution
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    require_by_id,
    resolve_sort_expression,
    validate_page,
)
from src.model import (
    Image,
    Media,
    MediaThumbnail,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
    get_database,
)
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.videos.items import (
    VideoCollectionRef,
    VideoItemCreateRequest,
    VideoItemDetailResource,
    VideoItemListItemResource,
    VideoItemUpdateRequest,
)
from src.service.media_detail_read_service import MediaDetailReadService


class VideoItemService:
    @staticmethod
    def _require_video(video_id: int) -> VideoItem:
        return require_by_id(VideoItem, video_id, "video_item", error_message="Video item not found")

    @staticmethod
    def _require_video_detail(video_id: int) -> VideoItem:
        query = VideoItem.select(VideoItem, Image).join(
            Image,
            JOIN.LEFT_OUTER,
            on=(VideoItem.cover_image == Image.id),
        )
        return require_by_id(
            VideoItem,
            video_id,
            "video_item",
            error_message="Video item not found",
            query=query,
        )

    @staticmethod
    def _parse_resolution(value: str | None) -> tuple[int | None, int | None]:
        """拆 Media.resolution 字符串（形如 "1920x1080"）为 (width, height)。

        空 / 缺 'x' / 非整数 / 非正值 一律返 (None, None)，由调用方决定回退策略（前端
        瀑布流回退 16:9）。MediaMetadataProbeService 探测失败时本就不写该字段。
        """
        normalized = normalize_media_resolution(value)
        if normalized is None:
            return (None, None)
        width, height = (int(part) for part in normalized.split("x"))
        return (width, height)

    @staticmethod
    def _first_media_alias():
        """构造视频媒体统计及「第一条媒体」的分组子查询与 Media 别名。

        返回 (first_media 别名, first_media_id 子查询)，两者配合 LEFT JOIN 后，
        first_media 即每个条目按 Media.id 升序的第一条媒体；同一次分组扫描还计算媒体数量
        和有效媒体数量，避免列表页再执行一次聚合查询。
        """
        valid_count = fn.SUM(Case(None, [(Media.valid == True, 1)], 0))
        first_media_id = Media.select(
            Media.video_item.alias("owner_id"),
            fn.MIN(Media.id).alias("first_media_id"),
            fn.COUNT(Media.id).alias("media_count"),
            valid_count.alias("valid_count"),
        ).where(Media.video_item.is_null(False)).group_by(Media.video_item)
        return Media.alias(), first_media_id

    @classmethod
    def _build_video_order(
        cls,
        sort: str | None,
        *,
        first_media,
        default_sort: str,
        extra_columns: dict | None = None,
        tie_breaker=None,
    ):
        """解析 `field:direction` 排序，返回 [主排序, 次序稳定项]。

        - created_at / title 取 VideoItem 自身列；
        - duration / file_size 取第一条媒体的时长 / 文件大小，无媒体时按 0 参与排序
          （COALESCE 兜底，保证空媒体排序稳定）；
        - extra_columns 注入域特有字段（合集的 position）。
        """
        columns = {
            "created_at": VideoItem.created_at,
            "title": VideoItem.title,
            "duration": fn.COALESCE(first_media.duration_seconds, 0),
            "file_size": fn.COALESCE(first_media.file_size_bytes, 0),
        }
        if extra_columns:
            columns.update(extra_columns)
        # 空白 sort 归一化为默认值（旧实现 ``(sort or default_sort).strip()`` 语义）。
        normalized_sort = (sort or "").strip() or default_sort
        breaker = VideoItem.id if tie_breaker is None else tie_breaker
        return resolve_sort_expression(
            normalized_sort,
            columns,
            error_code="invalid_video_filter",
            tie_breaker=breaker,
        )

    @classmethod
    def _filtered_query(
        cls,
        *,
        query: str | None = None,
    ):
        video_query = VideoItem.select(VideoItem)
        if query is not None:
            normalized = query.strip()
            if not normalized:
                raise ApiError(422, "invalid_video_filter", "Invalid video filter", {"query": query})
            video_query = video_query.where(VideoItem.title.contains(normalized))
        return video_query

    @staticmethod
    def _collections_map(video_ids: list[int]) -> dict[int, list[VideoCollectionRef]]:
        """批量拉每个视频归属的合集列表，按合集名称升序返回 {video_item_id: [Ref, ...]}。

        用于列表/详情共用的合集引用填充，避免逐条懒加载导致 N+1。
        """
        if not video_ids:
            return {}
        rows = (
            VideoCollectionItem.select(
                VideoCollectionItem.video_item,
                VideoCollection.id,
                VideoCollection.name,
            )
            .join(VideoCollection, on=(VideoCollectionItem.collection == VideoCollection.id))
            .where(VideoCollectionItem.video_item.in_(video_ids))
            .order_by(VideoCollection.name.asc(), VideoCollection.id.asc())
        )
        result: dict[int, list[VideoCollectionRef]] = {}
        for row in rows:
            result.setdefault(row.video_item_id, []).append(
                VideoCollectionRef(id=row.collection.id, name=row.collection.name)
            )
        return result

    @classmethod
    def _to_list_item(
        cls,
        video: VideoItem,
        media_count: int,
        can_play: bool,
        *,
        duration_seconds: int = 0,
        file_size_bytes: int = 0,
        cover_width: int | None = None,
        cover_height: int | None = None,
        collections: list[VideoCollectionRef] | None = None,
    ) -> VideoItemListItemResource:
        return VideoItemListItemResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            duration_seconds=duration_seconds,
            file_size_bytes=file_size_bytes,
            cover_width=cover_width,
            cover_height=cover_height,
            media_count=media_count,
            can_play=can_play,
            collections=collections or [],
            created_at=video.created_at,
            updated_at=video.updated_at,
        )

    @classmethod
    def list_videos(
        cls,
        *,
        query: str | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[VideoItemListItemResource]:
        validate_page(page, page_size, error_code="invalid_video_filter")
        base_query = cls._filtered_query(query=query)
        total = base_query.count()
        first_media, first_media_id = cls._first_media_alias()
        order_by = cls._build_video_order(
            sort, first_media=first_media, default_sort="created_at:desc"
        )
        # 一次 LEFT JOIN 预加载封面与「第一条媒体」的时长/大小：既供排序也供展示，
        # 避免 _to_list_item 逐行懒加载 cover_image（N+1）与额外的聚合查询。
        videos = list(
            base_query.select_extend(
                Image,
                fn.COALESCE(first_media.duration_seconds, 0).alias("first_duration_seconds"),
                fn.COALESCE(first_media.file_size_bytes, 0).alias("first_file_size_bytes"),
                fn.COALESCE(first_media.resolution, "").alias("first_resolution"),
                fn.COALESCE(first_media_id.c.media_count, 0).alias("media_count"),
                fn.COALESCE(first_media_id.c.valid_count, 0).alias("valid_count"),
            )
            .join(Image, JOIN.LEFT_OUTER, on=(VideoItem.cover_image == Image.id))
            .switch(VideoItem)
            .join(first_media_id, JOIN.LEFT_OUTER, on=(first_media_id.c.owner_id == VideoItem.id))
            .join(first_media, JOIN.LEFT_OUTER, on=(first_media.id == first_media_id.c.first_media_id))
            .order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        video_ids = [video.id for video in videos]
        collections_map = cls._collections_map(video_ids)
        items = []
        for video in videos:
            cover_width, cover_height = cls._parse_resolution(video.first_resolution)
            items.append(
                cls._to_list_item(
                    video,
                    video.media_count,
                    bool(video.valid_count),
                    duration_seconds=video.first_duration_seconds,
                    file_size_bytes=video.first_file_size_bytes,
                    cover_width=cover_width,
                    cover_height=cover_height,
                    collections=collections_map.get(video.id, []),
                )
            )
        return PageResponse[VideoItemListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_video_detail(cls, video_id: int) -> VideoItemDetailResource:
        video = cls._require_video_detail(video_id)
        media_batch = MediaDetailReadService.for_video(video)
        media_items = media_batch.resources
        stats_media_count = len(media_items)
        can_play = any(media.valid for media in media_items)
        # 时长/大小取第一条媒体（media_items 已按 Media.id 升序），无媒体时为 0。
        first_media = media_items[0] if media_items else None
        cover_width, cover_height = cls._parse_resolution(
            first_media.resolution if first_media else None
        )
        collections = cls._collections_map([video.id]).get(video.id, [])
        return VideoItemDetailResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            duration_seconds=first_media.duration_seconds if first_media else 0,
            file_size_bytes=first_media.file_size_bytes if first_media else 0,
            cover_width=cover_width,
            cover_height=cover_height,
            media_count=stats_media_count,
            can_play=can_play,
            collections=collections,
            created_at=video.created_at,
            updated_at=video.updated_at,
            media_items=media_items,
        )

    @classmethod
    def create_video(cls, payload: VideoItemCreateRequest) -> VideoItemDetailResource:
        video = VideoItem.create(
            title=payload.title,
            summary=payload.summary,
            release_date=payload.release_date,
        )
        return cls.get_video_detail(video.id)

    @classmethod
    def update_video(cls, video_id: int, payload: VideoItemUpdateRequest) -> VideoItemDetailResource:
        video = cls._require_video(video_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")
        obsolete_cover_image = None
        if "cover_thumbnail_id" in update_data:
            thumbnail_id = update_data["cover_thumbnail_id"]
            if thumbnail_id is None:
                raise ApiError(
                    422,
                    "video_cover_thumbnail_required",
                    "cover_thumbnail_id cannot be null",
                )
            thumbnail = (
                MediaThumbnail.select(MediaThumbnail, Media)
                .join(Media)
                .where(
                    MediaThumbnail.id == thumbnail_id,
                    Media.video_item == video,
                )
                .get_or_none()
            )
            if thumbnail is None:
                raise ApiError(
                    404,
                    "video_cover_thumbnail_not_found",
                    "Video cover thumbnail not found",
                    {"video_id": video.id, "thumbnail_id": thumbnail_id},
                )
            obsolete_cover_image = (
                video.cover_image if video.cover_image_id is not None else None
            )
            video.cover_image = thumbnail.image
        if "title" in update_data and update_data["title"] is not None:
            video.title = update_data["title"]
        if "summary" in update_data and update_data["summary"] is not None:
            video.summary = update_data["summary"]
        if "release_date" in update_data:
            video.release_date = update_data["release_date"]
        video.updated_at = utc_now_for_db()
        video.save()
        if obsolete_cover_image is not None:
            from src.service.catalog.image_cleanup_service import ImageCleanupService

            obsolete_image_paths = ImageCleanupService.delete_image_record_if_unused(
                obsolete_cover_image
            )
            ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)
        return cls.get_video_detail(video.id)

    @classmethod
    def delete_video(cls, video_id: int) -> None:
        # 延迟导入避免与 media_service / image_cleanup_service 顶层依赖链形成循环。
        from src.service.catalog.image_cleanup_service import ImageCleanupService
        from src.service.playback.media_service import MediaService

        video = cls._require_video(video_id)
        # 复用媒体删除链路：清理文件、缩略图图片、向量与级联子表，而非简单置空可空外键。
        media_ids = [media.id for media in Media.select(Media.id).where(Media.video_item == video)]
        for media_id in media_ids:
            MediaService.delete_media(media_id)
        # 封面 Image 为该视频独有（generate_cover 新建），随视频一并清理图片行与磁盘文件，避免孤儿。
        cover_image = video.cover_image if video.cover_image_id is not None else None
        obsolete_image_paths: set[str] = set()
        with get_database().atomic():
            # 剩余的合集关联均为非空外键，recursive 删除即可清理。
            video.delete_instance(recursive=True)
            if cover_image is not None:
                obsolete_image_paths |= ImageCleanupService.delete_image_record_if_unused(cover_image)
        ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)
