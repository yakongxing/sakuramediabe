# Javguru 频道采集

每天读取 <https://t.me/s/javguru_updates> 的公开消息，提取番号、导入影片元数据，
追加到 `javguru` 收藏列表。没有可播放媒体的影片加入宿主下载订阅。

## 安装与运行

需要 Host API 10（包含本插件列表接口的后端版本）、Python 3.10。
插件使用宿主已有的 httpx、portalocker 和 pydantic，无额外依赖。

在插件管理页面上传 ZIP 并启用。ZIP 根目录包含 `manifest.json`、`__init__.py`、
`plugin.py` 和本说明。也可将这些文件放到 `<plugins.root_dir>/javguru_updates/`，
在宿主配置中将 `javguru_updates` 加入 `plugins.enabled` 后重启。

默认按宿主运行时区每天 01:00 执行。在插件任务设置中可修改 cron，等价配置：

```toml
[plugins.job_crons.javguru_updates]
javguru_channel_sync = "0 1 * * *"
```

在后台任务页面可手动执行「Javguru 频道采集入库与收藏」，或运行：

```bash
uv run python -m src.start.commands aps sync-javguru-channel
```

启用只注册任务；第一次手动或定时执行时开始补抓最近 7 天。之后通过消息 ID
增量读取，停机超过 7 天也会补抓上次位置之后的消息。

Telegram 请求超时为 30 秒，跟随宿主进程的 `HTTP_PROXY`、`HTTPS_PROXY`、
`NO_PROXY` 环境变量。无需 Telegram 登录或 Bot Token。

## 入库、收藏与下载

- 入库沿用宿主 JavDB 优先及已启用元数据插件的补充流程。已有影片复用，不覆盖其资料。
- `javguru` 是普通收藏列表：有同名列表则保留原设置和成员并追加，没有则新建。
  若同名列表属于系统或其他插件，任务报告冲突，不接管归属。停用或卸载插件后列表仍保留。
- 已有可播放媒体的影片只收藏，原订阅状态保持。黑名单影片只收藏，跳过新增订阅。
- 无可播放媒体的影片加入订阅；已订阅影片保留现有搜索重试状态。
- 宿主「订阅缺失影片自动下载」任务负责搜索资源和提交下载，随后由宿主下载同步及
  自动导入任务完成媒体入库。需配置元数据源、索引器和下载器，并启用这些宿主任务。
  **加入订阅不代表已经创建下载任务或下载成功**；搜索无资源时沿用宿主重试策略。
  宿主当前自动下载只选择完全没有媒体记录的影片；仅有失效媒体的影片也会加入订阅，
  但需按宿主流程清理失效记录后才会进入自动搜索。

## 解析与恢复

提取正文及链接显示文字中的多个番号，优先使用显示文字；有文字但没有番号的
`jav.guru` 链接可从地址补提取。忽略空白、排名图标等模板链接中的旧番号，
忽略纯数字网页 ID，不访问影片详情页。支持常见带分隔符番号、FC2、XXX-AV 和
六位日期加三位序号的数字番号；数字番号的 `-` 与 `_` 不互换。
图片内文字、旧消息编辑和不带分隔符的一般番号不在此次采集范围内。

`data/state.json` 保存固定的首次时间边界、消息游标、待重试番号及已完成番号；
`data/sync.lock` 防止多进程重叠执行。升级时应保留数据目录。

分页请求或页面格式异常不会推进游标；已有待办仍会尝试处理。单个番号失败不影响
其他番号，后续每日或手动运行会继续重试。任务失败时已完成的收藏和订阅仍然保留，
任务结果记录失败阶段、原因及消息链接。状态文件损坏会报错，不自动重置。

已完成的番号不会因重复消息而再次订阅或收藏；用户后续取消订阅、移出列表时，
插件尊重这些调整。失败待办尚未完成，会继续重试。

结果中的 `collected_movies` 表示本次成功确保收藏的影片数（包括原有成员），
`subscribed_movies` 表示本次新加入订阅数；另提供 `playable_skipped`、
`blacklisted_skipped`、`pending_movies`、`failed_items` 和 `crawl_error`。

## 从源码打包

在仓库根目录执行，输出可直接上传的安装包：

```bash
python -m zipfile -c /tmp/javguru_updates-1.0.0.zip \
  plugin_packages/javguru_updates/manifest.json \
  plugin_packages/javguru_updates/__init__.py \
  plugin_packages/javguru_updates/plugin.py \
  plugin_packages/javguru_updates/README.md
```
