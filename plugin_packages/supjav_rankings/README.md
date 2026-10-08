# Supjav 排行榜

提供 `supjav` 来源下的三个当前榜单：

| 榜单 key | 名称 | 来源 |
| --- | --- | --- |
| `day` | 日榜 | <https://supjav.com/popular> |
| `week` | 周榜 | <https://supjav.com/popular?sort=week> |
| `month` | 月榜 | <https://supjav.com/popular?sort=month> |

每次执行重新抓取三个榜单的所有分页，按页码和页面卡片顺序提取番号。
同一番号的不同投稿保留各自的位置，不按番号去重。榜单保存当前排名，不累积每日历史。

## 安装与运行

需要 Host API 10、Python 3.10，使用宿主已有的 httpx、loguru、pydantic，
无需安装额外依赖。

在插件管理页面上传 ZIP 并启用。ZIP 根目录包含 `manifest.json`、`__init__.py`、
`plugin.py` 和本说明。也可复制这些文件到 `<plugins.root_dir>/supjav_rankings/`，
将 `supjav_rankings` 加入 `plugins.enabled` 后重启后端和 APS。

启用后在排行榜来源中显示「Supjav」。每天按**宿主运行时区**更新全部三个榜单，
默认 **01:00**，可在插件设置的「每日运行时刻」中修改；运行时区由宿主的 `TZ` / 系统时区决定。
定时更新需要宿主调度器启用。首次数据在第一次手动或定时任务执行后生成。

1.1.2 修复重复番号造成的排名错位：例如网页第 3、17 项是同一番号的不同投稿，
两项都会保留，后续条目不再因去重而前移。升级并重启后端和 APS 后，手动执行一次
同步任务以替换旧排名。前端使用默认排序或「名次升序」对照网页；热度排序、名次降序
会改变显示顺序。数据是最近一次抓取的快照，网页在此之后仍可能更新。

1.1.1 新增 `daily_run_time` 设置，使用 `HH:MM`（24 小时制），允许 `00:00`–`23:59`。
例如设置为 `"23:30"` 后，每天 23:30 更新日、周、月榜。保存后重启后端和 APS 生效。
旧配置未填写该字段时继续在 01:00 运行。

后台任务页面可手动执行「同步 Supjav 日、周、月排行榜」，或运行：

```bash
uv run python -m src.start.commands aps sync-supjav-rankings
```

也可在插件任务设置中覆盖 cron，默认时刻的等价配置为：

```toml
[plugins.job_crons.supjav_rankings]
supjav_ranking_sync = "0 1 * * *"
```

宿主的任务 cron 覆盖优先于「每日运行时刻」。如已配置上述覆盖项，需删除该项，
才能按 `daily_run_time` 运行。

## 请求配置

1.1.0 新增 FlareSolverr 浏览器抓取，兼容 1.0.0 的配置与榜单数据。
直接请求跟随宿主进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 等环境变量。
插件配置表单支持以下可选参数：

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `daily_run_time` | `01:00` | 每日运行时刻，按宿主时区，使用 `HH:MM`（24 小时制） |
| `request_mode` | `auto` | `auto` 遇到验证页后切换浏览器，`direct` 直接请求，`flaresolverr` 全程使用浏览器 |
| `flaresolverr_url` | 空 | FlareSolverr 服务地址，自动补全 `/v1`，空值表示未接入服务 |
| `flaresolverr_timeout_seconds` | 60 | 浏览器处理站点验证的超时，允许 10–180 秒 |
| `user_agent` | Chrome 浏览器 User-Agent | 使用 Cookie 时应与获得 Cookie 的浏览器一致 |
| `cookie` | 空 | 浏览器发往 Supjav 的 Cookie 请求头内容，不含 `Cookie:` 前缀 |
| `timeout_seconds` | 30 | 单次请求超时，允许 1–120 秒 |
| `request_interval_seconds` | 1 | 分页请求间隔，允许 0–30 秒 |

推荐部署 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)。它使用浏览器
加载页面并等待验证结束，插件直接读取该浏览器的 HTML。每个榜单复用一个
浏览器会话，后续分页不将浏览器 Cookie 搬到普通 HTTP 客户端；完成或失败时
都尝试关闭会话。Cookie、浏览器指纹、网络出口在该浏览器会话中保持一致。

如果后端直接运行在主机上，可启动服务：

```bash
docker run -d --name flaresolverr --restart unless-stopped \
  -p 127.0.0.1:8191:8191 -e LOG_LEVEL=info \
  ghcr.io/flaresolverr/flaresolverr:latest
```

随后在插件设置填写：

```json
{
  "request_mode": "auto",
  "flaresolverr_url": "http://127.0.0.1:8191",
  "flaresolverr_timeout_seconds": 60
}
```

如果后端也运行在 Docker 中，应将两个容器连接到同一 Docker 网络，服务命名为
`flaresolverr`，在插件配置中使用 `http://flaresolverr:8191`。容器内的
`127.0.0.1` 指向后端容器本身。FlareSolverr API 仅供本机或私有网络访问。

`auto` 检测 HTTP 403、`cf-mitigated: challenge` 响应头和典型验证页 HTML，
包括 HTTP 200/503 返回的验证页。切换后当前页和其余分页全部使用浏览器。
若希望每日抓取直接从浏览器开始，将 `request_mode` 改为 `flaresolverr`。

FlareSolverr 浏览器独立运行；宿主的代理环境只影响直接访问 Supjav 的请求，
不控制浏览器网络出口。如需代理，请在 FlareSolverr 服务中配置 `PROXY_URL`
及其相关环境变量。插件访问控制 API 不使用宿主代理环境，也不携带 Supjav Cookie。
`user_agent`、`cookie` 两项仅用于直接请求；浏览器使用自己的 User-Agent 和 Cookie。

未部署服务时，也可使用 `direct` 配合已通过验证的 Cookie 和对应 User-Agent，
请求出口应与取得 Cookie 时一致，失效后需要更新。Cookie 不写入插件日志。
浏览器方式不能保证完成所有 CF 验证；人工验证码、IP 拦截或验证超时仍会报错
并保留旧榜单。FlareSolverr 的具体失败原因可在其服务日志中查看。

## 抓取与更新语义

- 解析 `.post` 卡片的 `.img` 链接标题，缺失时使用卡片标题或图片 alt；
  不读取推荐侧栏、不访问影片详情、不下载视频。
- 支持常见带分隔符或紧凑形式的番号、FC2、XXX-AV 和数字番号。
  没有可识别番号的投稿跳过；若整榜无可识别番号则报错并保留旧榜。
- 从分页区、下一页链接和页码链接发现全部页面，包括省略号隐藏的中间页。
  周、月榜的后续页面始终携带对应 `sort` 参数。同番号的不同投稿在同页、跨页均保留，
  入库和默认接口排序沿用抓取顺序。
- 直接请求的超时、网络错误、HTTP 408/429/部分 5xx 最多尝试三次；
  识别到验证页后立即切换浏览器，不重复直接请求。浏览器验证按配置的超时等待。
  浏览器验证超时或仍返回验证页时，以新会话重试当前页一次；人工验证码不重复尝试。
- 全部分页成功后才交给宿主更新该榜；页面结构异常、分页越界、跨榜、跨站、
  分页内容重复或任一页失败时，该榜旧数据保留。超过 1000 页报错，不提交截断榜单。
- 三个榜单独立更新；某榜失败仍继续处理其他榜，最后将任务标记为失败。
  任务进度包含 `success_targets`、`failed_targets`、`fetched_numbers`、
  `stored_items` 等统计，失败详情见任务日志。
- 已有影片复用宿主记录；缺失影片通过宿主 JavDB 获取详情入库。
  无法入库的番号由宿主跳过，成功条目保留提取序列中的排名，可能有排名空缺。
  实际显示数量也会受黑名单过滤影响。

页面规则参照公开的 [Supjav 采集脚本](https://github.com/cluntop/tvbox/blob/main/js/supjav.user.js)
进行适配。测试覆盖固定 HTML、同页和跨页重复番号的名次保留、CF 自动回退、
浏览器会话复用及清理、请求失败、返回结果校验和宿主及前端接口集成。
浏览器返回验证页、其他站点、错误分页或丢失 `sort`
参数时拒绝提交，避免将日榜写入周/月榜。

2026-10-07 使用 1.1.1 通过临时 FlareSolverr Docker 服务进行了只读实测：三个榜单均完成
全部 3 页抓取，日榜得到 71 个、周榜 66 个、月榜 65 个去重番号。
部分分页第一次验证超时，新会话重试后成功。这是本次站点状态和网络出口下的
结果；站点验证策略或出口改变后仍可能失败，不承诺永久通过全部 CF 验证。

## 打包

在仓库根目录执行以下命令，生成可直接上传的 ZIP：

```bash
python -m zipfile -c /tmp/supjav_rankings-1.1.2.zip \
  plugin_packages/supjav_rankings/manifest.json \
  plugin_packages/supjav_rankings/__init__.py \
  plugin_packages/supjav_rankings/plugin.py \
  plugin_packages/supjav_rankings/README.md
```
