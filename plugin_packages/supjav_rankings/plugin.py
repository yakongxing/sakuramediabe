"""Supjav 日、周、月榜全分页采集；入库与榜单替换由宿主完成。"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common.movie_numbers import normalize_movie_number
from src.plugins import (
    RANKING_SOURCE_EXTENSION_KEY,
    PluginContext,
    PluginExtension,
    PluginRankingBoard,
    PluginRankingSource,
    PluginRegistration,
)
from src.scheduler.contracts import JobDefinition

PLUGIN_ID = "supjav_rankings"
PLUGIN_VERSION = "1.1.1"
SOURCE_KEY = "supjav"
TASK_KEY = "supjav_ranking_sync"
BASE_URL = "https://supjav.com/popular"
BOARDS = (("day", "日榜"), ("week", "周榜"), ("month", "月榜"))
# 防止站点损坏的分页制造无限循环；超过上限报错，绝不提交截断榜单。
MAX_PAGES = 1000
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
NUMBER_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:"
    r"FC2[-_ ]*(?:PPV[-_ ]*)?\d{5,9}|"
    r"\d{6}[-_]\d{3}|XXX[-_]AV[-_]\d+|"
    r"(?:\d{1,4})?[A-Z][A-Z0-9]{1,11}[-_]S?\d{2,7}|"
    r"[A-Z]{2,6}[ ]\d{3,6}|[A-Z]{2,6}\d{3,6}|N\d{4}"
    r")(?![A-Z0-9])",
    re.IGNORECASE,
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    daily_run_time: str = Field(
        default="01:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        title="每日运行时刻",
        description="宿主运行时区的每日运行时刻，使用 HH:MM（24 小时制），例如 01:00 或 23:30；保存后重启后端和 APS 生效。",
    )
    request_mode: Literal["auto", "direct", "flaresolverr"] = Field(
        default="auto", title="抓取方式",
        description="auto：遇到 CF 验证时切换 FlareSolverr；direct：直接请求；flaresolverr：全部使用浏览器。",
    )
    flaresolverr_url: str = Field(
        default="", title="FlareSolverr 地址",
        description="例如 http://flaresolverr:8191 或 http://127.0.0.1:8191；自动补全 /v1。",
    )
    flaresolverr_timeout_seconds: int = Field(
        default=60, ge=10, le=180, title="浏览器验证超时（秒）",
    )
    user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        min_length=1,
        title="User-Agent",
        description="直接请求使用；填写获得 Cookie 的浏览器 User-Agent。FlareSolverr 使用自己的浏览器。",
    )
    cookie: str = Field(
        default="", title="Supjav Cookie",
        description="可选，填写浏览器请求中的 Cookie；不包含 Cookie: 前缀。",
        json_schema_extra={"format": "password"},
    )
    timeout_seconds: float = Field(default=30, ge=1, le=120, title="请求超时（秒）")
    request_interval_seconds: float = Field(
        default=1, ge=0, le=30, title="分页请求间隔（秒）",
    )

    @field_validator("user_agent", "cookie")
    @classmethod
    def _validate_header(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("请求头不能包含换行")
        return value.strip()

    @field_validator("flaresolverr_url")
    @classmethod
    def _validate_solver_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "\r" in value or "\n" in value):
            raise ValueError("FlareSolverr 地址须为 HTTP(S) URL，不包含凭据、查询参数或片段")
        # 同时校验端口，避免非法地址留到执行阶段才报错。
        _ = parts.port
        value = value.rstrip("/")
        return value if value.endswith("/v1") else value + "/v1"

    @model_validator(mode="after")
    def _require_solver_url(self):
        if self.request_mode == "flaresolverr" and not self.flaresolverr_url:
            raise ValueError("浏览器抓取方式必须填写 FlareSolverr 地址")
        return self


class CloudflareChallengeError(RuntimeError):
    """直接请求或浏览器仍被站点验证拦截；与普通解析错误区分。"""


class CaptchaRequiredError(CloudflareChallengeError):
    """人工验证码不能通过新建会话自动重试解决。"""


def is_cloudflare_challenge(html: str, headers) -> bool:
    # cf-mitigated 是 CF 官方的验证页标记，HTTP 200 也可能是验证页面。
    if any(str(key).lower() == "cf-mitigated" and str(value).lower() == "challenge"
           for key, value in headers.items()):
        return True
    return re.search(
        r"(?:\b_cf_chl_opt\b|\bcf_chl_opt\b|"
        r"<title[^>]*>\s*(?:Just a moment|Attention Required![^<]*Cloudflare)|"
        r"id=[\"']challenge-(?:running|stage|form)[\"'])",
        html, re.IGNORECASE,
    ) is not None


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self):
        # 推荐栏不属于排行榜，不提取其中的影片或分页。
        if (self.tag == "aside" or self.attrs.get("id") == "sidebar"
                or self.has_class("sidebar") or self.has_class("widget")):
            return
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)


class PageParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def extract_number(title: str) -> str | None:
    match = NUMBER_PATTERN.search(title)
    if match is None:
        return None
    number = match.group().upper()
    if number.startswith("FC2"):
        return "FC2-" + re.search(r"\d{5,9}$", number).group()
    compact = re.fullmatch(r"([A-Z]{2,6})[ ]?(\d{3,6})", number)
    if compact:
        number = "-".join(compact.groups())
    return normalize_movie_number(number)


def ranking_page_url(href: str, current_url: str, board_key: str) -> tuple[int, str]:
    """只接受同站同榜分页；链接省略 sort 时仍保留当前榜单。"""
    parts = urlsplit(urljoin(current_url, href))
    if parts.scheme not in {"http", "https"} or parts.netloc not in {"supjav.com", "www.supjav.com"}:
        raise ValueError("Supjav 分页链接指向其他站点")
    path = re.fullmatch(r"/popular(?:/page/(\d+))?/?", parts.path)
    if path is None:
        raise ValueError("Supjav 分页链接不是排行榜页面")
    query = parse_qs(parts.query, keep_blank_values=True)
    sort = query.get("sort", [board_key])
    if sort != [board_key]:
        raise ValueError("Supjav 分页链接切换了榜单")
    raw_pages = [path.group(1)] if path.group(1) else []
    for key in ("page", "paged"):
        raw_pages.extend(query.get(key, []))
    if any(not value.isdecimal() or not 1 <= int(value) <= MAX_PAGES for value in raw_pages):
        raise ValueError("Supjav 分页页码异常或超过 1000 页")
    if len({int(value) for value in raw_pages}) > 1:
        raise ValueError("Supjav 分页页码互相矛盾")
    page_number = int(raw_pages[0]) if raw_pages else 1
    # 固定 sort，避免 WordPress 分页链接丢失查询参数后混入日榜。
    if board_key == "day":
        query.pop("sort", None)
    else:
        query["sort"] = [board_key]
    return page_number, urlunsplit(("https", parts.netloc, parts.path, urlencode(query, doseq=True), ""))


@dataclass(frozen=True)
class RankingPage:
    numbers: tuple[str, ...]
    post_urls: tuple[str, ...]
    pages: dict[int, str]


def parse_page(html: str, current_url: str, board_key: str) -> RankingPage:
    parser = PageParser()
    parser.feed(html)
    nodes = list(parser.root.walk())
    posts = [node for node in nodes if node.has_class("post")]
    if not posts:
        raise ValueError("Supjav 页面没有排行榜影片卡片，可能需要通过站点验证")
    numbers, post_urls = [], []
    for post in posts:
        descendants = list(post.walk())
        anchor = next((n for n in descendants if n.tag == "a" and n.has_class("img")), None)
        if anchor is None:
            raise ValueError("Supjav 影片卡片缺少 .img 链接")
        href = anchor.attrs.get("href", "")
        link = urlsplit(urljoin(current_url, href))
        if not href or link.scheme not in {"http", "https"} or link.hostname not in {"supjav.com", "www.supjav.com"}:
            raise ValueError("Supjav 影片卡片链接异常")
        titles = [anchor.attrs.get("title", "")]
        titles.extend(n.text() for n in descendants if n.tag in {"h2", "h3"})
        titles.extend(n.attrs.get("alt", "") for n in descendants if n.tag == "img")
        title = next((text.strip() for text in titles if text.strip()), None)
        if title is None:
            raise ValueError("Supjav 影片卡片标题缺失")
        post_urls.append(link.path.rstrip("/"))
        number = extract_number(title)
        if number:
            numbers.append(number)

    pages = {}
    pagination_nodes = {}
    for node in nodes:
        if any(node.has_class(name) for name in ("pagination", "wp-pagenavi", "nav-links")):
            for child in node.walk():
                if child.tag == "a" and child.attrs.get("href"):
                    pagination_nodes.setdefault(id(child), (child, False))
                if child.has_class("next-page") or child.has_class("next"):
                    for anchor in child.walk():
                        if anchor.tag == "a" and anchor.attrs.get("href"):
                            pagination_nodes[id(anchor)] = (anchor, True)
        if node.tag == "a" and node.has_class("page-numbers") and node.attrs.get("href"):
            pagination_nodes.setdefault(id(node), (node, node.has_class("next")))
        if node.tag in {"a", "link"} and "next" in node.attrs.get("rel", "").split():
            pagination_nodes[id(node)] = (node, True)
    current_page, _ = ranking_page_url(current_url, current_url, board_key)
    for node, is_next in pagination_nodes.values():
        page_number, url = ranking_page_url(node.attrs.get("href", ""), current_url, board_key)
        if is_next and page_number <= current_page:
            raise ValueError("Supjav 下一页没有向后推进")
        pages[page_number] = url
    return RankingPage(tuple(numbers), tuple(post_urls), pages)


def missing_page_url(page_urls: dict[int, str], page_number: int) -> str:
    # 沿用已发现分页的路径或查询参数格式，补齐省略号隐藏的中间页。
    template = next(url for page, url in sorted(page_urls.items()) if page > 1)
    parts = urlsplit(template)
    path = re.sub(r"/page/\d+", f"/page/{page_number}", parts.path)
    query = parse_qs(parts.query, keep_blank_values=True)
    for key in ("page", "paged"):
        if key in query:
            query[key] = [str(page_number)]
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query, doseq=True), ""))


def request_page(client: httpx.Client, url: str, board_key: str) -> str:
    expected_page, _ = ranking_page_url(url, url, board_key)
    for _redirect in range(6):
        for attempt in range(3):
            try:
                response = client.get(url)
                if is_cloudflare_challenge(response.text, response.headers):
                    raise CloudflareChallengeError("Supjav 返回 Cloudflare 验证页")
                if response.status_code not in RETRYABLE_STATUSES or attempt == 2:
                    break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt == 2:
                    raise
            time.sleep(attempt + 1)
        if response.is_redirect:
            page, url = ranking_page_url(response.headers.get("location", ""), url, board_key)
            if page != expected_page:
                raise ValueError("Supjav 请求被重定向到其他分页")
            continue
        if response.status_code == 403:
            raise CloudflareChallengeError("Supjav 返回 HTTP 403，可能需要浏览器验证")
        response.raise_for_status()
        return response.text
    raise ValueError("Supjav 重定向次数过多")


class RankingPageClient:
    """一个榜单一个浏览器会话；一旦切换浏览器，所有后续页都在该会话抓取。"""

    def __init__(self, settings: Settings, board_key: str):
        self.settings = settings
        self.board_key = board_key
        self.direct_client = None
        self.solver_client = None
        self.session = None
        self.sessions_to_destroy: set[str] = set()
        self.use_browser = settings.request_mode == "flaresolverr"

    def __enter__(self):
        headers = {"User-Agent": self.settings.user_agent, "Referer": BASE_URL}
        if self.settings.cookie:
            headers["Cookie"] = self.settings.cookie
        self.direct_client = httpx.Client(
            timeout=self.settings.timeout_seconds, headers=headers, follow_redirects=False,
        )
        return self

    def __exit__(self, *_args):
        try:
            for session in tuple(self.sessions_to_destroy):
                self._destroy_session(session)
        finally:
            if self.solver_client is not None:
                self.solver_client.close()
            self.direct_client.close()

    def get(self, url: str) -> str:
        if not self.use_browser:
            try:
                return request_page(self.direct_client, url, self.board_key)
            except CloudflareChallengeError as exc:
                if self.settings.request_mode == "direct" or not self.settings.flaresolverr_url:
                    raise CloudflareChallengeError(
                        f"{exc}；请部署并配置 FlareSolverr（request_mode=auto 或 flaresolverr），"
                        "或更新直接请求的 Cookie、User-Agent"
                    ) from exc
                logger.info("Supjav 检测到站点验证，切换 FlareSolverr 浏览器 board={}", self.board_key)
                self.use_browser = True
        for attempt in range(2):
            try:
                return self._browser_page(url)
            except CaptchaRequiredError:
                raise
            except CloudflareChallengeError:
                if attempt == 1:
                    raise
                self._destroy_session(self.session)
                logger.warning("Supjav 浏览器验证未通过，使用新会话重试当前页 board={}", self.board_key)
                time.sleep(max(2, self.settings.request_interval_seconds))
        raise AssertionError("浏览器重试没有返回结果")

    def _destroy_session(self, session: str) -> None:
        try:
            self._solver_api({"cmd": "sessions.destroy", "session": session}, timeout=10)
            self.sessions_to_destroy.discard(session)
        except Exception:
            logger.warning("Supjav FlareSolverr 会话清理失败，请检查 FlareSolverr 服务")
        finally:
            if self.session == session:
                self.session = None

    def _solver_api(self, payload: dict, *, timeout: float | None = None) -> dict:
        if self.solver_client is None:
            # 控制 API 与 Supjav 请求分开，Cookie 不发送给控制 API；本地 API 不跟随代理环境。
            self.solver_client = httpx.Client(
                timeout=self.settings.flaresolverr_timeout_seconds + 10,
                trust_env=False, follow_redirects=False,
            )
        try:
            response = self.solver_client.post(
                self.settings.flaresolverr_url, json=payload,
                timeout=timeout or self.settings.flaresolverr_timeout_seconds + 10,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("FlareSolverr 请求超时，请检查服务或增加浏览器验证超时") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("无法连接 FlareSolverr，请检查地址、服务和容器网络") from exc
        try:
            result = response.json()
        except ValueError as exc:
            if response.status_code != 200:
                raise RuntimeError(f"FlareSolverr API 返回 HTTP {response.status_code}，请检查服务地址") from exc
            raise ValueError("FlareSolverr API 未返回有效 JSON") from exc
        if isinstance(result, dict) and result.get("status") == "error":
            # 不将外部服务可能包含 Cookie 的原始 message 写入日志。
            message = str(result.get("message", ""))
            if (payload.get("cmd") == "sessions.destroy"
                    and any(text in message.lower() for text in ("session doesn't exist", "session does not exist"))):
                # 清理响应超时后服务可能已经完成删除；重复清理视为成功。
                return {"status": "ok"}
            if "captcha" in message.lower():
                raise CaptchaRequiredError("FlareSolverr 遇到人工验证码，无法自动完成；请检查服务日志")
            if "timeout" in message.lower() or "timed out" in message.lower():
                raise CloudflareChallengeError("FlareSolverr 浏览器验证超时，未更新榜单")
            raise RuntimeError("FlareSolverr 未完成浏览器请求，可能验证超时；请查看 FlareSolverr 日志")
        if response.status_code != 200:
            raise RuntimeError(f"FlareSolverr API 返回 HTTP {response.status_code}，请检查服务地址")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ValueError("FlareSolverr API 响应格式异常")
        return result

    def _browser_page(self, url: str) -> str:
        if self.session is None:
            # 在调用前记录自己生成的 ID：创建请求超时也会尝试清理可能已建立的会话。
            self.session = f"supjav-{uuid.uuid4().hex}"
            self.sessions_to_destroy.add(self.session)
            result = self._solver_api({"cmd": "sessions.create", "session": self.session})
            if result.get("session") != self.session:
                raise ValueError("FlareSolverr 创建会话响应异常")
        result = self._solver_api({
            "cmd": "request.get", "session": self.session, "url": url,
            "maxTimeout": self.settings.flaresolverr_timeout_seconds * 1000,
        })
        solution = result.get("solution")
        if not isinstance(solution, dict):
            raise TypeError("FlareSolverr 缺少浏览器结果")
        html, status = solution.get("response"), solution.get("status")
        if not isinstance(html, str) or not html.strip() or type(status) is not int:
            raise ValueError("FlareSolverr 浏览器结果格式异常")
        headers = solution.get("headers", {})
        if not isinstance(headers, dict):
            raise TypeError("FlareSolverr 浏览器响应头格式异常")
        if is_cloudflare_challenge(html, headers) or status == 403:
            raise CloudflareChallengeError("FlareSolverr 浏览器仍被 Cloudflare 拦截，未更新榜单")
        if not 200 <= status < 300:
            raise RuntimeError(f"FlareSolverr 浏览器请求返回 HTTP {status}，未更新榜单")
        final_url = solution.get("url")
        if not isinstance(final_url, str) or not final_url:
            raise ValueError("FlareSolverr 浏览器结果缺少最终 URL")
        expected_page, _ = ranking_page_url(url, url, self.board_key)
        actual_page, _ = ranking_page_url(final_url, url, self.board_key)
        # 浏览器已跟随重定向，不能将丢失 sort 的日榜结果误当作周/月榜。
        sort = parse_qs(urlsplit(final_url).query).get("sort", ["day"])
        if actual_page != expected_page or sort != [self.board_key]:
            raise ValueError("FlareSolverr 浏览器返回了其他榜单或分页，未更新榜单")
        return html


def fetch_numbers(board_key: str, settings: Settings, period: str = "") -> list[str]:
    if board_key not in dict(BOARDS) or period:
        raise ValueError("Supjav 只支持日、周、月三个当前榜单")
    _, first_url = ranking_page_url(BASE_URL, BASE_URL, board_key)
    page_urls = {1: first_url}
    numbers = {}
    signatures = set()
    page_number, last_page = 1, 1
    with RankingPageClient(settings, board_key) as client:
        while page_number <= last_page:
            if page_number > 1:
                time.sleep(settings.request_interval_seconds)
            # 总页数链接可能跨过省略号；中间页也必须逐一读取。
            url = page_urls.get(page_number)
            if url is None:
                url = missing_page_url(page_urls, page_number)
            page = parse_page(client.get(url), url, board_key)
            if page.post_urls in signatures:
                raise ValueError("Supjav 不同分页返回了重复内容，未更新榜单")
            signatures.add(page.post_urls)
            numbers.update(dict.fromkeys(page.numbers))
            page_urls.update(page.pages)
            last_page = max(last_page, *page.pages) if page.pages else last_page
            logger.info("Supjav 排行榜 board={} page={}/{} numbers={}", board_key, page_number, last_page, len(numbers))
            page_number += 1
    if not numbers:
        raise ValueError("Supjav 全部页面未提取到番号，未更新榜单")
    return list(numbers)


def run_sync(context: PluginContext, reporter, params: dict):
    if params:
        raise ValueError("Supjav 排行榜同步不接受参数")
    summary = context.sync_ranking_sources(progress_callback=lambda payload: reporter.emit(**payload))
    if summary["failed_targets"]:
        raise RuntimeError(f"Supjav 排行榜同步失败 {summary['failed_targets']} 个榜单；失败榜单保留旧数据，请查看任务日志")
    return summary


def register(context: PluginContext) -> PluginRegistration:
    settings = Settings.model_validate(dict(context.settings))
    hour, minute = map(int, settings.daily_run_time.split(":"))
    return PluginRegistration(
        plugin_id=PLUGIN_ID, display_name="Supjav 排行榜", version=PLUGIN_VERSION,
        host_api_version=10,
        extensions=(PluginExtension(
            key=RANKING_SOURCE_EXTENSION_KEY,
            data=PluginRankingSource(
                source_key=SOURCE_KEY, name="Supjav",
                boards=tuple(PluginRankingBoard(
                    key=key, name=name,
                    fetch_numbers=lambda period, key=key: fetch_numbers(key, settings, period),
                ) for key, name in BOARDS),
            ),
        ),),
        jobs=(JobDefinition(
            task_key=TASK_KEY, log_name="supjav-ranking-sync", cli_name="sync-supjav-rankings",
            cli_help="同步 Supjav 日、周、月排行榜", default_cron=f"{minute} {hour} * * *",
            handler=lambda reporter, params: run_sync(context, reporter, params),
        ),),
    )
