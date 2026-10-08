"""全分页、榜单隔离、异常保护与可安装插件的宿主集成。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from plugin_packages.supjav_rankings import plugin
from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers.deps import db_deps, get_current_user
from src.api.routers.discovery.ranking_sources import router as ranking_router
from src.config.config import Plugins
from src.model import Movie, RankingItem
from src.plugins import PluginContext
from src.plugins.installer import safe_extract_zip
from src.plugins.loader import check_plugin_dir, load_plugin_settings_model
from src.scheduler.ranking_plugin_adapter import apply_plugin_ranking_sources
from src.service.discovery.ranking_service import RANKING_SOURCE_OWNERS, RANKING_SOURCES

SOURCE = Path(__file__).resolve().parents[1] / "plugin_packages" / plugin.PLUGIN_ID
HTTPX_CLIENT = httpx.Client


def page(*titles, links="", start=1):
    posts = "".join(
        f'<div class="post"><a class="img" href="/{start + i}.html" title="{title}">'
        f'<img alt="WRONG-999"/></a><h3>{title}</h3></div>'
        for i, title in enumerate(titles)
    )
    return f'<main><div class="posts">{posts}</div><ul class="pagination">{links}</ul></main>'


def link(number, href=None):
    return f'<li><a href="{href or f"/popular/page/{number}/"}">{number}</a></li>'


class Reporter:
    def __init__(self):
        self.summary = {}

    def emit(self, **payload):
        self.summary.update(payload.get("summary_patch") or {})


def mock_client(monkeypatch, handler):
    factory = Mock(side_effect=lambda **kwargs: HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(plugin.httpx, "Client", factory)
    monkeypatch.setattr(plugin.time, "sleep", lambda _: None)
    return factory


@pytest.mark.parametrize("title,number", [
    ("[Uncensored] WAAA-682 Title", "WAAA-682"),
    ("abc_123", "ABC-123"), ("ABP123 Title", "ABP-123"),
    ("ABP 123 Title", "ABP-123"), ("300MIUM-123", "300MIUM-123"),
    ("FC2-PPV-1234567", "FC2-1234567"), ("FC2PPV_1234567", "FC2-1234567"),
    ("123456_789", "123456_789"), ("123456-789", "123456-789"),
    ("XXX-AV-12345", "XXX-AV-12345"), ("MKBD-S123", "MKBD-S123"),
    ("2026-10-07 1080p 123456.html Amateur", None),
])
def test_extract_title_number(title, number):
    assert plugin.extract_number(title) == number


def test_parse_titles_in_order_and_exclude_recommendations():
    html = page("abc_123 &amp; Title", "FC2-PPV-1234567", "Amateur without a number")
    html += '<aside>' + page("WRONG-001") + '</aside>'
    html += '<div id="sidebar">' + page("WRONG-002") + '</div>'
    parsed = plugin.parse_page(html, plugin.BASE_URL, "day")
    assert parsed.numbers == ("ABC-123", "FC2-1234567")
    assert parsed.post_urls == ("/1.html", "/2.html", "/3.html")
    assert plugin.parse_page(page("ABC-123").replace('title="ABC-123"', ""), plugin.BASE_URL, "day").numbers == ("ABC-123",)


@pytest.mark.parametrize("board", ["day", "week", "month"])
@pytest.mark.parametrize("pagination", ["path", "paged", "page"])
def test_fetches_every_page_including_ellipsis_and_preserves_sort(monkeypatch, board, pagination):
    calls = []

    def handler(request):
        query = parse_qs(request.url.query.decode())
        path_number = request.url.path.strip("/").split("/")[-1]
        current = int(query.get(pagination, [path_number if path_number.isdecimal() else "1"])[0])
        calls.append((current, query.get("sort")))
        href = "/popular/page/4/" if pagination == "path" else f"/popular?{pagination}=4"
        # 第 2 页没有番号，仍须继续抓取后续页；跨页重复番号保留首个位置。
        titles = {1: ("ABC-001",), 2: ("Amateur",), 3: ("ABC-001", "ABC-003"), 4: ("ABC-004",)}
        return httpx.Response(200, text=page(*titles[current], links=link(4, href), start=current * 10))

    mock_client(monkeypatch, handler)
    assert plugin.fetch_numbers(board, plugin.Settings()) == ["ABC-001", "ABC-003", "ABC-004"]
    assert calls == [(i, None if board == "day" else [board]) for i in range(1, 5)]


def test_discovers_more_pages_from_next_link(monkeypatch):
    calls = []

    def handler(request):
        number = int(request.url.path.strip("/").split("/")[-1]) if "/page/" in request.url.path else 1
        calls.append(number)
        next_link = f'<li class="next-page"><a href="/popular/page/{number + 1}/">Next</a></li>' if number < 3 else ""
        return httpx.Response(200, text=page(f"ABC-00{number}", links=next_link, start=number))

    mock_client(monkeypatch, handler)
    assert plugin.fetch_numbers("week", plugin.Settings()) == ["ABC-001", "ABC-002", "ABC-003"]
    assert calls == [1, 2, 3]


@pytest.mark.parametrize("href", [
    "https://other.example/popular/page/2/", "/category/censored-jav/page/2/",
    "/popular/page/2/?sort=month", "/popular?paged=oops", "/popular/page/1001/",
    "/popular/page/2/?paged=3", "/popular?sort=week&sort=month",
])
def test_rejects_invalid_pagination(href):
    with pytest.raises(ValueError, match="Supjav"):
        plugin.parse_page(page("ABC-001", links=link(2, href)), plugin.BASE_URL + "?sort=week", "week")


def test_rejects_nonadvancing_next_page():
    html = page("ABC-001", links='<li class="next-page"><a href="/popular">Next</a></li>')
    with pytest.raises(ValueError, match="没有向后推进"):
        plugin.parse_page(html, plugin.BASE_URL, "day")


@pytest.mark.parametrize("html", [
    "<html>Just a moment...</html>", page(),
    '<div class="post"><a class="img" href="/1.html"></a></div>',
    page("ABC-001").replace('href="/1.html"', 'href="https://ads.example/video"'),
])
def test_invalid_pages_fail_closed(html):
    with pytest.raises(ValueError):
        plugin.parse_page(html, plugin.BASE_URL, "day")


def test_duplicate_page_content_and_no_numbers_do_not_return_partial_board(monkeypatch):
    mock_client(monkeypatch, lambda _: httpx.Response(200, text=page("ABC-001", links=link(2))))
    with pytest.raises(ValueError, match="重复内容"):
        plugin.fetch_numbers("day", plugin.Settings())
    mock_client(monkeypatch, lambda _: httpx.Response(200, text=page("Amateur")))
    with pytest.raises(ValueError, match="未提取到番号"):
        plugin.fetch_numbers("day", plugin.Settings())


def test_headers_timeout_retry_and_403_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(503 if len(calls) == 2 else 200, text=page("ABC-001"))

    factory = mock_client(monkeypatch, handler)
    config = plugin.Settings(user_agent="test-browser", cookie="cf_clearance=secret", timeout_seconds=9)
    assert plugin.fetch_numbers("month", config) == ["ABC-001"]
    assert len(calls) == 3
    assert calls[0].headers["user-agent"] == "test-browser"
    assert calls[0].headers["cookie"] == "cf_clearance=secret"
    assert factory.call_args.kwargs["timeout"] == 9
    calls.clear()
    mock_client(monkeypatch, lambda request: (calls.append(request) or httpx.Response(403)))
    with pytest.raises(RuntimeError, match="HTTP 403") as error:
        plugin.fetch_numbers("day", config)
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_redirect_preserves_board_and_rejects_other_hosts(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/popular":
            return httpx.Response(301, headers={"Location": "/popular/"})
        return httpx.Response(200, text=page("ABC-001"))

    mock_client(monkeypatch, handler)
    assert plugin.fetch_numbers("week", plugin.Settings()) == ["ABC-001"]
    assert calls == [plugin.BASE_URL + "?sort=week", plugin.BASE_URL + "/?sort=week"]
    mock_client(monkeypatch, lambda _: httpx.Response(302, headers={"Location": "https://other.example/"}))
    with pytest.raises(ValueError, match="其他站点"):
        plugin.fetch_numbers("week", plugin.Settings(cookie="secret"))


@pytest.mark.parametrize("config", [{"user_agent": " "}, {"cookie": "value\r\nSecret: 1"}, {"timeout_seconds": 0}])
def test_settings_validation(config):
    with pytest.raises(ValidationError):
        plugin.Settings.model_validate(config)


@pytest.mark.parametrize("value", [
    "", "24:00", "23:60", "-1:00", "1:00", "01:0", "01:00:00",
    "01:00 * * *", "０１:００", None, 100,
])
def test_daily_run_time_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        plugin.Settings(daily_run_time=value)


@pytest.mark.parametrize("config", [
    {"request_mode": "flaresolverr"}, {"request_mode": "unknown"},
    {"flaresolverr_url": "ftp://localhost:8191"},
    {"flaresolverr_url": "http://user:password@localhost:8191"},
    {"flaresolverr_url": "http://localhost:8191/?key=secret"},
    {"flaresolverr_url": "http://localhost:bad"},
    {"flaresolverr_timeout_seconds": 0},
])
def test_browser_settings_validation(config):
    with pytest.raises(ValidationError):
        plugin.Settings.model_validate(config)


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_solver_url_normalizes_api_path(suffix):
    settings = plugin.Settings(flaresolverr_url="http://solver:8191" + suffix)
    assert settings.flaresolverr_url == "http://solver:8191/v1"


def solver_response(payload, *, html=None, url=None, status=200, headers=None):
    if payload["cmd"] == "sessions.create":
        return httpx.Response(200, json={"status": "ok", "session": payload["session"]})
    if payload["cmd"] == "sessions.destroy":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(200, json={"status": "ok", "solution": {
        "url": url or payload["url"], "status": status,
        "headers": headers or {}, "response": html or page("ABC-001"),
        "cookies": [{"name": "cf_clearance", "value": "solver-secret"}],
        "userAgent": "browser-user-agent",
    }})


@pytest.mark.parametrize("status,headers,html", [
    (403, {}, "blocked"),
    (200, {"cf-mitigated": "challenge"}, "checking"),
    (503, {}, "<title>Just a moment...</title>"),
    (200, {}, "<script>window._cf_chl_opt={}</script>"),
])
def test_cf_fallback_uses_one_browser_session_for_all_pages(monkeypatch, status, headers, html):
    direct_calls, solver_calls = [], []

    def handler(request):
        if request.url.host == "supjav.com":
            direct_calls.append(str(request.url))
            return httpx.Response(status, text=html, headers=headers)
        assert request.headers.get("Cookie") is None
        assert request.headers.get("Referer") is None
        assert request.url.path == "/v1"
        payload = json.loads(request.content)
        solver_calls.append(payload)
        if payload["cmd"] == "request.get":
            current, _ = plugin.ranking_page_url(payload["url"], plugin.BASE_URL, "week")
            assert payload["maxTimeout"] == 90000
            assert parse_qs(urlsplit(payload["url"]).query)["sort"] == ["week"]
            return solver_response(payload, html=page(f"ABC-00{current}", links=link(3), start=current))
        return solver_response(payload)

    factory = mock_client(monkeypatch, handler)
    config = plugin.Settings(flaresolverr_url="http://solver:8191", cookie="direct-secret", flaresolverr_timeout_seconds=90)
    assert plugin.fetch_numbers("week", config) == ["ABC-001", "ABC-002", "ABC-003"]
    assert len(direct_calls) == 1
    assert [call["cmd"] for call in solver_calls] == ["sessions.create", "request.get", "request.get", "request.get", "sessions.destroy"]
    assert len({call["session"] for call in solver_calls}) == 1
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["timeout"] == 100


def test_cf_detected_after_first_page_switches_remaining_pages(monkeypatch):
    direct, browser = [], []

    def handler(request):
        if request.url.host == "supjav.com":
            current, _ = plugin.ranking_page_url(str(request.url), plugin.BASE_URL, "day")
            direct.append(current)
            return httpx.Response(200, text=page("ABC-001", links=link(3))) if current == 1 else httpx.Response(403)
        payload = json.loads(request.content)
        if payload["cmd"] == "request.get":
            current, _ = plugin.ranking_page_url(payload["url"], plugin.BASE_URL, "day")
            browser.append(current)
            return solver_response(payload, html=page(f"ABC-00{current}", start=current))
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    assert plugin.fetch_numbers("day", plugin.Settings(flaresolverr_url="http://solver:8191")) == ["ABC-001", "ABC-002", "ABC-003"]
    assert direct == [1, 2]
    assert browser == [2, 3]


@pytest.mark.parametrize("mode,challenged", [("auto", False), ("direct", True)])
def test_direct_request_does_not_call_solver_unnecessarily(monkeypatch, mode, challenged):
    def handler(request):
        assert request.url.host == "supjav.com"
        return httpx.Response(403 if challenged else 200, text=page("ABC-001"))

    mock_client(monkeypatch, handler)
    config = plugin.Settings(request_mode=mode, flaresolverr_url="http://solver:8191")
    if challenged:
        with pytest.raises(plugin.CloudflareChallengeError):
            plugin.fetch_numbers("day", config)
    else:
        assert plugin.fetch_numbers("day", config) == ["ABC-001"]


def test_browser_mode_skips_direct_request_and_cleans_up_after_timeout(monkeypatch):
    calls = []

    def handler(request):
        assert request.url.host == "solver"
        payload = json.loads(request.content)
        calls.append(payload["cmd"])
        if payload["cmd"] == "request.get":
            raise httpx.ReadTimeout("solver-secret", request=request)
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="FlareSolverr 请求超时") as error:
        plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert "solver-secret" not in str(error.value)
    assert calls == ["sessions.create", "request.get", "sessions.destroy"]


def test_ambiguous_session_creation_also_attempts_cleanup(monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["cmd"] == "sessions.create":
            raise httpx.ReadTimeout("timeout", request=request)
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="请求超时"):
        plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert [call["cmd"] for call in calls] == ["sessions.create", "sessions.destroy"]
    assert calls[0]["session"] == calls[1]["session"]


def test_browser_verification_timeout_retries_current_page_with_fresh_session(monkeypatch):
    calls, failed = [], False

    def handler(request):
        nonlocal failed
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["cmd"] == "request.get":
            current, _ = plugin.ranking_page_url(payload["url"], plugin.BASE_URL, "day")
            if current == 2 and not failed:
                failed = True
                return httpx.Response(500, json={"status": "error", "message": "Timeout after 60 seconds"})
            return solver_response(payload, html=page(f"ABC-00{current}", links=link(2), start=current))
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    result = plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert result == ["ABC-001", "ABC-002"]
    creates = [call for call in calls if call["cmd"] == "sessions.create"]
    assert len(creates) == 2 and creates[0]["session"] != creates[1]["session"]
    assert [plugin.ranking_page_url(call["url"], plugin.BASE_URL, "day")[0] for call in calls if call["cmd"] == "request.get"] == [1, 2, 2]
    assert [call["session"] for call in calls if call["cmd"] == "sessions.destroy"] == [call["session"] for call in creates]


def test_repeated_browser_verification_timeout_stops_after_two_attempts(monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["cmd"] == "request.get":
            return httpx.Response(500, json={"status": "error", "message": "Timeout after 60 seconds"})
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    with pytest.raises(plugin.CloudflareChallengeError, match="验证超时"):
        plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert sum(call["cmd"] == "request.get" for call in calls) == 2
    assert sum(call["cmd"] == "sessions.destroy" for call in calls) == 2


@pytest.mark.parametrize("html,status,headers,url", [
    ("<title>Just a moment...</title>", 200, {}, None),
    ("checking", 200, {"CF-Mitigated": "challenge"}, None),
    ("blocked", 403, {}, None), ("error", 500, {}, None),
    (None, 200, {}, "https://other.example/popular?sort=week"),
    (None, 200, {}, "https://supjav.com/popular"),
    (None, 200, {}, "https://supjav.com/popular/page/2/?sort=week"),
])
def test_browser_result_must_be_the_requested_unblocked_board(monkeypatch, html, status, headers, url):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["cmd"])
        return solver_response(payload, html=html, status=status, headers=headers, url=url)

    mock_client(monkeypatch, handler)
    with pytest.raises((RuntimeError, ValueError)):
        plugin.fetch_numbers("week", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert calls[-1] == "sessions.destroy"


@pytest.mark.parametrize("api_status", [200, 500])
def test_solver_failure_does_not_expose_returned_cookie_and_cleanup_error_does_not_mask_it(monkeypatch, api_status):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["cmd"])
        if payload["cmd"] == "request.get":
            return httpx.Response(api_status, json={"status": "error", "message": "Captcha detected cf_clearance=secret"})
        if payload["cmd"] == "sessions.destroy":
            return httpx.Response(503)
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    with pytest.raises(plugin.CloudflareChallengeError, match="人工验证码") as error:
        plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191"))
    assert "secret" not in str(error.value)
    assert calls[-1] == "sessions.destroy"


def test_regular_cloudflare_javascript_does_not_trigger_browser():
    assert not plugin.is_cloudflare_challenge(
        page("ABC-001") + '<script src="/cdn-cgi/challenge-platform/scripts/jsd/api.js"></script>', {},
    )


def test_already_destroyed_browser_session_is_successful_cleanup(monkeypatch):
    warnings = Mock()
    monkeypatch.setattr(plugin, "logger", SimpleNamespace(info=Mock(), warning=warnings))

    def handler(request):
        payload = json.loads(request.content)
        if payload["cmd"] == "sessions.destroy":
            return httpx.Response(500, json={"status": "error", "message": "Error: The session doesn't exist."})
        return solver_response(payload)

    mock_client(monkeypatch, handler)
    assert plugin.fetch_numbers("day", plugin.Settings(request_mode="flaresolverr", flaresolverr_url="http://solver:8191")) == ["ABC-001"]
    warnings.assert_not_called()


@pytest.fixture()
def registered_context(tmp_path):
    sources, owners = RANKING_SOURCES.copy(), RANKING_SOURCE_OWNERS.copy()
    context = PluginContext(plugin.PLUGIN_ID, {"request_interval_seconds": 0}, tmp_path)
    apply_plugin_ranking_sources((plugin.register(context),))
    try:
        yield context
    finally:
        RANKING_SOURCES.clear()
        RANKING_SOURCES.update(sources)
        RANKING_SOURCE_OWNERS.clear()
        RANKING_SOURCE_OWNERS.update(owners)


def test_host_sync_stores_three_boards_in_page_order_and_replaces_old_items(test_db, registered_context, monkeypatch):
    movies = [Movie.create(movie_number=f"ABC-00{i}", javdb_id=f"id{i}", title=f"Movie {i}") for i in range(1, 5)]
    RankingItem.create(source_key="supjav", board_key="day", period="", rank=1, movie=movies[3], movie_number="ABC-004")

    def handler(request):
        board = parse_qs(request.url.query.decode()).get("sort", ["day"])[0]
        is_second = "/page/2" in request.url.path
        number = "ABC-003" if is_second else {"day": "ABC-002", "week": "ABC-001", "month": "ABC-004"}[board]
        return httpx.Response(200, text=page(number, links="" if is_second else link(2), start=20 if is_second else 10))

    mock_client(monkeypatch, handler)
    result = plugin.run_sync(registered_context, Reporter(), {})
    assert result["success_targets"] == 3
    assert result["stored_items"] == result["local_hit_movies"] == 6
    for board, first in (("day", "ABC-002"), ("week", "ABC-001"), ("month", "ABC-004")):
        rows = RankingItem.select().where(RankingItem.board_key == board).order_by(RankingItem.rank)
        assert [(row.rank, row.movie_number) for row in rows] == [(1, first), (2, "ABC-003")]
    assert Movie.select().count() == 4


def test_frontend_reads_all_synced_boards_with_default_period(test_db, registered_context, monkeypatch):
    for i in range(1, 4):
        Movie.create(movie_number=f"ABC-00{i}", javdb_id=f"id{i}", title=f"Movie {i}", heat=i)
    mock_client(monkeypatch, lambda _: httpx.Response(200, text=page("ABC-001", "ABC-002", "ABC-003")))
    assert plugin.run_sync(registered_context, Reporter(), {})["stored_items"] == 9
    app = FastAPI()
    app.include_router(ranking_router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[db_deps] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: object()
    with TestClient(app) as client:
        boards = client.get("/ranking-sources/supjav/boards").json()
        assert all(board["supported_periods"] == [] for board in boards)
        for board in boards:
            path = f"/ranking-sources/supjav/boards/{board['board_key']}/items"
            # 前端对无周期榜单使用 daily；它必须读取空周期下已经抓取的条目。
            query = {"period": "daily", "page": 1, "page_size": 24}
            response = client.get(path, params=query)
            assert response.status_code == 200, response.json()
            body = response.json()
            assert body["total"] == 3
            assert body["synced_at"] is not None
            assert [(item["movie_number"], item["rank"]) for item in body["items"]] == [
                ("ABC-001", 1), ("ABC-002", 2), ("ABC-003", 3),
            ]
            assert client.get(path, params={**query, "period": ""}).json() == body
            assert client.get(path, params={"page_size": 24}).json() == body
            sorted_response = client.get(path, params={**query, "sort": "heat:desc", "page_size": 2})
            assert sorted_response.status_code == 200
            assert [item["rank"] for item in sorted_response.json()["items"]] == [3, 2]
            next_page = client.get(path, params={**query, "sort": "heat:desc", "page_size": 2, "page": 2})
            assert [item["rank"] for item in next_page.json()["items"]] == [1]
    assert {row.period for row in RankingItem.select()} == {""}


def test_failed_later_page_preserves_old_board_and_other_boards_update(test_db, registered_context, monkeypatch):
    old = Movie.create(movie_number="ABC-001", javdb_id="old", title="Old")
    Movie.create(movie_number="ABC-002", javdb_id="new", title="New")
    RankingItem.create(source_key="supjav", board_key="week", period="", rank=5, movie=old, movie_number=old.movie_number)

    def handler(request):
        board = parse_qs(request.url.query.decode()).get("sort", ["day"])[0]
        if "/page/2" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, text=page("ABC-002", links=link(2) if board == "week" else ""))

    mock_client(monkeypatch, handler)
    reporter = Reporter()
    with pytest.raises(RuntimeError, match="失败 1 个榜单"):
        plugin.run_sync(registered_context, reporter, {})
    assert reporter.summary["failed_targets"] == 1
    assert reporter.summary["success_targets"] == 2
    old_row = RankingItem.get(RankingItem.board_key == "week")
    assert (old_row.rank, old_row.movie_number) == (5, "ABC-001")
    assert RankingItem.select().count() == 3


def test_task_rejects_params_before_sync():
    context = SimpleNamespace(sync_ranking_sources=Mock())
    with pytest.raises(ValueError, match="不接受参数"):
        plugin.run_sync(context, Reporter(), {"unexpected": True})
    context.sync_ranking_sources.assert_not_called()


@pytest.mark.parametrize("config,hour,minute", [
    ({}, 1, 0),
    ({"daily_run_time": "00:00"}, 0, 0),
    ({"daily_run_time": "23:59"}, 23, 59),
    ({"daily_run_time": " 12:34 "}, 12, 34),
])
def test_packaged_plugin_loads_three_boards_and_daily_job(tmp_path, config, hour, minute):
    archive = tmp_path / "supjav.zip"
    with ZipFile(archive, "w") as output:
        for name in ("__init__.py", "plugin.py", "manifest.json", "README.md"):
            output.write(SOURCE / name, name)
    destination = tmp_path / "plugins" / plugin.PLUGIN_ID
    safe_extract_zip(archive, destination)
    settings_model = load_plugin_settings_model(destination)
    assert settings_model().timeout_seconds == 30
    assert settings_model().daily_run_time == "01:00"
    time_schema = settings_model.model_json_schema()["properties"]["daily_run_time"]
    assert time_schema["title"] == "每日运行时刻"
    assert time_schema["default"] == "01:00"
    registration = check_plugin_dir(
        plugin_dir=destination,
        plugin_settings=Plugins(settings={plugin.PLUGIN_ID: config}),
    )
    source = registration.extensions[0].data
    assert source.source_key == "supjav"
    assert [(board.key, board.name) for board in source.boards] == list(plugin.BOARDS)
    job, = registration.jobs
    assert job.task_key == plugin.TASK_KEY
    assert job.plugin_id == plugin.PLUGIN_ID
    assert job.default_cron == f"{minute} {hour} * * *"
    assert job.manual_trigger_allowed and not job.manual_only
    host_timezone = timezone(timedelta(hours=8))
    trigger = CronTrigger.from_crontab(job.default_cron, timezone=host_timezone)
    now = datetime(2026, 10, 7, tzinfo=host_timezone)
    first_fire = trigger.get_next_fire_time(None, now)
    assert first_fire == now.replace(hour=hour, minute=minute)
    assert trigger.get_next_fire_time(first_fire, first_fire) == first_fire + timedelta(days=1)
