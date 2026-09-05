#!/usr/bin/env bash
set -euo pipefail

APP_USER="app"
APP_GROUP="app"
DATA_ROOT="${SAKURAMEDIA_DATA_ROOT:-/data}"
APP_ROOT="${SAKURAMEDIA_APP_ROOT:-/app}"
SUPERVISORD_BIN="${SAKURAMEDIA_SUPERVISORD_BIN:-/usr/bin/supervisord}"
SUPERVISORD_CONFIG="${SAKURAMEDIA_SUPERVISORD_CONFIG:-/etc/supervisor/supervisord.conf}"
PYTHON_BIN="${SAKURAMEDIA_PYTHON_BIN:-python}"

ensure_app_identity() {
    local target_uid="${PUID:-1000}"
    local target_gid="${PGID:-1000}"
    local existing_group_name=""

    if ! id "${APP_USER}" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "${APP_USER}"
    fi

    existing_group_name="$(getent group "${target_gid}" | cut -d: -f1 || true)"
    if [ -n "${existing_group_name}" ]; then
        usermod -g "${existing_group_name}" "${APP_USER}"
    else
        groupmod -o -g "${target_gid}" "${APP_GROUP}"
        usermod -g "${target_gid}" "${APP_USER}"
    fi

    usermod -o -u "${target_uid}" "${APP_USER}"
}

chown_if_mismatch() {
    # 目录属主已经是目标 UID/GID 时直接返回，正常重启零开销；只在不匹配时 chown，且仅改 inode 本身。
    local target_uid="$1"
    local target_gid="$2"
    local path="$3"
    [ -e "${path}" ] || return 0
    local cur_uid cur_gid
    cur_uid="$(stat -c '%u' "${path}")"
    cur_gid="$(stat -c '%g' "${path}")"
    if [ "${cur_uid}" != "${target_uid}" ] || [ "${cur_gid}" != "${target_gid}" ]; then
        # 只读挂载或 NFS/SMB 等不支持 chown 的后端会失败；此处不中断启动，
        # 但必须告警——否则容器会一路起到写 config.toml/日志时才炸，错误现场离根因很远。
        if ! chown "${target_uid}:${target_gid}" "${path}"; then
            echo "WARNING: chown failed path=${path} target=${target_uid}:${target_gid} current=${cur_uid}:${cur_gid}" >&2
            echo "WARNING: 若后续出现权限错误，请在宿主机执行 chown -R ${target_uid}:${target_gid} 对应目录，或调整 PUID/PGID 与挂载目录属主一致。" >&2
        fi
    fi
}

verify_writable() {
    # 提前确认 app 用户真的能在关键目录里落文件，把权限问题暴露在启动早期而不是首次写配置时。
    local path="$1"
    if ! su -s /bin/sh -c "test -w \"${path}\"" "${APP_USER}"; then
        echo "ERROR: ${APP_USER} 无法写入 ${path}" >&2
        echo "ERROR: 请检查挂载点属主是否匹配 PUID=${PUID:-1000} PGID=${PGID:-1000}，或该挂载是否为只读。" >&2
        return 1
    fi
}

bootstrap_data_dirs() {
    if ! mkdir -p \
        "${DATA_ROOT}/config" \
        "${DATA_ROOT}/cache/assets" \
        "${DATA_ROOT}/cache/gfriends" \
        "${DATA_ROOT}/media-clips" \
        "${DATA_ROOT}/plugins" \
        "${DATA_ROOT}/logs"; then
        echo "ERROR: 无法在 ${DATA_ROOT} 下创建数据目录；请确认该路径已挂载且可写。" >&2
        exit 1
    fi

    # bind mount 上来的 /data 可能属主是 root 或宿主机用户，导致切到 app 用户后写不进 config.toml/日志。
    # 只把我们自己 mkdir 的目录节点归给 app，非递归——volume 里的历史缓存/媒体文件保持原样，避免海量文件被扫。
    # 顶层 ${DATA_ROOT} 是用户挂载点，此处不动；app 用户只需要在下列子目录内新建文件即可。
    local target_uid="${PUID:-1000}"
    local target_gid="${PGID:-1000}"
    local dir
    for dir in \
        "${DATA_ROOT}/config" \
        "${DATA_ROOT}/cache" \
        "${DATA_ROOT}/cache/assets" \
        "${DATA_ROOT}/cache/gfriends" \
        "${DATA_ROOT}/media-clips" \
        "${DATA_ROOT}/plugins" \
        "${DATA_ROOT}/logs"; do
        chown_if_mismatch "${target_uid}" "${target_gid}" "${dir}"
    done

    # config 与 logs 是启动必写路径（config.toml 自举、supervisord 日志），不可写就直接失败退出。
    verify_writable "${DATA_ROOT}/config" || exit 1
    verify_writable "${DATA_ROOT}/logs" || exit 1
}

wait_for_database() {
    echo "Waiting for database to become ready..."
    # 显式等待 PostgreSQL 可连接：宿主机重启时容器间无启动顺序保证，避免应用先于数据库就绪导致迁移失败。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands wait-db --timeout 120" "${APP_USER}"
}

run_database_migrations() {
    echo "Running database migrations..."
    # 迁移必须以应用用户执行，保持运行期文件与日志权限一致。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands migrate" "${APP_USER}"
}

run_v053_upgrade() {
    echo "Syncing bundled providers and checking for a v0.5.3 database upgrade..."
    # 官方 provider 随镜像升级；仅更高版本替换代码，插件 data/ 和启停状态由插件管理器保留。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands upgrade-v053" "${APP_USER}"
}

bootstrap_default_data() {
    echo "Bootstrapping default account and system playlists..."
    # 默认数据初始化保持幂等，首装补齐账号/系统播放列表，老库重复执行会自动跳过。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands initdb" "${APP_USER}"
}

sync_plugin_dependencies() {
    echo "Syncing plugin dependencies..."
    # 必须在 supervisor 启动 api/aps 前串行执行：两者都会在 import 期加载插件。
    # 单个插件失败由命令落盘并由加载器隔离，命令本身保持成功以便服务继续启动。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands plugins sync-dependencies" "${APP_USER}"
}

if [ "${1:-}" = "start" ]; then
    ensure_app_identity
    bootstrap_data_dirs
    wait_for_database
    run_v053_upgrade
    run_database_migrations
    bootstrap_default_data
    sync_plugin_dependencies

    # 主服务只负责 API 和任务编排，不处理嵌入推理设备映射。
    id "${APP_USER}" || true

    echo "Starting supervisor..."
    exec "${SUPERVISORD_BIN}" -c "${SUPERVISORD_CONFIG}"
fi

# 非 start 子命令：交给调用方指定的可执行文件（调试、一次性运维命令等）。
# 这些路径不经过 bootstrap，因此仍以 root 运行，由调用方自行决定是否降权。
exec "$@"
