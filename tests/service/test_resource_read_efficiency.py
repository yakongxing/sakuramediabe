from types import SimpleNamespace

from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    VideoItem,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.catalog.movie_service import MovieService
from src.service.videos.video_item_service import VideoItemService


def _count_queries(monkeypatch, database):
    queries: list[str] = []
    execute_sql = database.execute_sql

    def tracked_execute_sql(sql, *args, **kwargs):
        queries.append(sql)
        return execute_sql(sql, *args, **kwargs)

    monkeypatch.setattr(database, "execute_sql", tracked_execute_sql)
    return queries


def _image(name: str) -> Image:
    return Image.create(
        origin=f"https://images.example/{name}",
        small=f"https://images.example/{name}",
        medium=f"https://images.example/{name}",
        large=f"https://images.example/{name}",
    )


def _add_media_detail(owner, library: MediaLibrary, suffix: str) -> Media:
    owner_field = {"movie": owner} if isinstance(owner, Movie) else {"video_item": owner}
    media = Media.create(
        **owner_field,
        library=library,
        file_name=f"{suffix}.mp4",
        resolution="1920x1080",
    )
    MediaProgress.create(media=media, position_seconds=12)
    thumbnail = MediaThumbnail.create(media=media, image=_image(f"{suffix}.webp"), offset=12)
    MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=12)
    return media


def test_movie_detail_has_fixed_query_budget(test_db, monkeypatch):
    library = MediaLibrary.create(name="movie-read", provider_key="demo", provider_config={})
    movie = Movie.create(
        movie_number="READ-001",
        javdb_id="read-001",
        title="read",
        cover_image=_image("movie-cover.webp"),
    )
    _add_media_detail(movie, library, "movie-1")
    _add_media_detail(movie, library, "movie-2")
    provider_calls: list[str] = []

    def provider(provider_key):
        provider_calls.append(provider_key)
        return SimpleNamespace(playback_deliveries=("proxy",), merged_playback_format="mp4")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", provider)
    queries = _count_queries(monkeypatch, test_db)

    detail = MovieService.get_movie_detail(movie.movie_number)

    assert len(queries) <= 7
    assert len(detail.media_items) == 2
    assert detail.media_items[0].progress.last_position_seconds == 12
    assert len(detail.media_items[0].points) == 1
    assert detail.merge_playback_candidates[0].segment_count == 2
    assert provider_calls == ["demo"]


def test_video_detail_has_fixed_query_budget(test_db, monkeypatch):
    library = MediaLibrary.create(name="video-read", provider_key="demo", provider_config={})
    video = VideoItem.create(title="read", cover_image=_image("video-cover.webp"))
    _add_media_detail(video, library, "video-1")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )
    queries = _count_queries(monkeypatch, test_db)

    detail = VideoItemService.get_video_detail(video.id)

    assert len(queries) <= 4
    assert detail.cover_image is not None
    assert detail.media_items[0].progress.last_position_seconds == 12
    assert len(detail.media_items[0].points) == 1


def test_movie_list_has_fixed_query_budget(test_db, monkeypatch):
    movie = Movie.create(movie_number="LIST-001", javdb_id="list-001", title="movie")
    queries = _count_queries(monkeypatch, test_db)
    movies = MovieService.list_movies(page=1, page_size=20)

    assert len(queries) <= 2
    assert movies.items[0].movie_number == movie.movie_number


def test_video_list_has_fixed_query_budget(test_db, monkeypatch):
    video = VideoItem.create(title="video")
    library = MediaLibrary.create(name="list-read", provider_key="demo", provider_config={})
    Media.create(video_item=video, library=library, file_name="video.mp4")
    queries = _count_queries(monkeypatch, test_db)

    videos = VideoItemService.list_videos(page=1, page_size=20)

    assert len(queries) <= 3
    assert videos.items[0].id == video.id
    assert videos.items[0].media_count == 1
    assert videos.items[0].can_play is True
