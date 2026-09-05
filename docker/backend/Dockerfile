# ── Stage 1: 安装 Python 依赖 ──
FROM python:3.10-slim-bookworm AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 运行时镜像 ──
FROM python:3.10-slim-bookworm AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PUID=1000 \
    PGID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /install/bin /usr/local/bin

RUN find /usr/local/lib/python3.10/site-packages \
    \( -type d -name "__pycache__" -o \
       -type d -name "tests" -o \
       -type d -name "test" -o \
       -name "*.pyc" -o \
       -name "*.pyo" \) \
    -exec rm -rf {} + 2>/dev/null; \
    # 保留 dist-info，部分依赖会在运行时通过 importlib.metadata 读取包元数据。
    # slim 基础镜像不含 binutils，strip 缺失时静默跳过；仅为装了 binutils 的派生构建保留优化。
    if command -v strip >/dev/null 2>&1; then \
        find /usr/local/lib/python3.10/site-packages -name "*.so" -exec strip --strip-debug {} + 2>/dev/null || true; \
    fi; \
    true

COPY docker/backend/docker-entrypoint.sh /docker-entrypoint.sh
COPY docker/backend/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod +x /docker-entrypoint.sh /usr/local/bin/healthcheck.py \
    && groupadd --system app \
    && useradd --create-home --shell /bin/bash --gid app app

COPY docker/backend/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/backend/package_bundled_providers.py /app/docker/backend/package_bundled_providers.py
COPY src /app/src
RUN python /app/docker/backend/package_bundled_providers.py --output /app/bundled-plugins \
    && rm -rf /app/docker \
    && chmod -R a+rX /app/src /app/bundled-plugins

ARG SAKURAMEDIA_BACKEND_VERSION=v0.0.1
ENV SAKURAMEDIA_BACKEND_VERSION=${SAKURAMEDIA_BACKEND_VERSION}

# 主服务只保留自身运行所需的数据目录，嵌入模型由独立服务管理。
VOLUME ["/data"]
EXPOSE 8000 9001

# supervisord 收到 SIGTERM 才会走优雅停止流程（默认 SIGTERM，此处显式声明避免派生镜像改错）。
STOPSIGNAL SIGTERM

# 存活探针：确认 uvicorn 能完成一次 HTTP 往返。
# start-period 覆盖 entrypoint 里 wait-db(最长 120s) + 迁移 + 插件依赖同步的串行启动窗口，
# 否则冷启动期间会被判定 unhealthy 并触发编排层重启循环。
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
