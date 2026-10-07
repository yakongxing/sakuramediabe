# SakuraMediaBE

SakuraMediaBE 是 SakuraMedia 的服务端项目，负责提供媒体库管理、影片元数据、下载任务、视频缩略图生成、精彩时刻标记、以图搜图、播放访问等后端能力。 前端项目仓库在 [sakuramedia](https://github.com/tinypinglite/sakuramedia)

项目当前基于 Python 3.10、FastAPI、Peewee、Pydantic 2 和 APScheduler 构建，代码结构按 `api -> service -> model` 分层组织，面向单账号场景运行。

## 数据库断线恢复

PostgreSQL 重启后，API 和后台工作线程会在后续数据库操作时自动替换失效连接，无需重启后端。事务外复用连接前执行一次 `SELECT 1` 探测；事务或媒体操作会话锁持有期间不更换连接。

连接不可用时，API 返回 HTTP `503`，错误码为 `database_unavailable`。执行途中断线的业务 SQL、写操作和事务不会自动重放；提交时断线可能无法确认提交结果，客户端不应直接重复提交写请求。后台任务沿用现有失败处理和租约恢复规则。

新连接的 `connect_timeout` 默认为 5 秒，可通过数据库 URL 查询参数覆盖，例如 `?connect_timeout=10`。这个参数限制建立连接的等待时间，不是 SQL 执行超时。

独立 PostgreSQL 停库、启库测试可通过 `SAKURAMEDIA_TEST_PG_BIN=/usr/lib/postgresql/15/bin .venv/bin/pytest -q -n0 --no-testmon tests/common/test_database_restart.py` 运行；测试会自行创建并清理临时数据库实例，不操作已有实例。

## 致谢

番号解析的实现与回归测试参考了以下开源项目，在此表示感谢：[JavSP](https://github.com/Yuukiy/JavSP)、[JavBoss](https://github.com/Solr159/JavBoss)、[Emby.Plugins.JavScraper](https://github.com/JavScraper/Emby.Plugins.JavScraper)、[JAVOneStop](https://github.com/ddd354/JAVOneStop)、[metatube-sdk-go](https://github.com/metatube-community/metatube-sdk-go)、[Movie_Data_Capture](https://github.com/mvdctop/Movie_Data_Capture)、[mdcz](https://github.com/ShotHeadman/mdcz)、[javinizer-go](https://github.com/javinizer/javinizer-go)。

## 风险声明

* 本项目仅用于技术交流
* 不提供任何影片资源以及资源下载链接等.
* 用户使用本工具产生的一切后果由使用者自行承担，和作者无关.
* 用户在使用本软件前，请用户了解并遵守当地法律法规，如果本软件使用过程中存在违反当地法律法规的行为，请勿使用该软件.
* 禁止将本软件用于商业用途
* 若用户不同意上述条款任意一条，请勿使用本软件

如有侵权，请邮箱联系 tinyping@protonmail.com

## 视频片段独立存储

`storage.clips_backend` 单独控制生成的 MP4 片段，图片/缩略图仍使用 `storage.backend`：

| `clips_backend` | 片段存储位置 |
| --- | --- |
| `"inherit"`（默认） | 跟随 `storage.backend`，保持升级前行为 |
| `"local"` | 使用 `media.media_clip_root_path`，默认 `/data/media-clips` |
| `"webdav"` | 使用现有 WebDAV 端点/凭据及 `clips` 命名空间 |

例如，图片走 WebDAV、视频片段落本地：

```toml
[storage]
backend = "webdav"
clips_backend = "local"
# webdav_base_url、username、password 等沿用已有配置

[media]
media_clip_root_path = "/data/media-clips"
```

也支持反向组合：`backend = "local"`、`clips_backend = "webdav"`；此时仍须配置有效的 `storage.webdav_base_url`。图片和片段使用同一组 WebDAV 配置，不支持为片段另设端点或账号。

环境变量可用：

```text
STORAGE__CLIPS_BACKEND=local
MEDIA__MEDIA_CLIP_ROOT_PATH=/data/media-clips
```

标准前缀形式同样支持 `SAKURAMEDIA_STORAGE__CLIPS_BACKEND` 和 `SAKURAMEDIA_MEDIA__MEDIA_CLIP_ROOT_PATH`。环境变量覆盖 TOML；若同时设置两套环境变量，上面的无前缀部署变量优先。

本地目录是**容器内路径**，需要持久卷且运行用户有写权限。例如将宿主机 `/srv/sakura/clips` 挂载到 `/data/media-clips`；自定义位置如 `/mnt/clips` 时，应同时修改 `media_clip_root_path` 并配置对应挂载。`thumbnail_staging_root_path` 是缩略图暂存目录，不是 MP4 片段的最终目录。

修改配置后重启 API/APS。`storage` 节不能经通用配置 API 修改；本地目录字段属于 `media` 节，可通过配置 API 保存或直接修改 TOML/环境变量。本次无需数据库迁移。

**切换片段后端或保存目录不会自动迁移旧文件，也不回退读取旧位置。** 切换前应备份数据库、暂停相关服务，将文件按原相对 key 迁移到新位置并核验，再启用配置；否则列表/访问可能把目标位置明确缺失的片段作为无效记录清理。片段仍是独立 MP4，不使用 ZIP，也没有新增上传断点续传能力。

## 媒体缩略图存储

新生成的电影和视频缩略图使用统一资产存储：`storage.backend = "local"` 时保存到本地，配置为 `"webdav"` 时保存到 WebDAV 的 `assets` 命名空间。缩略图尺寸读取、图片访问、缩略图搜索索引、推荐读取和媒体删除均使用同一存储配置。

切换存储后不会自动迁移历史本地缩略图，也不会回退读取本地副本；已有缩略图记录不会因此自动重新生成。

### 媒体封面 URL

外部来源的影片封面直接保存、返回原始 HTTP(S) URL（含查询参数），不下载或上传 WebDAV；元数据搜索候选同样直接使用原封面 URL。插件交付的本地封面、生成的薄封面与视频首帧封面保存在 `media.import_image_root_path` 下的 `local-covers/`，通过签名图片 URL 访问，不进入 WebDAV 发布队列。使用 WebDAV 存放缩略图时，本地封面目录仍需持久化。

### 缩略图 ZIP 存储

- **本地存储**：新缩略图批次写入独立的 `thumbnails/<批次UUID>.zip`（ZIP_STORED），数据库保留逻辑图片路径 `thumbnails/<批次UUID>/<时间点>.webp`。内部影片图片按影片归入 `assets.zip`；外部 HTTP(S) 图片引用仍直接返回，不下载打包。
- **WebDAV**：同样以 `thumbnails/<批次UUID>.zip` 整包上传（ZIP_STORED），不再逐张上传缩略图；读取逻辑图片路径时从远端 ZIP 提取对应条目，不回退读取本地 ZIP/图片缓存。
- 本地读取兼容已有 `thumbnails.zip`、新批次 ZIP 和历史单文件。升级前尚未完成的暂存批次会转为 ZIP 续传，保留原 generation 和图片路径，不重新切片；历史已发布的单图仍可读取。
- `media_thumbnail_pack_backfill`、`movie_asset_pack_backfill` 为本地存储的手动维护任务；WebDAV 模式在扫描和文件操作前明确拒绝，错误为 `image_pack_requires_local_storage`。
- 图片响应只返回 `id`、`origin`，不再返回 `small`、`medium`、`large`；内部图片继续使用签名 URL，缓存寿命不超过签名有效期。

此次上游同步包含数据库迁移（演员合并、孤立视频清理和图片派生尺寸列移除）。升级前请备份数据库；裸机部署使用 `python -m src.start.commands migrate`，容器按既有启动流程执行迁移。不要在迁移期间混跑旧版应用；图片字段变化需要配套前端版本。

### 缩略图本地 ZIP 暂存与重试

缩略图先生成并打包为持久化本地 ZIP，校验通过后再上传统一资产存储。每张图片的大小、SHA-256、批次存储格式、固定对象 key 所需信息及上传成功状态通过原子清单写入本地；远端凭据不保存到清单。ZIP 保存在批次目录的 `thumbnails.zip`，重试优先复用此文件，无需原始图片或再次切片。

```toml
[media]
thumbnail_staging_root_path = "/data/cache/thumbnail-staging"
```

也可设置 `SAKURAMEDIA_MEDIA__THUMBNAIL_STAGING_ROOT_PATH`。**此目录必须挂持久卷，并留出足够磁盘空间**；不要放在随容器重建丢失的临时盘中，也不要公开为图片目录。生成目录可能保留提供方输出及待上传副本，因此峰值占用可能高于最终图片总大小。

- WebDAV 直接 PUT 到 `thumbnails/<批次UUID>.zip`，不使用临时对象或 MOVE。上传后完整回读远端 ZIP，核对文件长度和 SHA-256；只有全部一致才确认上传成功。
- 上传或校验失败保留本地 ZIP，并生成该批次的系统失败通知；不删除远端对象，也不在上传内部自动重试。人工重试直接将同一个本地 ZIP 重传到同一 generation key，允许替换上次上传留下的不完整文件。
- 上传成功状态落盘后立即删除本地 ZIP，再统一创建数据库记录。数据库写入失败保留清单中的成功状态，下次只重试入库，不再次上传或切片；提交响应丢失时核对数据库批次，避免重复记录。
- 明确提交成功后清理剩余本地批次文件和清单。清理失败不撤销已完成的任务；之后对该媒体的生成请求会核对已提交记录并重试清理。
- 进程重启后可以恢复 ready 批次，但仍遵守现有任务退避和终态策略；终态失败需要人工重试，不会无限自动重试。尚未完成生成/校验的目录不是可恢复上传批次。
- 源媒体身份或存储目标变化时使用隔离批次；损坏清单或本地 ZIP 损坏会明确失败并保留现场。有效 ZIP 已保存时无需依赖原始图片；旧批次尚无 ZIP 时仍需完整图片用于打包。失败、未完成或旧身份批次不会自动过期删除，需要在停止相关写入并确认无需恢复后人工维护。

本地 ZIP 同样遵守“确认整批入库后才清理暂存”的规则；入库失败复用已发布的包，不重新切片。

同机多进程通过媒体会话锁和文件锁串行处理同一媒体。多实例跨主机续传需要共享支持文件锁的同一缓存卷；私有磁盘之间不会同步暂存文件。暂存不是已发布资源的长期备份，也不会自动迁移历史资源。


### WebDAV 上传与失败处理

缩略图 ZIP 使用上文的直接 PUT、完整回读校验与本地文件重试流程。其它图片、字幕（未单独选择本地存储时）和媒体片段先写本次操作独有的临时对象，校验后再用 WebDAV `MOVE` 发布。不可变图片保留完整 SHA-256 校验，直接流式计算，不再为校验下载一份磁盘副本；普通上传校验长度和文件类型。需要“不覆盖”时使用 `Overwrite: F`，不以“上传前检查不存在”代替原子创建。服务端必须正确实现这些资源的 `MOVE` 和覆盖条件。

- **确定失败**：认证/权限错误、空间不足、冲突和内容损坏不会盲目重试。短暂网络故障及部分 5xx 有限退避；429/503 的 `Retry-After` 单次最多等待 60 秒，并受剩余预算约束。
- **结果不明**：MOVE 响应丢失或对象暂不可见时，通过远端长度和完整内容核对确认结果；无法确认则返回 `storage_publication_unknown`，保留可能已经发布的对象。超时 PUT 的后续尝试使用另一个临时 key，避免旧请求晚到后破坏重试结果。
- **安全补偿**：回执区分本次创建、复用和已发布但所有权未证实；不会因为一次任务失败就删除复用的文件。数据库提交或引用状态不明时保留对象，清理错误不掩盖最初的上传错误。
- **业务竞争**：缩略图的新批次使用独立 generation key，历史 key 继续可读；片段生成、删除和失效清理使用一致的媒体锁；同影片字幕分配和同步通过 PostgreSQL 会话锁协调。文件发布不等于数据库事务，不能保证两者同时原子提交。

### 上传配置与并发范围

```toml
[storage]
webdav_publication_max_workers = 4
webdav_publication_timeout_seconds = 600
webdav_upload_retry_seconds = [0.1, 0.25, 0.5, 1.0]
webdav_final_visibility_retry_seconds = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]
upload_chunk_size = 1048576
download_chunk_size = 1048576
```

`webdav_publication_max_workers` 限制**每个进程**中同一 WebDAV 端点/账号的发布网络请求，assets 与 clips 共享额度，不是整个部署的全局限额。等待同名对象锁、计算本地哈希或退避时不占用网络额度；批量上传的在途任务也有上限。多个 API/worker 进程的总请求数可能更高，部署时需按进程数和服务端容量配置。

`webdav_publication_timeout_seconds` 是一次发布调用的时间预算，包含排队、校验和重试；现有 connect/read/write/pool 分项超时继续生效。重试间隔允许空列表以关闭该阶段重试，最多 16 项，每项为 0～60 秒的有限数值。上述新配置支持 `SAKURAMEDIA_STORAGE__...` 环境变量；部署用的 `STORAGE__WEBDAV_PUBLICATION_TIMEOUT_SECONDS`、`STORAGE__WEBDAV_UPLOAD_RETRY_SECONDS` 和 `STORAGE__WEBDAV_FINAL_VISIBILITY_RETRY_SECONDS` 也可直接使用，间隔列表用 JSON 字符串。

已确认的目录缓存 10 分钟，缓存失效会有限修复；目录确认使用 Depth: 0，避免枚举全部子对象。连接池会复用，配置重载后旧后端在已有上传/响应流结束后关闭。

### 恢复与临时文件维护

通用存储协议只保证**单次调用内的有限重试**。媒体缩略图额外提供上述持久批次恢复；其他资源没有因此获得持久上传队列。重试耗尽后由现有任务策略或人工重试接手，也不承诺自动清除所有孤儿对象。同一后端实例对相同内容的未知结果重试会先核对远端，不以一条存在性缓存作为成功依据。

成功上传不再顺便扫描目录删除历史临时文件。进程崩溃、请求超时或结果不明时，可能留下 `.文件名.uploading-<uuid>` 对象；日志会记录 key、操作和阶段，供排查。

`cleanup_expired_uploads(prefix, older_than=..., max_deletes=..., uploads_paused=True)` 仅作为显式维护入口。调用前必须暂停**所有进程和实例**的相关写入，并确认没有仍在执行的远端请求；`uploads_paused=True` 是操作者声明，不会自动停止其他实例。维护只针对指定目录、符合临时文件命名规则且超过截止时间的对象，不会扫描整个存储或删除 final。原 `webdav_temp_cleanup_*` 配置保留兼容，但不再触发上传后的自动清理；维护调用需显式提供目录、时间和数量上限。
