import os
import re
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _build_fake_bin(bin_dir: Path) -> None:
    # 这些桩命令只用于隔离入口脚本的系统副作用，不改动真实用户和 supervisor。
    _write_executable(bin_dir / "id", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "getent", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(bin_dir / "usermod", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "groupmod", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "useradd", "#!/usr/bin/env bash\nexit 0\n")
    # entrypoint 用 GNU stat -c 语法查目录属主，macOS 上 BSD stat 参数不同，必须桩掉；
    # 固定回 0 让属主检查始终视为不匹配，从而触发下方 chown 桩记录调用。
    _write_executable(
        bin_dir / "stat",
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ] && { [ \"$2\" = \"%u\" ] || [ \"$2\" = \"%g\" ]; }; then\n"
        "  echo 0\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    # 默认 no-op；只在设了 CHOWN_LOG_PATH 时把参数写进独立日志，避免污染 su/supervisord 日志的行数断言。
    # FAIL_CHOWN=1 模拟只读挂载或不支持 chown 的网络文件系统。
    _write_executable(
        bin_dir / "chown",
        "#!/usr/bin/env bash\n"
        "if [ -n \"${CHOWN_LOG_PATH:-}\" ]; then\n"
        "  printf 'chown:%s\\n' \"$*\" >> \"$CHOWN_LOG_PATH\"\n"
        "fi\n"
        "if [ \"${FAIL_CHOWN:-0}\" = \"1\" ]; then\n"
        "  echo \"chown: changing ownership: Read-only file system\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    # su 同时承载两类调用：`-c \"test -w ...\"` 的可写性预检，和 `-c \"... src.start.commands ...\"`
    # 的启动编排。预检结果由 WRITABLE 控制，编排步骤仍按 FAIL_* 开关决定成败。
    _write_executable(
        bin_dir / "su",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'test -w'*) [ \"${WRITABLE:-1}\" = \"1\" ] && exit 0 || exit 1 ;;\n"
        "esac\n"
        "printf 'su:%s\\n' \"$*\" >> \"$LOG_PATH\"\n"
        "case \"$*\" in\n"
        "  *'commands wait-db'*) [ \"${FAIL_WAIT_DB:-0}\" = \"1\" ] && exit 1 ;;\n"
        "  *'commands upgrade-v053'*) [ \"${FAIL_V053_UPGRADE:-0}\" = \"1\" ] && exit 1 ;;\n"
        "  *'commands migrate'*) [ \"${FAIL_MIGRATE:-0}\" = \"1\" ] && exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _write_executable(
        bin_dir / "supervisord",
        "#!/usr/bin/env bash\n"
        "printf 'supervisord:%s\\n' \"$*\" >> \"$LOG_PATH\"\n"
        "exit 0\n",
    )


def _run_entrypoint(
    tmp_path: Path,
    *,
    fail_migrate: bool = False,
    fail_v053_upgrade: bool = False,
    fail_wait_db: bool = False,
    args: list[str] | None = None,
    create_config: bool = True,
    chown_log: bool = False,
    writable: bool = True,
    chown_fails: bool = False,
):
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "docker" / "backend" / "docker-entrypoint.sh"
    data_root = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "entrypoint.log"
    app_root = tmp_path / "app"

    (data_root / "config").mkdir(parents=True, exist_ok=True)
    if create_config:
        (data_root / "config" / "config.toml").write_text("", encoding="utf-8")
    bin_dir.mkdir(parents=True, exist_ok=True)
    app_root.mkdir(parents=True, exist_ok=True)
    _build_fake_bin(bin_dir)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "LOG_PATH": str(log_path),
            "FAIL_MIGRATE": "1" if fail_migrate else "0",
            "FAIL_V053_UPGRADE": "1" if fail_v053_upgrade else "0",
            "FAIL_WAIT_DB": "1" if fail_wait_db else "0",
            "WRITABLE": "1" if writable else "0",
            "FAIL_CHOWN": "1" if chown_fails else "0",
            "SAKURAMEDIA_DATA_ROOT": str(data_root),
            "SAKURAMEDIA_APP_ROOT": str(app_root),
            "SAKURAMEDIA_SUPERVISORD_BIN": str(bin_dir / "supervisord"),
            "SAKURAMEDIA_SUPERVISORD_CONFIG": str(tmp_path / "supervisord.conf"),
        }
    )
    chown_log_path = tmp_path / "chown.log"
    if chown_log:
        env["CHOWN_LOG_PATH"] = str(chown_log_path)
    command = ["bash", str(script_path), *(args or ["start"])]
    # 入口脚本的退出码由各用例自行断言，这里显式 check=False 不抛异常。
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return result, lines


_STARTUP_STEP_RE = re.compile(
    r"-m src\.start\.commands "
    r"(wait-db|upgrade-v053|migrate|initdb|plugins sync-dependencies)"
)


def _startup_steps(lines: list[str]) -> list[str]:
    """只保留启动编排步骤，忽略 su 承载的其它检查（如目录可写性预检）。

    入口脚本会新增/调整预检步骤，直接对 su 调用做计数断言会让这些无关改动
    误伤测试；此处按已知步骤名归一化，专注验证编排顺序。
    """
    steps: list[str] = []
    for line in lines:
        match = _STARTUP_STEP_RE.search(line)
        if match:
            steps.append(match.group(1))
        elif line.startswith("supervisord:"):
            steps.append("supervisord")
    return steps


def test_docker_entrypoint_runs_migrations_before_starting_supervisor(tmp_path):
    result, lines = _run_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Waiting for database to become ready..." in result.stdout
    assert "Syncing bundled providers and checking for a v0.5.3 database upgrade..." in result.stdout
    assert "Running database migrations..." in result.stdout
    assert "Bootstrapping default account and system playlists..." in result.stdout
    assert "Syncing plugin dependencies..." in result.stdout
    assert "Starting supervisor..." in result.stdout
    assert _startup_steps(lines) == [
        "wait-db",
        "upgrade-v053",
        "migrate",
        "initdb",
        "plugins sync-dependencies",
        "supervisord",
    ]


def test_docker_entrypoint_stops_when_database_is_not_ready(tmp_path):
    result, lines = _run_entrypoint(tmp_path, fail_wait_db=True)

    assert result.returncode != 0
    assert "Waiting for database to become ready..." in result.stdout
    assert "Running database migrations..." not in result.stdout
    assert "Starting supervisor..." not in result.stdout
    assert _startup_steps(lines) == ["wait-db"]


def test_docker_entrypoint_stops_when_migration_fails(tmp_path):
    result, lines = _run_entrypoint(tmp_path, fail_migrate=True)

    assert result.returncode != 0
    assert "Running database migrations..." in result.stdout
    assert "Bootstrapping default account and system playlists..." not in result.stdout
    assert "Starting supervisor..." not in result.stdout
    assert _startup_steps(lines) == ["wait-db", "upgrade-v053", "migrate"]


def test_docker_entrypoint_stops_when_v053_upgrade_fails(tmp_path):
    result, lines = _run_entrypoint(tmp_path, fail_v053_upgrade=True)

    assert result.returncode != 0
    assert "Syncing bundled providers and checking for a v0.5.3 database upgrade..." in result.stdout
    assert "Running database migrations..." not in result.stdout
    assert "Starting supervisor..." not in result.stdout
    assert _startup_steps(lines) == ["wait-db", "upgrade-v053"]


def test_docker_entrypoint_starts_without_config_file(tmp_path):
    # config.toml 不再强制存在：缺失时入口脚本仍应正常走 migrate -> initdb -> supervisor。
    result, lines = _run_entrypoint(tmp_path, create_config=False)

    assert result.returncode == 0, result.stderr
    assert "Starting supervisor..." in result.stdout
    assert _startup_steps(lines) == [
        "wait-db",
        "upgrade-v053",
        "migrate",
        "initdb",
        "plugins sync-dependencies",
        "supervisord",
    ]


def test_docker_entrypoint_passthrough_for_non_start_commands(tmp_path):
    result, lines = _run_entrypoint(tmp_path, args=["echo", "hello"])

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert lines == []


def test_docker_entrypoint_fails_when_managed_dirs_are_not_writable(tmp_path):
    """app 用户写不进 config/logs 时必须启动即失败。

    否则容器会一路跑到写 config.toml 才崩，错误现场离根因很远。
    """
    result, lines = _run_entrypoint(tmp_path, writable=False)

    assert result.returncode != 0
    assert "无法写入" in result.stderr
    # 权限预检发生在任何数据库操作之前。
    assert "Waiting for database to become ready..." not in result.stdout
    assert _startup_steps(lines) == []


def test_docker_entrypoint_warns_when_chown_fails(tmp_path):
    """chown 失败（只读挂载/NFS）不阻断启动，但必须留下告警。"""
    result, _ = _run_entrypoint(tmp_path, chown_fails=True)

    assert result.returncode == 0, result.stderr
    assert "WARNING: chown failed" in result.stderr
    assert "Starting supervisor..." in result.stdout


def test_docker_entrypoint_chowns_managed_dirs_only(tmp_path):
    # 用户可能在 /data 下放无关目录（备份、其它项目共享数据等），入口脚本必须一根汗毛不动。
    data_root = tmp_path / "data"
    (data_root / "user-stuff").mkdir(parents=True, exist_ok=True)
    (data_root / "backup").mkdir(parents=True, exist_ok=True)

    result, _ = _run_entrypoint(tmp_path, chown_log=True)
    assert result.returncode == 0, result.stderr

    chown_log = (tmp_path / "chown.log").read_text(encoding="utf-8").splitlines()
    chown_targets = {line.split()[-1] for line in chown_log if line.startswith("chown:")}
    expected = {
        str(data_root / sub)
        for sub in (
            "config",
            "cache",
            "cache/assets",
            "cache/gfriends",
            "media-clips",
            "plugins",
            "logs",
        )
    }
    assert chown_targets == expected, chown_targets
    # 顶层 DATA_ROOT 和用户自建目录都不能被 chown 触碰。
    assert str(data_root) not in chown_targets
    assert str(data_root / "user-stuff") not in chown_targets
    assert str(data_root / "backup") not in chown_targets
