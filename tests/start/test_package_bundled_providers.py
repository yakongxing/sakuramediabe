import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest


def _load_packager():
    script = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "backend"
        / "package_bundled_providers.py"
    )
    spec = importlib.util.spec_from_file_location("package_bundled_providers", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_zip(plugin_id: str, host_api_version: int) -> bytes:
    """构造带 manifest.json 的最小 provider 包。

    打包脚本会读取 manifest 校验 Host API 版本，因此测试夹具必须是真实 ZIP。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(
                {
                    "plugin_id": plugin_id,
                    "display_name": plugin_id,
                    "version": "0.0.1",
                    "host_api_version": host_api_version,
                    "requires_python": ">=3.10,<3.11",
                    "dependencies": [],
                }
            ),
        )
    return buffer.getvalue()


def test_package_bundled_providers_downloads_and_verifies_latest_releases(
    monkeypatch, tmp_path
):
    packager = _load_packager()
    host_version = packager.host_api_version()
    local_zip = _provider_zip("sakuramedia_local_provider", host_version)
    cloud115_zip = _provider_zip("sakuramedia_115_provider", host_version)
    local_api_url = packager.PROVIDER_RELEASES[0][1]
    cloud115_api_url = packager.PROVIDER_RELEASES[1][1]
    local_download_url = "https://downloads.example/local.zip"
    cloud115_download_url = "https://downloads.example/cloud115.zip"
    responses = {
        local_api_url: json.dumps(
            {
                "tag_name": "v1.2.3",
                "assets": [
                    {
                        "name": "sakuramedia_local_provider-1.2.3.zip",
                        "browser_download_url": local_download_url,
                        "digest": f"sha256:{hashlib.sha256(local_zip).hexdigest()}",
                    }
                ],
            }
        ).encode(),
        cloud115_api_url: json.dumps(
            {
                "tag_name": "v4.5.6",
                "assets": [
                    {
                        "name": "sakuramedia_115_provider-4.5.6.zip",
                        "browser_download_url": cloud115_download_url,
                        "digest": f"sha256:{hashlib.sha256(cloud115_zip).hexdigest()}",
                    }
                ],
            }
        ).encode(),
        local_download_url: local_zip,
        cloud115_download_url: cloud115_zip,
    }
    monkeypatch.setattr(packager, "_request_bytes", responses.__getitem__)

    output = tmp_path / "output"
    plugins = packager.package_latest_releases(output)

    index = json.loads((output / "official-providers.json").read_text(encoding="utf-8"))
    assert index == {"version": 1, "plugins": plugins}
    assert (output / "sakuramedia_local_provider.zip").read_bytes() == local_zip
    assert (output / "sakuramedia_115_provider.zip").read_bytes() == cloud115_zip


def test_package_bundled_providers_rejects_a_release_asset_with_wrong_digest(
    monkeypatch,
):
    packager = _load_packager()
    release_api_url = "https://api.example/releases/latest"
    download_url = "https://downloads.example/local.zip"
    monkeypatch.setattr(
        packager,
        "_request_bytes",
        {
            release_api_url: json.dumps(
                {
                    "tag_name": "v1.2.3",
                    "assets": [
                        {
                            "name": "sakuramedia_local_provider-1.2.3.zip",
                            "browser_download_url": download_url,
                            "digest": f"sha256:{'0' * 64}",
                        }
                    ],
                }
            ).encode(),
            download_url: b"unexpected content",
        }.__getitem__,
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        packager._release_asset("sakuramedia_local_provider", release_api_url)


def test_package_bundled_providers_rejects_incompatible_host_api_version(
    monkeypatch, tmp_path
):
    """Host API 不兼容必须在打包期失败。

    否则镜像会被推到 Docker Hub，用户拉起后在 entrypoint 的 upgrade-v053
    阶段崩溃退出，排查成本远高于构建期报错。
    """
    packager = _load_packager()
    host_version = packager.host_api_version()
    local_zip = _provider_zip("sakuramedia_local_provider", host_version)
    # 115 provider 声明比宿主更高的版本，模拟 provider 先行发版的真实情况。
    cloud115_zip = _provider_zip("sakuramedia_115_provider", host_version + 1)
    local_api_url = packager.PROVIDER_RELEASES[0][1]
    cloud115_api_url = packager.PROVIDER_RELEASES[1][1]
    local_download_url = "https://downloads.example/local.zip"
    cloud115_download_url = "https://downloads.example/cloud115.zip"
    monkeypatch.setattr(
        packager,
        "_request_bytes",
        {
            local_api_url: json.dumps(
                {
                    "tag_name": "v1.2.3",
                    "assets": [
                        {
                            "name": "sakuramedia_local_provider-1.2.3.zip",
                            "browser_download_url": local_download_url,
                            "digest": f"sha256:{hashlib.sha256(local_zip).hexdigest()}",
                        }
                    ],
                }
            ).encode(),
            cloud115_api_url: json.dumps(
                {
                    "tag_name": "v4.5.6",
                    "assets": [
                        {
                            "name": "sakuramedia_115_provider-4.5.6.zip",
                            "browser_download_url": cloud115_download_url,
                            "digest": f"sha256:{hashlib.sha256(cloud115_zip).hexdigest()}",
                        }
                    ],
                }
            ).encode(),
            local_download_url: local_zip,
            cloud115_download_url: cloud115_zip,
        }.__getitem__,
    )

    output = tmp_path / "output"
    with pytest.raises(ValueError, match="host_api_version mismatch"):
        packager.package_latest_releases(output)

    # 校验失败必须发生在落盘之前，避免留下半套产物被后续构建步骤误用。
    assert not (output / "sakuramedia_local_provider.zip").exists()
    assert not (output / "official-providers.json").exists()


def test_package_bundled_providers_rejects_release_without_manifest(monkeypatch):
    packager = _load_packager()
    with pytest.raises(ValueError, match="manifest.json missing"):
        packager._manifest_host_api_version("sakuramedia_local_provider", _empty_zip())


def _empty_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    return buffer.getvalue()


def test_host_api_version_reads_contracts_source(tmp_path):
    packager = _load_packager()
    contracts = tmp_path / "contracts.py"
    contracts.write_text(
        "HOST_API_VERSION = 42\nMIN_SUPPORTED_HOST_API_VERSION = HOST_API_VERSION\n",
        encoding="utf-8",
    )
    assert packager.host_api_version(contracts) == 42


def test_host_api_version_matches_repository_contracts():
    """守护正则解析与真实源码同步，避免 contracts.py 改写法后校验静默失效。"""
    packager = _load_packager()
    from src.plugins.contracts import HOST_API_VERSION

    assert packager.host_api_version() == HOST_API_VERSION
