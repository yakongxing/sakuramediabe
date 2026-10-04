"""插件合集的归属校验、成员替换与普通播放列表复用。"""

from collections.abc import Collection

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import find_movie_by_number
from src.model import (
    PLAYLIST_KIND_CUSTOM,
    ClipCollection,
    MomentCollection,
    Playlist,
    PlaylistMovie,
)
from src.model.base import get_database
from src.service.collections.clip_collection_service import (
    ClipCollectionService,
)
from src.service.collections.moment_collection_service import (
    MomentCollectionService,
)


class PluginCollectionService:
    """向插件 facade 提供有归属校验的合集操作和普通列表追加。"""

    @staticmethod
    def _validate_key(plugin_key: str) -> str:
        key = (plugin_key or "").strip()
        if not key:
            raise ValueError("plugin_key 不能为空")
        if len(key) > 128:
            raise ValueError("plugin_key 不能超过 128 个字符")
        return key

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = (name or "").strip()
        if not normalized:
            raise ValueError("合集名称不能为空")
        if len(normalized) > 255:
            raise ValueError("合集名称不能超过 255 个字符")
        return normalized

    @staticmethod
    def _normalize_description(description: str | None) -> str:
        return (description or "").strip()

    @classmethod
    def _ensure_collection(
        cls,
        model,
        *,
        plugin_id: str,
        plugin_key: str,
        name: str,
        description: str | None,
        **defaults,
    ):
        plugin_key = cls._validate_key(plugin_key)
        name = cls._normalize_name(name)
        description = cls._normalize_description(description)
        collection = model.get_or_none(
            (model.owner_plugin_id == plugin_id) & (model.plugin_key == plugin_key)
        )
        if collection is not None:
            changed = collection.name != name or collection.description != description
            if changed:
                collection.name = name
                collection.description = description
                try:
                    collection.save(only=[model.name, model.description])
                except IntegrityError as exc:
                    raise ApiError(
                        409,
                        "plugin_collection_conflict",
                        "插件合集名称已存在",
                        {"plugin_key": plugin_key, "name": name},
                    ) from exc
            return collection

        try:
            return model.create(
                **defaults,
                name=name,
                description=description,
                owner_plugin_id=plugin_id,
                plugin_key=plugin_key,
            )
        except IntegrityError as exc:
            # 名称唯一约束或并发创建冲突都转成插件可理解的稳定错误。
            raise ApiError(
                409,
                "plugin_collection_conflict",
                "插件合集名称或 key 已存在",
                {"plugin_key": plugin_key, "name": name},
            ) from exc

    @staticmethod
    def _require_owned(model, *, plugin_id: str, plugin_key: str):
        plugin_key = PluginCollectionService._validate_key(plugin_key)
        collection = model.get_or_none(
            (model.owner_plugin_id == plugin_id) & (model.plugin_key == plugin_key)
        )
        if collection is None:
            raise ApiError(
                404,
                "plugin_collection_not_found",
                "插件合集不存在",
                {"plugin_key": plugin_key},
            )
        return collection

    @classmethod
    def ensure_playlist(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> Playlist:
        return cls._ensure_collection(
            Playlist,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
            kind=PLAYLIST_KIND_CUSTOM,
        )

    @staticmethod
    def _check_appendable_playlist(plugin_id: str, playlist: Playlist) -> Playlist:
        if playlist.kind != PLAYLIST_KIND_CUSTOM:
            raise ApiError(409, "playlist_managed_by_system", "不能修改系统播放列表")
        if playlist.owner_plugin_id not in (None, plugin_id):
            raise ApiError(
                409, "plugin_collection_conflict", "播放列表归其他插件管理",
                {"playlist_id": playlist.id},
            )
        return playlist

    @classmethod
    def ensure_playlist_by_name(
        cls, plugin_id: str, name: str, description: str | None = None
    ) -> Playlist:
        from src.service.collections.playlist_service import PlaylistService

        name = cls._normalize_name(name)
        PlaylistService._ensure_name_not_reserved(name)
        # get_or_create 使用事务并恢复并发创建的唯一键冲突。
        playlist, _created = Playlist.get_or_create(
            name=name,
            defaults={
                "kind": PLAYLIST_KIND_CUSTOM,
                "description": cls._normalize_description(description),
            },
        )
        return cls._check_appendable_playlist(plugin_id, playlist)

    @classmethod
    def add_playlist_movies(
        cls, plugin_id: str, collection: int | str, movie_numbers: Collection[str]
    ) -> Playlist:
        if type(collection) is int and collection > 0:
            condition = Playlist.id == collection
        elif isinstance(collection, str):
            condition = Playlist.name == cls._normalize_name(collection)
        else:
            raise ValueError("collection 必须是正整数 ID 或非空列表名称")
        if isinstance(movie_numbers, (str, bytes)):
            raise TypeError("movie_numbers 必须是番号集合")

        with get_database().atomic():
            playlist = Playlist.select().where(condition).for_update().first()
            if playlist is None:
                raise ApiError(404, "playlist_not_found", "播放列表不存在")
            cls._check_appendable_playlist(plugin_id, playlist)
            movie_ids = set()
            for raw_number in movie_numbers:
                if not isinstance(raw_number, str) or not raw_number.strip():
                    raise ValueError("movie_number 必须是非空字符串")
                movie = find_movie_by_number(raw_number.strip())
                if movie is None:
                    raise ApiError(
                        404, "movie_not_found", "影片不存在",
                        {"movie_number": raw_number},
                    )
                movie_ids.add(movie.id)
            if movie_ids:
                touched_at = utc_now_for_db()
                inserted = list(
                    PlaylistMovie.insert_many([
                        {"playlist": playlist.id, "movie": movie_id,
                         "created_at": touched_at, "updated_at": touched_at}
                        for movie_id in sorted(movie_ids)
                    ]).on_conflict_ignore().returning(PlaylistMovie.id).execute()
                )
                if inserted:
                    playlist.updated_at = touched_at
                    playlist.save(only=[Playlist.updated_at])
        return playlist

    @classmethod
    def set_playlist_movies(
        cls,
        plugin_id: str,
        plugin_key: str,
        movie_numbers: Collection[str],
    ) -> Playlist:
        playlist = cls._require_owned(Playlist, plugin_id=plugin_id, plugin_key=plugin_key)
        movies = []
        seen_ids: set[int] = set()
        for raw_number in movie_numbers:
            number = (raw_number or "").strip()
            if not number:
                raise ValueError("movie_number 不能为空")
            movie = find_movie_by_number(number)
            if movie is None:
                raise ApiError(
                    404,
                    "movie_not_found",
                    "影片不存在",
                    {"movie_number": number},
                )
            if movie.id not in seen_ids:
                seen_ids.add(movie.id)
                movies.append(movie)

        touched_at = utc_now_for_db()
        with get_database().atomic():
            PlaylistMovie.delete().where(PlaylistMovie.playlist == playlist).execute()
            for movie in movies:
                PlaylistMovie.create(
                    playlist=playlist,
                    movie=movie,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
            playlist.updated_at = touched_at
            playlist.save(only=[Playlist.updated_at])
        return playlist

    @classmethod
    def ensure_moment(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> MomentCollection:
        return cls._ensure_collection(
            MomentCollection,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
        )

    @classmethod
    def set_moment_points(
        cls,
        plugin_id: str,
        plugin_key: str,
        point_ids: Collection[int],
    ) -> MomentCollection:
        collection = cls._require_owned(
            MomentCollection, plugin_id=plugin_id, plugin_key=plugin_key
        )
        MomentCollectionService.set_points(collection.id, list(point_ids))
        return collection

    @classmethod
    def ensure_clip(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> ClipCollection:
        return cls._ensure_collection(
            ClipCollection,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
        )

    @classmethod
    def set_clip_clips(
        cls,
        plugin_id: str,
        plugin_key: str,
        clip_ids: Collection[int],
    ) -> ClipCollection:
        collection = cls._require_owned(
            ClipCollection, plugin_id=plugin_id, plugin_key=plugin_key
        )
        ClipCollectionService.set_clips(collection.id, list(clip_ids))
        return collection
