"""插件按名称复用列表，并只追加成员。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest

from src.api.exception.errors import ApiError
from src.model import Movie, Playlist, PlaylistMovie
from src.plugins import PluginContext


def test_reuse_and_append_preserve_user_data(test_db, tmp_path):
    api = PluginContext("javguru_updates", {}, tmp_path).collections
    playlist = Playlist.create(name="javguru", description="我的收藏")
    first = Movie.create(movie_number="ABC-001", javdb_id="first", title="first")
    second = Movie.create(movie_number="ABC-002", javdb_id="second", title="second")
    timestamp = datetime(2020, 1, 1)
    member = PlaylistMovie.create(
        playlist=playlist, movie=first, created_at=timestamp, updated_at=timestamp
    )

    reused = api.ensure_playlist_by_name(" javguru ", "不覆盖")
    assert reused.collection_id == playlist.id
    assert reused.description == "我的收藏"
    assert reused.key == ""
    assert reused.member_count == 1
    assert api.add_playlist_movies("javguru", ["abc_001", "ABC-002", "ABC-002"]).member_count == 2
    assert api.add_playlist_movies(playlist.id, ["ABC-001"]).member_count == 2
    original = PlaylistMovie.get_by_id(member.id)
    assert original.created_at == original.updated_at == timestamp
    assert {item.movie_id for item in PlaylistMovie.select()} == {first.id, second.id}
    assert Playlist.get_by_id(playlist.id).owner_plugin_id is None


def test_create_plain_playlist_and_reuse_own_plugin_playlist(test_db, tmp_path):
    api = PluginContext("javguru_updates", {}, tmp_path).collections
    created = api.ensure_playlist_by_name("javguru", "频道收藏")
    model = Playlist.get_by_id(created.collection_id)
    assert model.kind == "custom"
    assert model.owner_plugin_id is None
    assert model.plugin_key is None
    assert created.description == "频道收藏"
    owned = api.ensure_playlist("own", "插件列表", "保留")
    reused = api.ensure_playlist_by_name("插件列表", "不覆盖")
    assert reused.collection_id == owned.collection_id
    assert reused.key == "own"
    assert reused.description == "保留"


@pytest.mark.parametrize("kind,owner", [("recently_played", None), ("custom", "other_plugin")])
def test_reject_system_and_other_plugin_lists(test_db, tmp_path, kind, owner):
    api = PluginContext("javguru_updates", {}, tmp_path).collections
    playlist = Playlist.create(name="javguru", kind=kind, owner_plugin_id=owner)
    with pytest.raises(ApiError) as caught:
        api.ensure_playlist_by_name("javguru")
    assert caught.value.status_code == 409
    with pytest.raises(ApiError) as caught:
        api.add_playlist_movies(playlist.id, [])
    assert caught.value.status_code == 409


def test_append_is_atomic_and_validates_inputs(test_db, tmp_path):
    api = PluginContext("javguru_updates", {}, tmp_path).collections
    playlist = api.ensure_playlist_by_name("javguru")
    Movie.create(movie_number="ABC-001", javdb_id="first", title="first")
    with pytest.raises(ApiError) as caught:
        api.add_playlist_movies(playlist.collection_id, ["ABC-001", "MISSING-001"])
    assert caught.value.code == "movie_not_found"
    assert PlaylistMovie.select().count() == 0
    with pytest.raises(ApiError) as caught:
        api.add_playlist_movies("不存在", [])
    assert caught.value.code == "playlist_not_found"
    for invalid in [True, 0, -1, None, " "]:
        with pytest.raises(ValueError):
            api.add_playlist_movies(invalid, [])
    with pytest.raises(TypeError):
        api.add_playlist_movies("javguru", "ABC-001")
    with pytest.raises(ValueError):
        api.add_playlist_movies("javguru", [""])
    with pytest.raises(ApiError):
        api.ensure_playlist_by_name("最近播放")


def test_concurrent_create_and_append_are_idempotent(test_db, tmp_path):
    api = PluginContext("javguru_updates", {}, tmp_path).collections
    Movie.create(movie_number="ABC-001", javdb_id="first", title="first")
    barrier = Barrier(4)

    def append():
        try:
            barrier.wait(timeout=10)
            playlist = api.ensure_playlist_by_name("javguru")
            return api.add_playlist_movies(playlist.collection_id, ["ABC-001"]).collection_id
        finally:
            if not test_db.is_closed():
                test_db.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: append(), range(4)))
    assert len(set(ids)) == 1
    assert Playlist.select().count() == PlaylistMovie.select().count() == 1
