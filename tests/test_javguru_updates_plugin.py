"""频道解析、增量边界、重试与宿主集成；网络请求使用固定 HTML。"""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import httpx
import portalocker
import pytest

from plugin_packages.javguru_updates import plugin
from src.model import Media, MediaLibrary, Movie, Playlist, PlaylistMovie
from src.plugins import PluginContext
from src.plugins.installer import safe_extract_zip
from src.plugins.loader import check_plugin_dir
from src.plugins.manifest import load_manifest_from_file

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
SOURCE = Path(__file__).resolve().parents[1] / "plugin_packages" / "javguru_updates"


class Reporter:
    def __init__(self):
        self.summary = {}

    def emit(self, **payload):
        self.summary.update(payload.get("summary_patch") or {})


def page(*messages, before=None):
    older = f'<link rel="prev" href="/s/javguru_updates?before={before}">' if before else ""
    body = "".join(
        f'<div class="tgme_widget_message" data-post="javguru_updates/{post_id}">'
        f'<div class="tgme_widget_message_text">{text}</div>'
        f'<div class="footer"><time datetime="{date.isoformat()}"></time></div></div>'
        for post_id, date, text in messages
    )
    return f'<html><head>{older}</head><body><section class="tgme_channel_history">{body}</section></body></html>'


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, params=None):
        self.calls.append(params)
        result = self.pages[params["before"] if params else None]
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, text=result, request=httpx.Request("GET", url))


@pytest.fixture()
def context(tmp_path):
    context = PluginContext(plugin.PLUGIN_ID, {}, tmp_path)
    plugin.save_state(tmp_path / "state.json", plugin.SyncState(initial_since=NOW - timedelta(days=7)))
    return context


def test_extract_all_numbers_without_dates_or_page_ids():
    assert plugin.extract_numbers(
        "SEPTEMBER 2026 1080p 2026-10-03 /1051331/ "
        "waaa-682 WAAA_682 FC2-PPV-1234567 fc2ppv_1234567 "
        "123456-789 123456_789 XXX-AV-12345 MKBD-S123"
    ) == ["WAAA-682", "FC2-1234567", "123456-789", "123456_789", "XXX-AV-12345", "MKBD-S123"]


def test_recap_text_wins_and_template_links_are_ignored():
    messages, before = plugin.parse_page(page((101, NOW, (
        '<b>RECAP</b><br/><a href="http://jav.guru/NIMA-040">1️⃣</a> '
        '<a href="https://jav.guru/OLD-001"><b>WAAA-682</b></a><br/>'
        '<a href="https://jav.guru/JUR-615"> </a>'
        '<a href="https://jav.guru/1051331/">EBWH-365</a><br/>'
        '<a href="https://jav.guru/SNOS-363">Watch here</a><br/>'
        '<a href="https://other.example/FAKE-001">Other website</a>'
    )), before=82))
    assert messages[0].numbers == ("WAAA-682", "EBWH-365", "SNOS-363")
    assert messages[0].post_id == 101
    assert before == 82


@pytest.mark.parametrize("html", [
    "<html>Too many requests</html>", page(),
    page((1, NOW, "ABC-001")).replace('datetime=', 'missing='),
    page((1, NOW, "ABC-001")).replace("javguru_updates/1", "other/1"),
])
def test_malformed_pages_fail_closed(html):
    with pytest.raises(ValueError):
        plugin.parse_page(html)


def test_initial_seven_day_boundary_and_incremental_pagination():
    cutoff = NOW - timedelta(days=7)
    state = plugin.SyncState(initial_since=cutoff)
    client = Client({
        None: page((30, NOW, "ABC-003"), before=30),
        30: page((28, cutoff - timedelta(seconds=1), "ABC-001"), (29, cutoff, "ABC-002"), before=28),
    })
    messages, cursor = plugin.collect_messages(client, state, Reporter())
    assert [m.post_id for m in messages] == [29, 30]
    assert cursor == 30
    assert client.calls == [None, {"before": 30}]
    # 停机超过 7 天仍按 ID 补抓，而非每天按日期截断。
    state.last_post_id = 28
    state.initial_since = NOW
    messages, cursor = plugin.collect_messages(client, state, Reporter())
    assert [m.post_id for m in messages] == [29, 30]
    state.last_post_id = 30
    assert plugin.collect_messages(client, state, Reporter()) == ([], 30)


def test_old_only_page_still_establishes_cursor():
    state = plugin.SyncState(initial_since=NOW)
    client = Client({None: page((30, NOW - timedelta(days=10), "ABC-003"), before=30)})
    assert plugin.collect_messages(client, state, Reporter()) == ([], 30)


def test_nonadvancing_pagination_fails():
    client = Client({None: page((30, NOW, "ABC-003"), before=31)})
    with pytest.raises(ValueError, match="分页"):
        plugin.collect_messages(client, plugin.SyncState(initial_since=NOW), Reporter())


def fake_context(tmp_path):
    movies = {}
    imported = Mock(side_effect=lambda number: movies[number])
    context = SimpleNamespace(
        data_dir=tmp_path,
        import_movie_by_number=imported,
        collections=SimpleNamespace(
            ensure_playlist_by_name=Mock(return_value=SimpleNamespace(collection_id=1)),
            add_playlist_movies=Mock(),
        ),
        media=SimpleNamespace(presence_for_movies=Mock(
            side_effect=lambda ids: {i: SimpleNamespace(has_playable=False) for i in ids}
        )),
        subscriptions=SimpleNamespace(subscribe=Mock()),
    )
    for i in range(1, 4):
        number = f"ABC-00{i}"
        movies[number] = SimpleNamespace(movie_id=i, values={
            "movie_number": number, "is_blacklisted": False, "is_subscribed": False,
        })
    plugin.save_state(tmp_path / "state.json", plugin.SyncState(initial_since=NOW))
    return context, movies


def test_failed_import_does_not_block_others_and_retries_after_restart(tmp_path, monkeypatch):
    context, movies = fake_context(tmp_path)
    client = Client({None: page((31, NOW, "ABC-001 ABC-002 ABC-001"))})
    monkeypatch.setattr(plugin.httpx, "Client", lambda **_: client)
    context.import_movie_by_number.side_effect = [ValueError("metadata unavailable"), movies["ABC-002"]]
    reporter = Reporter()
    with pytest.raises(RuntimeError, match="待重试番号=1"):
        plugin.run_sync(context, reporter, {})
    state = plugin.load_state(tmp_path / "state.json")
    assert state.last_post_id == 31
    assert set(state.pending) == {"ABC-001"}
    assert "ABC-002" in state.completed
    assert state.pending["ABC-001"].attempts == 1
    assert reporter.summary["failed_items"][0]["stage"] == "import"
    context.import_movie_by_number.reset_mock(side_effect=True)
    context.import_movie_by_number.side_effect = lambda number: movies[number]
    result = plugin.run_sync(context, Reporter(), {})
    assert result["new_messages"] == 0
    assert result["pending_movies"] == 0
    context.import_movie_by_number.assert_called_once_with("ABC-001")
    assert context.subscriptions.subscribe.call_count == 2
    plugin.run_sync(context, Reporter(), {})
    assert context.subscriptions.subscribe.call_count == 2
    assert context.collections.add_playlist_movies.call_count == 2


@pytest.mark.parametrize("stage", ["collection", "subscription"])
def test_side_effect_failures_keep_pending_for_retry(tmp_path, monkeypatch, stage):
    context, _movies = fake_context(tmp_path)
    client = Client({None: page((31, NOW, "ABC-001"))})
    monkeypatch.setattr(plugin.httpx, "Client", lambda **_: client)
    operation = context.collections.add_playlist_movies if stage == "collection" else context.subscriptions.subscribe
    operation.side_effect = RuntimeError("temporary failure")
    reporter = Reporter()
    with pytest.raises(RuntimeError):
        plugin.run_sync(context, reporter, {})
    assert reporter.summary["failed_items"][0]["stage"] == stage
    assert plugin.load_state(tmp_path / "state.json").pending
    if stage == "collection":
        context.subscriptions.subscribe.assert_not_called()
    operation.side_effect = None
    assert plugin.run_sync(context, Reporter(), {})["pending_movies"] == 0


def test_page_failure_preserves_cursor_and_retries_existing_pending(tmp_path, monkeypatch):
    context, _movies = fake_context(tmp_path)
    state = plugin.load_state(tmp_path / "state.json")
    state.last_post_id = 20
    state.pending["ABC-001"] = plugin.PendingMovie(movie_number="ABC-001", post_id=20)
    plugin.save_state(tmp_path / "state.json", state)
    client = Client({None: page((31, NOW, "ABC-002"), before=31), 31: httpx.ReadTimeout("offline")})
    monkeypatch.setattr(plugin.httpx, "Client", lambda **_: client)
    with pytest.raises(RuntimeError, match="offline"):
        plugin.run_sync(context, Reporter(), {})
    state = plugin.load_state(tmp_path / "state.json")
    assert state.last_post_id == 20
    assert not state.pending
    context.import_movie_by_number.assert_called_once_with("ABC-001")
    client.pages[31] = page((20, NOW - timedelta(days=20), "ABC-001"))
    assert plugin.run_sync(context, Reporter(), {})["last_post_id"] == 31


def test_corrupt_state_and_concurrent_execution_do_not_create_effects(tmp_path):
    context, _movies = fake_context(tmp_path)
    with (
        portalocker.Lock(str(tmp_path / "sync.lock"), mode="a", timeout=0),
        pytest.raises(portalocker.exceptions.LockException),
    ):
        plugin.run_sync(context, Reporter(), {})
    (tmp_path / "state.json").write_text("{}")
    with pytest.raises(ValueError):
        plugin.run_sync(context, Reporter(), {})
    context.collections.ensure_playlist_by_name.assert_not_called()


def test_atomic_checkpoint_failure_stops_processing(tmp_path, monkeypatch):
    context, _movies = fake_context(tmp_path)
    client = Client({None: page((31, NOW, "ABC-001 ABC-002"))})
    monkeypatch.setattr(plugin.httpx, "Client", lambda **_: client)
    original = plugin.save_state
    calls = 0

    def failing_save(path, state):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        original(path, state)

    monkeypatch.setattr(plugin, "save_state", failing_save)
    with pytest.raises(OSError, match="disk full"):
        plugin.run_sync(context, Reporter(), {})
    context.import_movie_by_number.assert_called_once_with("ABC-001")
    assert set(plugin.load_state(tmp_path / "state.json").pending) == {"ABC-001", "ABC-002"}


def test_host_integration_collects_and_only_subscribes_missing_media(test_db, context, monkeypatch):
    numbers = [f"ABC-00{i}" for i in range(1, 5)]
    movies = [Movie.create(movie_number=n, javdb_id=n, title=n, is_blacklisted=i == 2)
              for i, n in enumerate(numbers)]
    library = MediaLibrary.create(name="local", provider_key="local", provider_config={})
    Media.create(movie=movies[0], library=library, file_name="ABC-001.mp4", valid=True)
    Media.create(movie=movies[3], library=library, file_name="ABC-004.mp4", valid=False)
    existing = Playlist.create(name="javguru", description="用户创建")
    client = Client({None: page((31, NOW, " ".join(numbers)))})
    monkeypatch.setattr(plugin.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(PluginContext, "import_movie_by_number", lambda self, n: self.movies.find_by_numbers([n])[0])
    result = plugin.run_sync(context, Reporter(), {})
    assert result["collected_movies"] == 4
    assert result["subscribed_movies"] == 2
    assert result["playable_skipped"] == result["blacklisted_skipped"] == 1
    assert [Movie.get_by_id(m.id).is_subscribed for m in movies] == [False, True, False, True]
    assert PlaylistMovie.select().where(PlaylistMovie.playlist == existing.id).count() == 4
    assert Playlist.get_by_id(existing.id).description == "用户创建"
    # 用户之后取消订阅或移出列表，重复运行不会重新添加已完成的番号。
    context.subscriptions.unsubscribe(numbers[1])
    PlaylistMovie.delete().where(PlaylistMovie.movie == movies[1]).execute()
    plugin.run_sync(context, Reporter(), {})
    assert not Movie.get_by_id(movies[1].id).is_subscribed
    assert PlaylistMovie.select().count() == 3


def test_packaged_plugin_loads_and_registers_daily_manual_job(tmp_path):
    archive = tmp_path / "javguru.zip"
    with ZipFile(archive, "w") as output:
        for name in ("__init__.py", "plugin.py", "manifest.json", "README.md"):
            output.write(SOURCE / name, name)
    destination = tmp_path / "plugins" / plugin.PLUGIN_ID
    safe_extract_zip(archive, destination)
    manifest = load_manifest_from_file(destination)
    registration = check_plugin_dir(plugin_dir=destination)
    assert manifest.host_api_version == registration.host_api_version == 10
    job, = registration.jobs
    assert job.task_key == plugin.TASK_KEY
    assert job.default_cron == "0 1 * * *"
    assert job.manual_trigger_allowed
    assert not job.manual_only


def test_plugin_rejects_host_api_9(tmp_path, monkeypatch):
    from src.plugins import contracts
    from src.plugins.loader import PluginLoadError

    destination = tmp_path / plugin.PLUGIN_ID
    shutil.copytree(SOURCE, destination, ignore=shutil.ignore_patterns("__pycache__", "data"))
    monkeypatch.setattr(contracts, "HOST_API_VERSION", 9)
    with pytest.raises(PluginLoadError, match="不兼容"):
        check_plugin_dir(plugin_dir=destination)
