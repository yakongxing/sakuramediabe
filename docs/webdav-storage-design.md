# SakuraMediaBE 媒体资产迁移到 WebDAV：修改设计方案

> 状态：设计稿（含代码核实评审修正）；只描述后续代码改造，不包含本次提交中的业务代码变更。  
> 范围：图片、封面、剧照、缩略图、字幕、媒体片段和由本地媒体 Provider 管理的导入媒体文件。PostgreSQL 元数据、`config.toml` 文件本身、plugins、日志、GFriends 缓存均保持原存储方式。  
> 核心结论：不能把 WebDAV URL 填入现有 `*_root_path`。现有代码广泛依赖 `pathlib`、`FileResponse`、`os.replace` 和 ffmpeg/PyAV 的本地文件语义，必须引入“逻辑对象 key + backend”抽象，并为外部处理器提供受控的本地工作区。  
> 依赖核实：`webdav4` 0.11.0 要求 `httpx>=0.20,<1`，项目锁定 `httpx==0.28.1`，兼容；唯一新增传递依赖为 `python-dateutil`（当前未在依赖树中）。不需安装 `fsspec`/`http2` extra，Dockerfile 无需 davfs2/FUSE/特权模式。

## 1. 现状与边界

### 1.1 资产命名与数据库字段

`src/common/media_paths.py` 以 `settings.media.import_image_root_path`（默认 `/data/cache/assets`）为图片资产根。影片目录为：

```text
movies/<sha1(normalized_movie_number)[:2]>/<normalized_movie_number>/
├── cover.<ext>
├── thin-cover.<ext>
├── plot-<index>.<ext>
├── media/<media_id>/thumbnails/<offset>.webp
└── subtitles/<movie_number>-<N>.<srt|ass|ssa|vtt>

actors/<javdb_id>.<ext>
videos/<video_item_id>/cover/0.webp
```

图片表 `Image.origin/small/medium/large` 保存相对图片根的 POSIX 路径，这是可直接沿用的对象 key。`MediaClip.file_path` 也保存相对 `media_clip_root_path`（默认 `/data/media-clips`）的 `<movie_number|_unknown>/<clip_id>.mp4`。

字幕不同：`Subtitle.file_path` 当前保存标准字幕目录内的**绝对本地路径**，并由 `ensure_movie_subtitle_path()` 用 `Path.resolve()/relative_to()` 校验。迁移时应将其规范为逻辑 key（例如 `movies/ab/ABP-001/subtitles/ABP-001-1.srt`）；迁移过渡期必须能解析旧绝对路径。该字段仍是 PostgreSQL 元数据，不改变数据库介质，只改变路径值的语义。若坚持完全不更新存量字段，则每次读都要永久维护绝对路径转换，不推荐。

导入媒体原片不是由 `import_image_root_path` 管理。`MediaImportService` 把移动、删除、播放、探测、缩略图和切片委托给插件 `StorageProvider`；`Media.media_ref` 保存 provider opaque ref。因此“导入媒体文件迁移”应落在 bundled local provider 的实现及配置里，宿主不能把 opaque ref 当成本地路径。115 等远端 Provider 已有自己的存储语义，不应二次搬到本项目 WebDAV。

### 1.2 已核实的数据流

| 文件/函数 | 当前文件行为 | 当前路径/数据源 | WebDAV 影响 |
|---|---|---|---|
| `common/media_paths.py`：`media_image_root_path()`、`movie_asset_dir()`、`movie_subtitle_dir()` | 构造绝对 `Path`；sha1 两位分片 | 图片根 | 保留纯 key 生成；绝对路径构造仅归本地 backend |
| `common/subtitle_paths.py`：`ensure_movie_subtitle_path()` | `resolve/relative_to` 校验绝对路径 | `Subtitle.file_path` | 改为 key 校验和旧绝对路径适配 |
| `common/file_signatures.py`：`resolve_image_file_path()`、`resolve_subtitle_file_path()`、`media_clip_root_path()` | 签名后解析本地绝对路径 | 图片、字幕、片段 | 签名 payload/URL 可保持；解析结果改为 `(namespace, key)` |
| `catalog/movie_image_service.py`：任务构建、下载、薄封面、刷新 finalize | `mkdir/exists/write_bytes`；OpenCV/Pillow 本地读；刷新用 `os.replace` | `actors/`、`movies/` | 网络下载到本地临时文件，校验/变换后 `put`；批量发布需补偿 |
| `catalog/movie_subtitle_service.py`：sync/list/discover | 扫本地目录并剔除丢失文件的 DB 行 | 影片 `subtitles/` | 用 `list/stat`；WebDAV 短暂故障不得当成文件不存在并删 DB |
| `catalog/subtitle_asset_service.py`：allocate/import/write/hash | 扫目录分配序号；`copy2`；临时文件 + `os.replace`；SHA-256 | 影片 `subtitles/` | 分配锁、条件创建；本地 source 上传；远端流式 hash |
| `catalog/image_cleanup_service.py`：`delete_obsolete_image_files()` | 删除未引用 Image 对应文件 | 图片根 + origin | backend delete；404 幂等，网络错误可重试且不得伪装成功 |
| `playback/thumbnails/artifacts.py`：reset/validate/persist/read_dimensions | 清目录；Pillow 校验；`Path.replace` 后 DB 事务；失败回删 | `movies/.../media/<id>/thumbnails` | workspace 中校验；先上传临时 key，再发布；DB/对象补偿 |
| `playback/thumbnails/task_service.py`：`_generate_artifacts()` | provider 把产物写入 `TemporaryDirectory` | 系统临时目录 | 已符合本地工作区模型；只改 artifact persist |
| `playback/media_clip_service.py`：create/validate/delete/stream | provider 在临时目录产片；`move` 到片段根；`stat/unlink` | 片段根 | 上传 workspace 产物；用 `stat` 校验；流式端点走 backend range |
| `playback/media_metadata_probe_service.py`：`probe_file/probe_source` | 本地 `stat` 后 PyAV；也支持 seekable file-like | provider source | backend 资产先 materialize；provider 能提供 seekable range reader 时可保持其优化 |
| `transfers/imports/import_service.py`：`import_from_source()` | 调 provider 的 scan/stage/finalize/abort；宿主不直接搬文件 | Provider 管理根 | 修改 bundled local provider；保持 stage receipt/补偿协议 |
| `videos/video_cover_service.py`：`generate_cover()` | PyAV 读路径或 seekable stream；Pillow 写本地 WebP；DB 入 Image | `videos/<id>/cover/0.webp` | workspace 生成后上传，成功后才写 DB；失败仍为非致命 |
| `api/routers/files/images.py` | `FileResponse` | 图片根 | 小文件由 backend 流式 GET；保留签名校验与 MIME |
| `api/routers/files/subtitles.py` | `FileResponse` | `Subtitle.file_path` | backend GET；明确 UTF-8 响应策略，避免整文件无界入内存 |
| `api/routers/playback/media_clips.py` | `require_existing_file` + 本地 Range | 片段根 | backend `stat/open_range` |
| `api/routers/playback/media.py` | 播放完全委托 Provider，支持 proxy/redirect | `Media.media_ref` | local WebDAV provider 应只支持 proxy；不要泄露带凭据 URL |
| `api/routers/discovery/image_search.py` | 仅读取用户上传 bytes，自己不读资产盘 | HTTP upload | 路由无需改；但后台索引服务读取缩略图/剧照处要接 backend |

清单之外但必须纳入调用面扫描的文件包括 `api/routers/_utils.py`（`require_existing_file/stream_local_file_response`）、`common/file_signatures.py`、图片搜索的 `image_search_index_service.py`、`movie_plot_image_search_service.py` 及其索引任务。这些服务最终会将 `Image.origin` 解析为本地路径或读取图片内容，必须统一通过资产 backend，否则以图搜图上传接口虽正常，后台索引会失败。

## 2. 目标架构

### 2.1 两层职责，不混用 Provider 与资产 backend

```text
API / service
  ├─ AssetStorageRouter（宿主生成资产）
  │    ├─ LocalStorageBackend
  │    └─ WebDAVStorageBackend
  │
  ├─ LocalWorkspaceManager（ffmpeg / PyAV / OpenCV / Pillow）
  │    └─ /data/cache/storage-workspaces（仍为本地、可清理、非权威数据）
  │
  └─ StorageProvider（媒体库插件的业务协议，现有）
       ├─ bundled local provider → 内部使用 StorageBackend/WebDAV
       └─ 115/其他 provider → 保持原实现
```

`StorageBackend` 是通用对象存取能力；`StorageProvider` 是媒体库领域协议（导入阶段、receipt、播放、缩略图、切片等）。后者可依赖前者，但不能用前者替代全部 provider 业务语义。

### 2.2 key、namespace 与映射

所有 backend 接收 `StorageKey = str`：非空、相对 POSIX 路径，不允许前导 `/`、空段、`.`、`..`、反斜线、NUL 或 URL 编码后二次穿越。业务层不持有 WebDAV URL，不拼用户名密码。

| namespace | 逻辑 key 示例 | local 模式物理位置 | WebDAV 模式物理位置 |
|---|---|---|---|
| `assets` | `movies/ab/ABP-001/cover.jpg` | `<import_image_root_path>/<key>` | `<root_prefix>/assets/<key>` |
| `assets` | `movies/ab/ABP-001/subtitles/ABP-001-1.srt` | 同上 | `<root_prefix>/assets/<key>` |
| `clips` | `ABP-001/123.mp4` | `<media_clip_root_path>/<key>` | `<root_prefix>/clips/<key>` |
| `media` | `jav/ABP-001/source.mp4` | local provider 的 managed root | `<root_prefix>/media/<key>`（仅 local provider） |
| `workspace` | 随机任务目录 | `storage_temp_root_path/<uuid>` | **永不映射到 WebDAV** |

配置、plugins、日志和 GFriends cache 不注册 namespace，继续直接使用本地路径。不要把整个 `/data` 代理到 WebDAV。

### 2.3 接口建议

建议新增 `src/storage/`，让 backend API 保持同步（现有 service/任务多为同步）；播放端的网络流单独提供 async 实现，避免在 FastAPI event loop 内运行同步 WebDAV I/O。

```python
class StorageBackend(Protocol):
    def put_file(self, key: str, source: Path, *, overwrite: bool = True,
                 expected_sha256: str | None = None) -> ObjectStat: ...
    def put_stream(self, key: str, source: BinaryIO, *, size: int | None,
                   overwrite: bool = True) -> ObjectStat: ...
    def get_file(self, key: str, destination: Path) -> ObjectStat: ...
    def open(self, key: str) -> ContextManager[BinaryIO]: ...
    def stat(self, key: str) -> ObjectStat: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str, *, missing_ok: bool = True) -> None: ...
    def list(self, prefix: str, *, recursive: bool = False) -> Iterable[ObjectStat]: ...
    def mkdir(self, key: str, *, parents: bool = True) -> None: ...
    def rename(self, source: str, destination: str, *, overwrite: bool = False) -> None: ...

class AsyncRangeReader(Protocol):
    async def stat(self, key: str) -> ObjectStat: ...
    def open_range(self, key: str, start: int, end: int,
                   *, chunk_size: int) -> AsyncIterator[bytes]: ...
```

`ObjectStat` 至少包含 `key/size/is_file/etag/last_modified/checksum`。`exists` 只把确定的 404 映射为 `False`；401/403/超时/5xx 必须抛出分类异常。建议异常为 `StorageNotFound`、`StorageConflict`、`StorageAuthError`、`StorageUnavailable(retryable)`、`StorageIntegrityError`。业务层据此区分“不存在”与“存储暂不可用”，防止错误清库。

`rename` 的接口语义应为“调用方要求最终只暴露 destination”，但 capability 中声明 `atomic_rename: bool`。WebDAV 实现优先 `MOVE`；不保证服务器的 MOVE、覆盖和锁语义一致，业务算法不能假设其跨服务器原子性。

### 2.4 backend 构造与生命周期

- `storage_factory.py` 在进程启动时按配置为 `assets/clips/media` 构建 backend，复用长生命周期连接池；不要每次请求创建 HTTP client。
- 同步 job 使用 `webdav4.Client`；Range proxy 使用专用 `httpx.AsyncClient`，因为 Range 是普通认证 GET，且需要精确保留响应状态/头部。
- API 依赖注入取得 namespace storage；后台 APScheduler 任务从同一 factory 获取线程安全实例。若底层 client 的线程安全未获保证，则以每线程 client + 共享 transport 限流。
- 启动只做低成本 capability/认证探测（例如根目录 `PROPFIND Depth: 0`），失败时 readiness 不通过或把资产功能标记 unavailable；不得静默回落到空的本地目录。

## 3. WebDAV backend 设计

### 3.1 库选择

推荐 `webdav4`，固定经集成测试验证的版本范围后生成 lock（设计时可从 `webdav4>=0.11,<0.12` 起步验证）。它支持 Python 3.10、基于 HTTPX，并提供 `exists/ls/info/upload_file/upload_fileobj/download_file/download_fileobj/copy/move` 等高级操作。项目已经使用 HTTPX，因此认证、TLS、proxy、连接池和异常观测更容易统一。官方参考：[PyPI](https://pypi.org/project/webdav4/)；[Client API](https://skshetry.github.io/webdav4/reference/client.html)。

不建议把 `fsspec` 作为业务接口：它会诱导调用方继续假设 POSIX 文件系统语义，而且 Range 状态码和 WebDAV MOVE/ETag 等能力仍需显式处理。也不建议较老的 `webdavclient3` 作为首选。`webdav4` 负责 PROPFIND/MKCOL/PUT/MOVE/DELETE，精确 Range proxy 直接复用 HTTPX。

### 3.2 写入与“发布”协议

单对象写入遵循：

1. 在本地 workspace 完成下载、转换、大小及 SHA-256 校验。
2. 上传到同目录隐藏临时 key：`<final>.uploading.<operation_uuid>`。
3. 对临时对象 `stat`，核验 size；服务器 ETag 不能默认等于 MD5，只有服务明确保证时才能作哈希。
4. 若支持可靠 MOVE，`MOVE temp → final`；否则 `COPY temp → final` 后 `stat/校验`，最后删除 temp。若服务连 COPY 都不可靠，则直接 PUT final，但 DB 只能在完整 PUT 成功后引用它。
5. DB 事务写/更新元数据。事务失败则尝试删除新对象；删除失败记录 orphan，交给清理任务。

WebDAV 不具备对象存储式通用条件写，不应声称能提供跨 DB/文件的真正事务。实现目标是：读取者永远不访问临时 key；最终 key 只在上传完成后进入 DB；失败可重试且有补偿。图片刷新若覆盖同 key，MOVE/COPY 发布窗口仍可能让旧/新读者看到不同版本；若要求强版本一致，应使用内容版本 key（例如加 SHA-256）再切 DB 指针，而不是原地覆盖。

### 3.3 目录与并发

- `mkdir(parents=True)` 逐级 MKCOL，405（已存在）幂等处理；缓存已建目录只是优化，不能作为事实源。
- 限制并发上传/下载和连接数，建议初始值：元数据小文件 8，视频大文件 2，可配置。
- 所有网络操作设 connect/read/write/pool 分项超时；幂等 GET/HEAD/PROPFIND 可自动重试，PUT/MOVE 仅带 operation key 并先核验远端状态后重试。
- 记录 method、namespace、key 的哈希/安全截断、status、bytes、duration、retry；日志绝不含 password、Authorization 或完整含凭据 URL。

## 4. 按模块改造点与风险

### 4.1 路径与签名公共层

**`src/common/media_paths.py`**

- 新增纯函数 `movie_asset_key/movie_subtitle_prefix/media_thumbnail_prefix/video_cover_key`，返回 `PurePosixPath` 或规范字符串。
- 保留现有本地 path helper 供 `LocalStorageBackend` 和兼容迁移工具使用，但业务 service 不再调用。
- sha1 分片、番号归一化和既有相对目录布局不变，避免迁移时重排所有对象。

**`src/common/subtitle_paths.py`、`src/common/file_signatures.py`**

- 抽出单一 `normalize_storage_key()`；签名继续签规范 key，避免 URL 失效。
- `resolve_*` 返回 `StorageObjectRef(namespace, key)`，不返回 `Path`。
- 旧字幕绝对路径若位于旧 `import_image_root_path` 下，转换为 relative key；其它绝对路径拒绝。迁移完成后 DB 回填 key。
- `resolve_subtitle_file_path()` 当前还由 subtitle id 查 DB；该处需做双格式解析，不能让 WebDAV URL进入 DB。

风险：签名规范化若变化会使存量签名 URL 失效。验收必须覆盖空格、中文、`%2F`、双编码和路径穿越。

### 4.2 图片、封面和剧照

**`movie_image_service.py`**

- `ImagePersistTask.absolute_path` 改为 `storage_key`；本地处理另外携带 `workspace_path`。
- 下载外站图片始终先流式写 workspace 临时文件，限制最大 bytes，执行 Pillow/OpenCV 校验及薄封面切割，再 `assets.put_file()`。
- `download_image_tasks()` 的 exists、`persist_prepared_images()` 的落地检查改为 backend `stat`。并发线程使用限流。
- `download_image_tasks_to_temporary_files()` 不再把 temp 建在图片根下，而用 `LocalWorkspaceManager` 的本地 temp root。
- `finalize_prepared_image_files()` 从逐个 `os.replace` 改为逐个发布 + 已发布 key 列表补偿；DB 切换前全部上传完成。
- `resolve_thin_cover_from_existing_movie()` 将封面/候选剧照 materialize 到 workspace 后交给 Pillow/OpenCV。
- `_download_image()` 只负责 HTTP → workspace，不能直接写权威 backend。

风险：刷新涉及多张图片，不可能与 PostgreSQL 构成原子事务。使用 operation id、发布清单和 orphan 清理；封面仍按现有语义为关键失败，剧照/头像可跳过。

**`video_cover_service.py`**

- 输入若为 provider seekable stream可直接给 PyAV；否则由 provider/backend materialize。
- WebP 输出到 workspace，校验后上传 `videos/<id>/cover/0.webp`；上传成功后在 DB 事务创建 `Image` 并回写 `VideoItem`。
- DB 失败删除该 operation 新对象；保持“封面失败不阻断媒体导入”的现有产品语义。

### 4.3 字幕

**`subtitle_asset_service.py`**

- `allocate_target_path()` 返回 key；`import_file()` 从本地源 `put_file`，`write_content()` 用 workspace 或 `put_stream`。
- SHA-256 去重保留。已有远端字幕通过 `open()` 分块 hash；可把 hash/size 放入存储 sidecar 或数据库扩展作为后续优化，但首期不要求 schema 变化。
- `register_existing_file()` 不再接受任意本地绝对路径，只接受经校验 key；插件入口仍只能写受控字幕 prefix。

**`movie_subtitle_service.py`**

- `_discover_subtitle_paths()` 改成对影片字幕 prefix 做 depth=1 list；扩展名行为需统一：当前发现逻辑只接纳 `.srt`，而写入允许 `.ass/.ssa/.vtt`。迁移时应明确修正为四种扩展名，否则非 srt 上传后不会被同步发现。
- sync 遇 `StorageUnavailable/AuthError` 整次失败并保留 DB 行；只有成功完成 list/stat 且确认 404 才删除失效记录。
- 列表接口用 key basename，不再构造 `Path`。

**并发序号分配**

当前 `allocate_next_movie_subtitle_path()` 扫目录最大 N，仅用 `reserved_names` 解决同一批冲突，跨线程/进程会竞争。

**首选方案：复用已有唯一约束 + 行锁，不引入新锁原语。**

`Subtitle` 模型（`src/model/catalog/movies.py`）已经声明 `indexes = ((("movie", "file_path"), True),)`，即 `(movie, file_path)` 唯一约束**现成可用**。项目里 `SELECT ... FOR UPDATE` 已在 7 处使用（`task_queue_service.py`、`import_task_service.py`、`catalog_import_service.py` 等），而 advisory lock 一处都没有。因此贴合现有代码风格的做法是：

1. 事务内 `Movie.select().where(Movie.id == movie_id).for_update()` 锁住影片行，覆盖"list → 选 N → 上传 → Subtitle insert"全过程。单账号场景下 API + scheduler 多进程共用同一 PostgreSQL，行锁足够串行化同一影片的分配。
2. 锁内列出当前 prefix 与 DB 已有 `file_path`，取二者最大 N + 1。
3. `Subtitle.create()` 依赖唯一约束兜底：若并发绕过锁（例如未来出现跨库实例）导致重复，insert 会抛 `IntegrityError`，捕获后重新 list/分配并重试（建议上限 3 次）。这是**正确性的最后防线**，不能省。
4. 上传时使用 `overwrite=False`；若服务支持 `If-None-Match: *` 则条件 PUT。冲突则重新分配。
5. 若 WebDAV 忽略条件头，用随机临时 key 上传后，在事务内再次确认 final 不存在才发布；最坏情况是操作失败重试，绝不覆盖既有字幕。

**不推荐** PostgreSQL advisory transaction lock 作为首选：它能解决同样的问题，但引入了项目中不存在的锁原语，且语义上弱于"行锁 + 唯一约束"这一组合（后者即使锁失效仍不会写坏数据）。仅当出现无法用行锁覆盖的跨表分配场景时再考虑。

`Subtitle.file_path` 迁移为 key 后，唯一约束的语义从"绝对路径唯一"变为"key 唯一"，约束本身无需改动，但迁移脚本必须保证转换后不产生重复 key（同一影片的两条旧记录若指向同一文件的不同绝对路径写法，转换后会撞约束——迁移前清点必须检出这类冲突）。

### 4.4 缩略图与图片搜索

**`thumbnails/task_service.py`** 已用 `TemporaryDirectory` 接 provider 输出，无需改变生成协议；配置它使用统一 workspace root并加容量配额。

**`thumbnails/artifacts.py`**

- `validate_artifact()` 继续只读 workspace，保留 WEBP、路径逃逸、尺寸校验。
- `persist()` 对所有合法产物先上传 `.uploading.<op>`，发布为 final key，完成后再在 DB 事务创建 `Image/MediaThumbnail`。
- 失败删除本 operation 已发布对象，不要清理其它运行创建的 key。
- `reset_directory()` 不能先无条件清空远端目录；应先生成新集合并提交 DB，再按 DB 引用差集删除旧对象。
- `read_dimensions()` 把单张图下载到有 size 上限的临时文件或读取有限 bytes 给 Pillow；可短 TTL 缓存维度。

**图片搜索服务**

- `image_search.py` 的上传路由不改。
- `image_search_index_service.py`、`movie_plot_image_search_service.py` 及读取 `Image.origin` 的索引路径改用 `assets.get/open`；批处理设置 bounded concurrency 和本地 LRU 临时缓存。
- WebDAV 故障标记任务 retryable，不能将图片标记为永久缺失；Qdrant 元数据和 PostgreSQL 状态保持原位置。

### 4.5 片段

**`media_clip_service.py`**

- provider `create_clip()` 仍在 workspace 产生 mp4；本服务校验扩展、size、probe 后上传 `clips` backend。
- `MediaClip.file_path` 保持相对 key；`_has_valid_artifact` 用 `stat.size`；`_unlink_clip_file` 用幂等 delete。
- 先发布文件再更新占位 DB 行；DB 保存失败补偿删除。失效扫描遇 WebDAV 网络错误时不要删除 clip DB 行。
- `stream_file_path()` 改为 `stream_object_ref()`，router 进入统一 backend range response。

### 4.6 媒体原片导入与 Provider

**`transfers/imports/import_service.py`** 的总体编排不应退化为直接调用宿主 backend。需要：

- bundled local provider 新增 `storage_mode=local|webdav`，或注入 `media` namespace backend。
- `stage_import_file()` 将 source 流到 `media/<placement.relative_path>.staging.<operation_key>`，校验 source.size/哈希（若 provider 能提供），返回 receipt 包含 staging/final key、size、source disposition 和 operation id。
- 宿主创建 Media 并提交后，`finalize_import()` 发布 staging → final，再按 `delete_after_commit` 删除 source。重复 finalize 必须幂等：final 存在且校验一致即成功；staging/final 都无则明确报错。
- `abort_import()` 仅删本 operation staging；`delete_media()` 删除 media_ref 对应对象；`scan_managed_media_ref_keys()` list `media/`。
- `Media.media_ref` 继续是 provider opaque ref，建议保存相对 key及版本，不保存 WebDAV URL或凭据。
- `handle_playback()` 复用本文 Range proxy；local-WebDAV provider 不提供重定向，因为预签名 WebDAV URL通常不存在，带 Basic Auth URL更不可接受。
- 115 等已有远端 provider 不变。若导入源和目标均为同一 WebDAV 服务器，可在能力探测后用 server-side COPY/MOVE 优化，但不是正确性的前提。

这里的代码在打包的 bundled provider artifact 中，而非当前 `src/service/transfers/imports/import_service.py` 内；实施时必须同时修改 provider 源仓库/构建产物和 `docker/backend/package_bundled_providers.py` 的打包流程。否则“图片资产已迁移”不代表导入媒体原片已迁移。

## 5. 流式播放与 HTTP Range

### 5.1 推荐：应用鉴权后代理透传 Range

推荐链路：

```text
Browser --Range + signed Sakura URL--> FastAPI
FastAPI --Range + server-held WebDAV auth--> WebDAV
FastAPI --stream chunks/status/selected headers--> Browser
```

理由：视频/片段体积大，整体下载会增加首帧延迟、临时盘需求和双倍网络流量；播放器 seek 会触发多个离散 Range，整体缓存命中前代价尤其高。应用代理还能保留现有签名 URL、安全边界和审计，不暴露 WebDAV 凭据。

实现要求：

- 复用 `_get_range_header()` 的单 Range 解析语义，明确不支持 multipart ranges 时返回 416 或忽略为整文件，不能错误拼接。
- 先通过 `stat` 获取 size/ETag/content type；客户端无 Range 时可向 WebDAV 发普通 streaming GET，有 Range 时发精确 `Range: bytes=start-end`。
- 上游 206 必须校验 `Content-Range` 与请求一致；只向下游转发 `Content-Length/Content-Range/Accept-Ranges/ETag/Last-Modified/Content-Type` 白名单。不要转发上游 `Set-Cookie/WWW-Authenticate/Server`。
- 上游对 Range 返回 200 表示不支持或忽略 Range。对客户端 Range 请求不能把完整 200 冒充 206。小对象可受限下载后本地切片；媒体对象应返回 502 `storage_range_unsupported` 并在 readiness/capability check 预警。
- 上游 404→应用 404；401/403→502/503（服务端存储配置错误，不能误报用户无权）；超时/5xx→503。客户端断开必须关闭上游 response，停止下载。
- HEAD 不打开 body；空文件和 `bytes=-N`、`bytes=N-`、越界范围按 RFC 行为测试。保留 `content-encoding: identity`，向 WebDAV 发 `Accept-Encoding: identity`。
- 使用 `httpx.AsyncClient.stream()` 和 256 KiB～1 MiB chunk（可配置），连接池上限与 Uvicorn 并发匹配；设置背压，不在内存聚合视频。
- 若部署已有 CDN/反向代理，可缓存应用签名 URL响应；签名已按窗口对齐。缓存键必须包含 Range，且不得缓存存储 5xx。

### 5.2 为什么不把“整体下载缓存”作为默认

整体缓存适合 ffmpeg/PyAV 处理，不适合作为播放必经路径：冷启动慢、磁盘放大、并发 seek 导致重复下载，还需复杂缓存淘汰。可作为上游不支持 Range 时的**可配置小文件回退**，限制 `range_fallback_max_bytes`；视频大文件不回退。

二期可加入按 `(namespace,key,etag)` 的只读本地整文件缓存用于热点片段和重复探测。必须采用 single-flight 下载、`.partial` + 原子本地 rename、LRU/TTL/容量上限；该缓存不是权威存储，重启或清空不影响数据。

## 6. ffmpeg、PyAV、OpenCV/Pillow 本地工作区

外部处理器统一通过 `LocalWorkspaceManager`：

1. 创建 `/data/cache/storage-workspaces/<operation_type>/<uuid>/`，权限 0700。
2. 输入对象 `get_file()` 到 `<name>.partial`，校验远端 stat size（可用时再核验 SHA-256），本地原子 rename 为 input。
3. ffmpeg/PyAV/OpenCV/Pillow 只处理此本地 input/output；命令使用参数数组，禁止 shell 拼接。
4. 校验 output 类型、size、时长/尺寸，再走 backend 发布协议上传。
5. DB 提交后清理工作区；异常、超时、取消也在 `finally` 清理。

缓存/容量策略：

- 配置 `storage_temp_root_path`、`storage_temp_max_bytes`、`storage_temp_min_free_bytes`、`storage_temp_ttl_hours`；任务开始前按输入 size + 预计输出预留，空间不足返回 retryable 错误。
- 同一 `(key, etag)` 的大输入可用 single-flight 共享只读 cache，但任务输出必须隔离。缓存文件引用计数为零后才可淘汰。
- 启动及定时清理超过 TTL 且无活跃 lock/lease 的目录；PID 文件不足以判断容器重启后的活跃性，使用带更新时间的 lease。
- ffmpeg 超时沿用 `media_clip_ffmpeg_timeout_seconds`，超时 kill 进程组；远端下载和上传分别有超时。失败只清本地 workspace、临时远端 key和本 operation 新对象，不删旧 final。

现有 thumbnail provider 已输出到临时 workspace，片段 provider 也返回 `ClipArtifact`；应沿用契约。媒体原片输入下载通常发生在 bundled local-WebDAV provider 内，而不是宿主再次下载。

## 7. 原子性、一致性和清理

### 7.1 一致性规则

- **DB 引用 ⇒ final 对象应存在**：先上传/发布，再提交 DB。
- **对象存在 ⇏ DB 引用**：允许短暂 orphan，由延迟清理回收。
- 读取路径不访问 `.uploading.*`、`.staging.*`、`.trash.*`。
- DELETE 404 幂等成功；网络错误保留 DB/重试，绝不能把 unknown 当 absent。
- 覆盖操作应尽量改为 immutable/versioned key；必须覆盖时用同目录 MOVE capability，且记录旧 ETag。

### 7.2 无原子 rename 的降级

优先级：可靠同服务 MOVE → server-side COPY + 校验 + DELETE → PUT final + 校验。COPY/PUT 降级不提供真正原子可见性，所以 final key 在成功响应和 size 校验前绝不写入 DB。覆盖已有 final 时不使用非原子降级；改写到版本 key后切换 DB，旧对象延迟删除。

`image_cleanup_service` 改为 mark-and-sweep 思路：先从 DB 得到仍引用 key 集合，再 list 受管 prefix；只删除超过 grace period（建议 24 小时）的无引用 final/临时对象。直接由“删除 DB 行”同步触发的 delete仍可保留为 best-effort，但失败进入持久化重试/任务摘要。禁止在 WebDAV list 失败或分页不完整时执行 sweep。

### 7.3 可恢复操作记录

无需把文件内容放入 PostgreSQL，但建议迁移/发布 job 用数据库任务记录或本地持久 journal 保存 `operation_id, namespace, source, temp_key, final_key, expected_size, expected_sha256, state`。进程崩溃后据此 resume/finalize/abort。若不新增表，至少迁移 CLI 输出 append-only manifest；单靠日志不够回滚。

## 8. 配置、安全与部署

建议在 `Media` 之外新增独立 `Storage` Pydantic 配置段，避免把密码混进可公开返回的 media 配置：

```toml
[storage]
backend = "local"                    # local | webdav
webdav_base_url = "https://dav.example.com/remote.php/dav/files/account"
webdav_username = "account"
webdav_password = ""                 # 留空落盘；实际值由环境变量注入，见下方“凭据注入”
webdav_root_prefix = "sakuramedia"
webdav_verify_tls = true
webdav_ca_bundle = ""                # 可选；本地文件，非 WebDAV 资产
webdav_proxy = ""                    # 空=跟随明确策略；也可用 HTTP(S)_PROXY
webdav_connect_timeout_seconds = 5
webdav_read_timeout_seconds = 60
webdav_write_timeout_seconds = 300
webdav_max_connections = 16
webdav_upload_chunk_bytes = 4194304
webdav_download_chunk_bytes = 1048576
storage_temp_root_path = "/data/cache/storage-workspaces"
storage_temp_max_bytes = 107374182400
storage_temp_min_free_bytes = 1073741824
storage_temp_ttl_hours = 24
range_fallback_max_bytes = 16777216
```

可选加入逐 namespace backend，以便灰度：`assets_backend/clips_backend/media_backend`。迁移期更建议运行时模式 `local | dual_write | webdav_prefer | webdav`，而非只用一个全局开关。`import_image_root_path`、`media_clip_root_path` 在 local 和迁移源读取中保留，完成后标记 deprecated，不可立即删除。

#### 凭据注入：复用现有 Settings 源，不要自建 `*_env` 间接层

`Settings.settings_customise_sources()`（`src/config/config.py`）已经挂好 `init → toml → env → dotenv → file_secret` 五级源，因此**不需要**在 toml 里存"环境变量名"再由代码 `os.getenv()` 读取。该间接层会绕过 Pydantic 校验、在配置 API 里暴露内部约定，且与项目现有配置读取方式不一致。

正确做法是让 `[storage]` 成为普通配置节，由 env 源直接覆盖：

- 需要在 `model_config` 中显式补 `env_nested_delimiter="__"`（**当前未配置**，不加则嵌套节无法用环境变量覆盖），随后 `SAKURAMEDIA_STORAGE__WEBDAV_PASSWORD` 即可注入密码。
- 同时补 `env_prefix="SAKURAMEDIA_"` 以与既有 `SAKURAMEDIA_TELEMETRY_ENABLED` 命名风格一致；注意 telemetry 当前是 `TelemetryService` 内 `os.getenv()` 自行读取、并不经 Settings，属于历史特例，不应作为新代码范式。
- Docker secret 走 `file_secret_settings`，但 `model_config` 目前**没有配置 `secrets_dir`**，该源实际不生效。若要支持 secret 文件挂载，需一并加 `secrets_dir="/run/secrets"`。
- toml 中 `webdav_password` 保持空字符串占位，仅作为"字段存在"的声明；`ensure_runtime_config()` 不得为它自举生成随机值（与 `auth.secret_key` 语义不同）。

#### 配置 API 脱敏：需要新建能力，现有实现不脱敏

不能假设配置接口已有脱敏。`ConfigService._public_values()` 仅剔除 `READONLY_KEYS = {"auth", "enable_docs", "plugins"}`，其余配置节**明文全量返回**——`src/schema/system/config.py` 的注释明确写着"values 为全部配置节的明文快照（含敏感字段，前端自律）"，`database.url`（含库密码）当前就是明文下发的。

因此新增 `[storage]` 时必须二选一：

1. **推荐（首期）**：把 `"storage"` 加入 `READONLY_KEYS`，与 `plugins` 同等对待——整节不经配置 API 读写，只允许改 toml/环境变量后重启。改动一行，无新机制，且符合"含凭据的节手工维护"的既有惯例。
2. **字段级脱敏（可选后续）**：在 `ConfigService` 引入敏感字段路径集合，`get_config` 输出前替换为哨兵值，`update_config` 收到哨兵值时保留原值。这是新增能力，需要同时覆盖 `database.url`，否则只给 storage 脱敏会造成"部分敏感字段安全"的错觉。

若选方案 1，则 `backend`、`webdav_*` 的运行时切换只能靠重启，这与"配置改动需重启 api/aps 才生效"（见 `ConfigUpdateResource.restart_required`）的现状一致，不构成额外限制。

安全要求：

- 密码优先由环境变量/Docker secret 注入，不写回 `config.toml`；配置 API 必须按上文二选一处置（加入 `READONLY_KEYS` 或新建字段级脱敏），不能依赖不存在的脱敏行为。base URL 禁止 userinfo。
- 默认 TLS 校验开启；私有 CA 用只读 secret 挂载。`verify_tls=false` 仅作为显式开发选项并告警。
- root prefix 启动时规范化；所有 key 再做路径校验，防 SSRF/目录穿越。用户输入不能改变 host/scheme。
- WebDAV 账号仅授予该 prefix 的读写删权限；网络 ACL 只允许 API/APS 容器访问 endpoint。
- proxy 只作用于 WebDAV HTTP client；`NO_PROXY` 要覆盖内网 WebDAV 时显式配置。不要把 WebDAV proxy 设置误用于数据库或 provider。

依赖/镜像/Compose：

- `pyproject.toml` 增加 `webdav4` 直接依赖并更新 `requirements.txt` 锁定结果；HTTPX 已存在。若不使用 fsspec，不安装 extra。
- `docker/backend/Dockerfile` 不需要 davfs2、FUSE、特权模式或新增 apt 包；重新构建即可带入 Python 依赖。现有 ffmpeg 和 CA certificates 足够。
- Compose（若部署仓库维护）只需注入 secret、配置 endpoint/CA/proxy、为 workspace 保留有界本地卷；无需 WebDAV volume mount、`SYS_ADMIN` 或 `/dev/fuse`。
- API 与 APScheduler/supervisor 中所有会处理资产的进程都需 DNS/TCP/TLS 可达 WebDAV；评估最大并发、WebDAV 配额、单文件上限、MOVE/COPY、Depth=1 PROPFIND、Range、ETag 和连接超时。
- readiness 应验证认证、root可访问、可选测试 prefix 的 PUT/GET Range/MOVE/DELETE（破坏性 capability probe使用专用临时 key并清理）；liveness 不依赖远端，避免 WebDAV 故障造成重启风暴。

## 9. 存量迁移与回滚

### 9.1 迁移前清点

生成不可变 manifest：`namespace,key,local_path,size,mtime,sha256,db_reference_count`。来源包括：

- 完整遍历 `import_image_root_path` 与 `media_clip_root_path`；
- DB 的所有 `Image.*`、`Subtitle.file_path`、`MediaClip.file_path`；
- bundled local provider 的 managed media root 与 `Media.media_ref`；
- 识别 DB missing、磁盘 orphan、非法/越界路径、大小写冲突。WebDAV 可能大小写行为与本地不同，必须预检。

迁移工具必须有 `--dry-run`、断点续传、限速、并发限制和机器可读报告。默认不删除本地文件。

### 9.2 推荐在线迁移流程

1. **部署抽象但仍 local**：所有回归通过，路径语义不变。
2. **启用 dual-write**：新写先 WebDAV 后 local（或两者均成功才提交 DB）；读仍 local。失败进入重试/告警。
3. **全量/增量上传**：按 manifest 上传旧资产到相同 key。先比 size；最终用 SHA-256 校验。WebDAV ETag 不等价于内容哈希。
4. **第二遍增量**：按 mtime/size + dual-write journal 捕获迁移期间变化；核对 DB引用对象 100% 在 WebDAV 存在且 size/hash一致。
5. **切 `webdav_prefer`**：读 WebDAV；只在明确 404 时回退 local并异步修复，网络错误不回退以免掩盖故障。持续 dual-write。
6. **迁移字幕字段**：在 DB事务内把受信旧绝对路径转换为 key；保留备份映射。媒体原片由 local provider migration 更新 opaque ref/version，不能由宿主猜测。
7. **切 WebDAV authoritative**：写 WebDAV，读 WebDAV；本地保留只读观察期（建议 7～14 天）。
8. **停止 dual-write并归档本地**：另行获批后才删除；本方案不自动删除存量。

校验至少包括对象数、总字节、逐文件 size、抽样与关键对象全量 SHA-256、DB 引用可读率、随机 Range 首/中/尾字节比对、图片/Pillow、字幕、ffprobe/PyAV、媒体 seek。大视频全量 SHA-256 可以离峰执行，但切换前所有 DB 引用至少 size + 抽样 Range 一致，且迁移 manifest 标记完整。

### 9.3 回滚

- dual-write 期直接切读 local；WebDAV 新增 key按 journal反向同步回本地后再停 WebDAV写。
- `webdav_prefer` 期保持 local fallback，回滚开关无需改 DB图片/片段 key；字幕 key解析器在 local backend 下映射回旧根。
- 若已产生 WebDAV-only 对象，必须先执行 WebDAV→local reverse sync及 hash校验；否则禁止切回 local authoritative。
- 字幕 DB字段回滚使用迁移前映射恢复绝对路径；Provider media_ref 用 provider 自己的 migration journal恢复。
- 回滚不删除 WebDAV对象，待稳定后通过独立、带 grace period 的清理流程处置。

## 10. 分阶段实施与验收标准

### 阶段 0：契约与兼容性测试

建立 WebDAV 服务兼容矩阵和 integration fixture，探测 PROPFIND、MKCOL、PUT、GET Range、MOVE/COPY、DELETE、ETag、Unicode/空格、大文件、覆盖规则。

**验收：** 目标 WebDAV 实例通过 capability suite；明确记录不支持项和降级路径；错误分类不会把超时/403当404。

### 阶段 1：抽象层落地，local 行为不变

新增 `src/storage/{contracts,local,router,factory,workspace}.py`，改造公共 key helper、图片/字幕/片段 service和文件 routers，但配置仍为 local。补全单元/集成测试。

**验收：** 现有测试通过；资产 CRUD、签名 URL、Range、字幕同步、缩略图、片段均与改造前一致。

"业务 service 不再直接操作权威路径"必须由**可执行门禁**验证，不能靠人工检查。建议在 CI/pre-commit 加一条 grep 断言，命中即失败：

```bash
# 业务 service / router 内不得出现权威路径 I/O。
# 排除项均为迁移范围外的模块：telemetry（遥测落盘）、plugins router（插件 zip 安装）。
grep -rnE '\b(os\.replace|shutil\.(move|copy2?|rmtree)|\.write_bytes\(|\.write_text\(|\.unlink\(|\.mkdir\(|FileResponse)\b' \
  src/service src/api/routers \
  | grep -vE 'src/service/system/telemetry_service\.py|src/api/routers/system/plugins\.py' \
  && { echo 'FAIL: 业务层仍存在直接文件 I/O'; exit 1; }
```

**当前基线（改造前实测）**：该门禁在现有代码上命中 **24 处**（未加排除项时为 29 处，其中 5 处属范围外的 telemetry/plugins），分布于 7 个在范围内的文件——
`movie_image_service.py`、`subtitle_asset_service.py`、`media_clip_service.py`、`thumbnails/artifacts.py`、`video_cover_service.py`、`files/images.py`、`files/subtitles.py`。
阶段 1 的量化目标就是把这 24 处降到 0；这个数字同时可作为改造进度的客观刻度。

注意 `src/common/range_streaming.py` 使用 `os.stat` + `open()` 而非上述模式，不会被此门禁捕获，需单独确认它已改为经 backend 取 `stat`/流。

同时需要一次**全量调用面对账**，而不是只覆盖 §1.2 表格中的 21 个文件。实测 `Path(|write_bytes|open(|mkdir|shutil|unlink` 在 `src/` 命中 30 个文件，差集必须逐个显式判定"需改造 / 无需改造（附理由）"，例如：

| 文件 | 判定 | 理由 |
|---|---|---|
| `src/common/media_formats.py` | 无需改造 | 仅 `Path(file_name).suffix` 做扩展名判断，无 I/O |
| `src/plugins/context.py` | 需评估 | 向插件暴露 `data_dir: Path`；插件私有数据不在迁移范围，但要确认插件不借此写资产 |
| `src/config/config.py` | 无需改造 | 只读写 `config.toml`，明确排除在迁移范围外 |
| `src/scheduler/logging.py`、`src/service/system/telemetry_service.py` | 无需改造 | 日志与遥测，范围外 |
| `src/plugins/{installer,loader,manager,dependencies}.py` | 无需改造 | plugins 目录，范围外 |
| `src/start/{aps,commands,legacy_v053_upgrade}.py`、`migrations/runner.py` | 需复核 | 已确认不含资产路径逻辑，但迁移工具本身需要 local 读权限 |

对账结果应作为阶段 1 交付物落档，避免阶段 2 之后才发现遗漏的读写点。

### 阶段 2：WebDAV backend 与小资产灰度

实现 `webdav.py` 和 async range reader；先迁图片/封面/剧照/字幕/缩略图，启用 dual-write和 `webdav_prefer`。

**验收：** 小资产 CRUD/并发字幕/刷新补偿通过；断网、401、5xx、慢响应不会删 DB记录；WebDAV 中无长期临时对象；图片搜索索引可完整运行。

### 阶段 3：片段、Range 与处理工作区

片段上传和 streaming切换；实现 workspace配额、lease清理；对 ffmpeg/PyAV故障注入。

**验收：** Chrome/主流播放器首播和 seek正常；200/206/416及 HEAD正确；客户端断开释放上游连接；并发播放内存有界；ffmpeg超时/磁盘满/上传失败无脏 DB和半成品 final。

### 阶段 4：bundled local provider 的媒体原片

在 provider 源中注入 media backend，改造 stage/finalize/abort/delete/playback/thumbnail/clip/probe；重新打包 bundled provider。

**验收：** keep/delete_after_commit均成功；重复 finalize/abort幂等；崩溃恢复可继续；大文件播放 Range和 seek正常；115等非 local provider回归无变化。

### 阶段 5：存量迁移与切换

运行 manifest、dual-write、两遍增量、校验、字幕路径和 provider ref迁移，切 WebDAV authoritative。

**验收：** DB引用可读率100%；对象数/字节对账；规定比例 hash与 Range比对为零差异；观察期错误率、P95首字节、WebDAV容量和 workspace水位达标；完成回滚演练。

### 阶段 6：收口与运维

停止 dual-write，保留本地只读副本到观察期结束；启用 orphan/临时目录清理、监控和告警。

**验收：** 运维 runbook覆盖凭据轮换、证书、配额、WebDAV中断、反向同步和恢复；经单独审批才清理旧本地资产；配置/plugins/log/GFriends/数据库位置均未改变。

## 11. 预计改动文件清单

以下是实施期清单，不代表本设计提交已修改这些文件。

| 类别 | 预计文件 | 主要改动 | 风险 |
|---|---|---|---|
| 新增存储层 | `src/storage/contracts.py`, `local.py`, `webdav.py`, `factory.py`, `router.py`, `workspace.py` | backend、range、映射、工作区 | 高：全链路基础设施 |
| 配置 | `src/config/config.py`, `src/schema/system/config.py`, `src/service/system/config_service.py` | 新增 Storage 节、`env_nested_delimiter`/`secrets_dir`、`READONLY_KEYS` 加 `storage`（或新建字段级脱敏） | 高：凭据泄露/错误回退 |
| 路径/签名 | `src/common/media_paths.py`, `subtitle_paths.py`, `file_signatures.py`, `range_streaming.py` | Path→key/ref、backend range | 高：路径安全、签名兼容 |
| API工具/文件路由 | `src/api/routers/_utils.py`, `files/images.py`, `files/subtitles.py`, `playback/media_clips.py` | backend stat/stream | 高：206语义与连接释放 |
| 图片/字幕 | `movie_image_service.py`, `movie_subtitle_service.py`, `subtitle_asset_service.py`, `image_cleanup_service.py` | workspace、put/list/delete、补偿 | 高：并发分配、误删 |
| 缩略图/搜索 | `thumbnails/artifacts.py`, `thumbnails/task_service.py`, 图片搜索相关 service | 产物发布、远端读取 | 中高：批任务吞吐/状态误判 |
| 片段/探测/视频封面 | `media_clip_service.py`, `media_metadata_probe_service.py`, `video_cover_service.py` | materialize、上传、probe | 高：临时盘与大文件 |
| 媒体导入 | `imports/import_service.py`（少量适配）、bundled local provider 源及打包流程 | WebDAV stage/finalize/playback | 最高：跨仓/插件契约 |
| 依赖/部署 | `pyproject.toml`, `requirements.txt`, Docker/Compose配置 | webdav4、secret、网络/readiness | 中 |
| 测试/工具 | storage单测、WebDAV集成测试、迁移CLI/manifest测试 | 故障注入、迁移/回滚 | 高但不可省略 |

## 12. 主要风险与决策摘要

1. 最大风险不是 WebDAV CRUD，而是把网络故障误判为“不存在”后清理数据库或文件；异常分类和完整 list门禁是硬要求。
2. WebDAV与PostgreSQL无分布式事务；用临时 key、发布后写 DB、operation journal、补偿和延迟 orphan清理实现最终一致。
3. 视频播放必须代理透传 Range；整体下载只用于外部处理器或有上限的小文件回退。
4. ffmpeg/PyAV/OpenCV/Pillow仍使用本地工作区；需要容量预算、single-flight、超时和崩溃清理。
5. 字幕目录扫描分配序号存在既有并发竞争，迁移时用"`Movie` 行锁 + 已存在的 `(movie, file_path)` 唯一约束 + `IntegrityError` 重试"保护，不需要引入 advisory lock。
6. 导入媒体原片由 bundled local provider掌管，是独立改造面；只改宿主图片根和片段根无法完成任务范围。
7. 配置层有两处既有前提不成立，实施前必须先补：`model_config` 未设 `env_nested_delimiter`/`secrets_dir`（环境变量与 Docker secret 覆盖嵌套节实际不生效），且配置 API 完全不脱敏（`database.url` 当前明文下发）。新增 `[storage]` 前先决定"整节加入 `READONLY_KEYS`"还是"新建字段级脱敏"。
8. 推荐六个实施阶段（阶段0～6中，1～6为六个落地阶段），先 local抽象等价改造，再小资产、片段、媒体provider、迁移切换、运维收口；每一阶段都可独立验收和回滚。

## 13. 顺带发现的既有缺陷（非本次迁移引入）

这些问题在当前 local 实现下已存在，迁移到 WebDAV 会放大其影响，建议在对应阶段一并修复：

1. **字幕发现/写入扩展名不对称**：`_discover_subtitle_paths()`（`movie_subtitle_service.py`）只接纳 `.srt`，而 `MOVIE_SUBTITLE_EXTENSIONS` 与 `allocate_next_movie_subtitle_path()` 允许 `.srt/.ass/.ssa/.vtt`。插件或资产 API 写入 `.ass` 后，sync 永远发现不了它，该记录无法自愈。修复点在阶段 2。
2. **字幕同步会因"文件不存在"直接删 DB 行**：`movie_subtitle_service.py` 中 `if not Path(normalized_path).exists(): subtitle.delete_instance()`。本地文件系统下 `exists()` 基本可信，换成 WebDAV 后一次网络超时若被映射成 `False` 就会误删元数据。这是 §12.1 所述风险的具体落点，必须随抽象层一起改造，不能延后。
3. **`bytes=-N` 越界时的响应语义与注释不符**：`_get_range_header()`（`common/range_streaming.py`）注释称"超过文件大小则回退到整文件"，实际返回 `start=0, end=size-1` 但状态码仍为 `206` 且带 `content-range: bytes 0-(size-1)/size`。按 RFC 9110 这是合法的（206 可覆盖完整范围），但注释描述的是 200 行为。改造 Range 代理时应统一语义并补测试，避免"本地与 WebDAV 两条路径对同一请求返回不同状态码"。
