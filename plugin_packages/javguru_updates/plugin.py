"""公开频道增量采集；影片入库、收藏和订阅通过宿主门面完成。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import portalocker
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.common.movie_numbers import normalize_movie_number
from src.plugins import PluginContext, PluginRegistration
from src.scheduler.contracts import JobDefinition

PLUGIN_ID = "javguru_updates"
CHANNEL_URL = "https://t.me/s/javguru_updates"
TASK_KEY = "javguru_channel_sync"
PLAYLIST_NAME = "javguru"
# 保守提取带分隔符的番号，避免把日期、分辨率、网页纯数字 ID 当影片。
NUMBER_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9])(?:"
    r"FC2[-_ ]?(?:PPV[-_ ]?)?\d{5,9}|"
    r"\d{6}[-_]\d{3}|XXX[-_]AV[-_]\d+|"
    r"[A-Z][A-Z0-9]{1,5}[-_]S?\d{2,7}|N\d{4}"
    r")(?![a-zA-Z0-9])",
    re.IGNORECASE,
)


def extract_numbers(text: str) -> list[str]:
    numbers = []
    for match in NUMBER_PATTERN.finditer(text):
        number = match.group().upper()
        if number.startswith("FC2"):
            number = "FC2-" + re.search(r"\d{5,9}$", number).group()
        numbers.append(normalize_movie_number(number))
    return list(dict.fromkeys(numbers))


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in {"br", "hr"}:
            return "\n"
        value = "".join(c.text() if isinstance(c, Node) else c for c in self.children)
        return value + ("\n" if self.tag in {"p", "div", "li"} else "")


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


@dataclass(frozen=True)
class Message:
    post_id: int
    published_at: datetime
    numbers: tuple[str, ...]


def parse_page(html: str) -> tuple[list[Message], int | None]:
    parser = PageParser()
    parser.feed(html)
    nodes = list(parser.root.walk())
    if not any(node.has_class("tgme_channel_history") for node in nodes):
        raise ValueError("Telegram 页面缺少频道消息区，未推进采集游标")
    messages = {}
    before = None
    for node in nodes:
        if node.tag == "link" and "prev" in node.attrs.get("rel", "").split():
            link = urlsplit(node.attrs.get("href", ""))
            if (link.netloc not in ("", "t.me")
                    or link.path != "/s/javguru_updates"):
                raise ValueError("Telegram 历史分页链接异常")
            raw = parse_qs(link.query).get("before", [""])[0]
            if not raw.isdecimal() or int(raw) <= 0:
                raise ValueError("Telegram 历史分页游标异常")
            before = int(raw)
        if not node.has_class("tgme_widget_message"):
            continue
        post = re.fullmatch(r"javguru_updates/(\d+)", node.attrs.get("data-post", ""))
        if post is None:
            raise ValueError("Telegram 消息 ID 缺失或频道不匹配")
        descendants = list(node.walk())
        timestamp = next((n.attrs.get("datetime") for n in descendants if n.tag == "time"), None)
        if not timestamp:
            raise ValueError("Telegram 消息缺少发布时间")
        published = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("Telegram 消息时间缺少时区")
        numbers = []
        for body in (n for n in descendants if n.has_class("tgme_widget_message_text")):
            numbers.extend(extract_numbers(body.text()))
            for anchor in (n for n in body.walk() if n.tag == "a"):
                label = anchor.text().strip()
                # 空白/排名图标链接可能是复制模板遗留，不将其地址当影片。
                if not label or not any(c.isalpha() for c in label) or extract_numbers(label):
                    continue
                link = urlsplit(anchor.attrs.get("href", ""))
                if link.scheme in {"http", "https"} and link.hostname in {"jav.guru", "www.jav.guru"}:
                    numbers.extend(extract_numbers(unquote(link.path)))
        post_id = int(post.group(1))
        messages[post_id] = Message(post_id, published, tuple(dict.fromkeys(numbers)))
    if not messages:
        # 空壳、限流或页面结构变化不能当作已读；该频道正常历史页有消息。
        raise ValueError("Telegram 页面没有可识别的消息，未推进采集游标")
    return sorted(messages.values(), key=lambda item: item.post_id), before


class PendingMovie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    movie_number: str = Field(min_length=1)
    post_id: int = Field(gt=0)
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None


class SyncState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    initial_since: AwareDatetime
    last_post_id: int = Field(default=0, ge=0)
    pending: dict[str, PendingMovie] = Field(default_factory=dict)
    completed: set[str] = Field(default_factory=set)


def save_state(path: Path, state: SyncState) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(state.model_dump_json(indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_state(path: Path) -> SyncState:
    if path.exists():
        # 损坏的状态必须报错，不能静默重置游标后重复创建订阅。
        return SyncState.model_validate_json(path.read_text(encoding="utf-8"))
    state = SyncState(initial_since=datetime.now(timezone.utc) - timedelta(days=7))
    save_state(path, state)
    return state


def collect_messages(client, state: SyncState, reporter) -> tuple[list[Message], int]:
    collected = {}
    before = None
    latest_id = state.last_post_id
    for page_index in range(1000):
        response = client.get(CHANNEL_URL, params={"before": before} if before else None)
        response.raise_for_status()
        messages, next_before = parse_page(response.text)
        latest_id = max(latest_id, messages[-1].post_id)
        for message in messages:
            if message.post_id <= state.last_post_id:
                continue
            if not state.last_post_id and message.published_at < state.initial_since:
                continue
            collected[message.post_id] = message
        reporter.emit(text=f"已读取频道第 {page_index + 1} 页，收集 {len(collected)} 条新消息")
        reached_boundary = (
            any(m.post_id <= state.last_post_id for m in messages)
            if state.last_post_id else
            any(m.published_at < state.initial_since for m in messages)
        )
        if reached_boundary or next_before is None:
            return sorted(collected.values(), key=lambda item: item.post_id), latest_id
        if next_before > messages[0].post_id or (before is not None and next_before >= before):
            raise ValueError("Telegram 分页未向历史推进，未更新采集游标")
        before = next_before
    raise ValueError("Telegram 分页超过 1000 页，未更新采集游标")


def run_sync(context: PluginContext, reporter, params: dict):
    if params:
        raise ValueError("频道采集任务不接受参数")
    directory = context.data_dir
    # 与手动触发、CLI 及其它 worker 互斥，防止状态文件的读改写互相覆盖。
    with portalocker.Lock(str(directory / "sync.lock"), mode="a", timeout=0):
        return _run_locked(context, reporter, directory / "state.json")


def _run_locked(context, reporter, state_path):
    state = load_state(state_path)
    playlist = context.collections.ensure_playlist_by_name(PLAYLIST_NAME)
    summary = {
        "new_messages": 0, "collected_movies": 0, "subscribed_movies": 0,
        "playable_skipped": 0, "blacklisted_skipped": 0,
        "failed_items": [], "crawl_error": None,
    }
    try:
        # 使用宿主已有 httpx 依赖；沿用环境变量 HTTP(S)_PROXY/NO_PROXY。
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            messages, latest_id = collect_messages(client, state, reporter)
    except Exception as exc:
        summary["crawl_error"] = str(exc)
        messages = []
    else:
        for message in messages:
            for number in message.numbers:
                if number not in state.completed and number not in state.pending:
                    state.pending[number] = PendingMovie(movie_number=number, post_id=message.post_id)
        state.last_post_id = latest_id
        # 采集完整成功后，一次原子写入待办和游标；后续入库失败不再阻塞采集。
        save_state(state_path, state)
        summary["new_messages"] = len(messages)

    pending = list(state.pending.items())
    for index, (key, item) in enumerate(pending, start=1):
        if key in state.completed:
            del state.pending[key]
            save_state(state_path, state)
            continue
        stage = "import"
        try:
            movie = context.import_movie_by_number(item.movie_number)
            canonical = movie.values["movie_number"]
            stage = "collection"
            context.collections.add_playlist_movies(playlist.collection_id, [canonical])
            summary["collected_movies"] += 1
            stage = "subscription"
            presence = context.media.presence_for_movies([movie.movie_id]).get(movie.movie_id)
            if presence is None:
                raise ValueError("影片在媒体检查前已被删除")
            if presence.has_playable:
                summary["playable_skipped"] += 1
            elif movie.values["is_blacklisted"]:
                summary["blacklisted_skipped"] += 1
            elif not movie.values["is_subscribed"]:
                context.subscriptions.subscribe(canonical)
                summary["subscribed_movies"] += 1
            state.completed.update((key, normalize_movie_number(canonical)))
            del state.pending[key]
        except Exception as exc:
            item.attempts += 1
            item.last_error = str(exc)
            summary["failed_items"].append({
                "movie_number": item.movie_number, "stage": stage,
                "detail": str(exc), "source_url": f"https://t.me/javguru_updates/{item.post_id}",
            })
        # 保存失败必须终止，不能在失去检查点后继续制造副作用。
        save_state(state_path, state)
        reporter.emit(
            current=index, total=len(pending),
            text=f"已处理 {index}/{len(pending)} 个番号；已收藏 {summary['collected_movies']}，"
                 f"已加入下载订阅 {summary['subscribed_movies']}",
            summary_patch=summary,
        )
    summary["pending_movies"] = len(state.pending)
    summary["last_post_id"] = state.last_post_id
    reporter.emit(current=len(pending), total=len(pending), summary_patch=summary)
    if summary["crawl_error"] or summary["failed_items"]:
        raise RuntimeError(
            f"Javguru 采集未全部完成：抓取错误={summary['crawl_error'] or '无'}，"
            f"待重试番号={len(state.pending)}；已完成的收藏和订阅已保留"
        )
    return summary


def register(context: PluginContext) -> PluginRegistration:
    return PluginRegistration(
        plugin_id=PLUGIN_ID, display_name="Javguru 频道采集", version="1.0.0",
        host_api_version=10,
        jobs=(JobDefinition(
            task_key=TASK_KEY, log_name="javguru-channel-sync", cli_name="sync-javguru-channel",
            cli_help="Javguru 频道采集入库与收藏", default_cron="0 1 * * *",
            handler=lambda reporter, params: run_sync(context, reporter, params),
        ),),
    )
