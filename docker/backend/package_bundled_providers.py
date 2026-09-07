"""Download the latest official provider releases for the image build."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

PROVIDER_RELEASES = (
    (
        "sakuramedia_local_provider",
        "v0.1.12",
        "https://api.github.com/repos/tinypinglite/sakuramedia_local_provider/releases/tags/v0.1.12",
    ),
    (
        "sakuramedia_115_provider",
        "v0.1.15",
        "https://api.github.com/repos/tinypinglite/sakuramedia_115_provider/releases/tags/v0.1.15",
    ),
)

_CONTRACTS_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "plugins" / "contracts.py"
)
_HOST_API_VERSION_PATTERN = re.compile(
    r"^HOST_API_VERSION\s*=\s*(\d+)\s*$", re.MULTILINE
)
_MIN_SUPPORTED_HOST_API_VERSION_PATTERN = re.compile(
    r"^MIN_SUPPORTED_HOST_API_VERSION\s*=\s*(\d+)\s*$", re.MULTILINE
)


def host_api_version(contracts_path: Path | None = None) -> int:
    """读取宿主支持的插件 Host API 版本。

    这里用正则解析源码而不是 import：打包脚本在 CI 里于依赖安装之前运行，
    而 src.plugins.contracts 会连带引入 pydantic 与 src.scheduler.contracts。
    """
    path = contracts_path or _CONTRACTS_PATH
    match = _HOST_API_VERSION_PATTERN.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"cannot resolve HOST_API_VERSION from {path}")
    return int(match.group(1))


def min_supported_host_api_version(contracts_path: Path | None = None) -> int:
    """读取宿主仍兼容的最低插件 Host API 版本。"""
    path = contracts_path or _CONTRACTS_PATH
    match = _MIN_SUPPORTED_HOST_API_VERSION_PATTERN.search(
        path.read_text(encoding="utf-8")
    )
    if match is None:
        raise ValueError(
            f"cannot resolve MIN_SUPPORTED_HOST_API_VERSION from {path}"
        )
    return int(match.group(1))


def _manifest_host_api_version(plugin_id: str, archive: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        try:
            raw_manifest = bundle.read("manifest.json")
        except KeyError as exc:
            raise ValueError(f"manifest.json missing in release: {plugin_id}") from exc
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid manifest.json: {plugin_id}") from exc
    if not isinstance(manifest, dict):
        raise TypeError(f"invalid manifest.json: {plugin_id}")
    declared_version = manifest.get("host_api_version")
    if not isinstance(declared_version, int):
        raise ValueError(f"manifest host_api_version missing: {plugin_id}")
    return declared_version


def _request_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sakuramedia-image-build",
        },
    )
    with urlopen(request) as response:
        return response.read()


def _release_asset(plugin_id: str, release_api_url: str) -> tuple[str, bytes, str]:
    try:
        release = json.loads(_request_bytes(release_api_url))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid latest release response: {plugin_id}") from exc
    if not isinstance(release, dict):
        raise TypeError(f"invalid latest release response: {plugin_id}")

    tag_name = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(tag_name, str) or not tag_name or not isinstance(assets, list):
        raise ValueError(f"invalid latest release metadata: {plugin_id}")
    version = tag_name.removeprefix("v")
    asset_name = f"{plugin_id}-{version}.zip"
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if asset is None:
        raise ValueError(f"latest release ZIP missing: {plugin_id} tag={tag_name}")

    download_url = asset.get("browser_download_url")
    digest = asset.get("digest")
    if (
        not isinstance(download_url, str)
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
    ):
        raise ValueError(f"latest release ZIP checksum missing: {plugin_id}")
    content = _request_bytes(download_url)
    sha256 = hashlib.sha256(content).hexdigest()
    if sha256 != digest.removeprefix("sha256:").lower():
        raise ValueError(f"latest release ZIP checksum mismatch: {plugin_id}")
    return tag_name, content, sha256


def package_latest_releases(output: Path) -> list[dict[str, str]]:
    downloaded = []
    for plugin_id, expected_tag, release_api_url in PROVIDER_RELEASES:
        tag_name, content, sha256 = _release_asset(plugin_id, release_api_url)
        if tag_name != expected_tag:
            raise ValueError(f"unexpected provider release tag: {plugin_id} expected={expected_tag} actual={tag_name}")
        downloaded.append((plugin_id, tag_name, content, sha256))

    # 打包期就拒绝 Host API 不兼容的 provider：宿主在 entrypoint 的 upgrade-v053 阶段
    # 会因 manifest 版本不匹配直接退出，容器起不来。把校验前移到构建期，
    # 让发版在 CI 就失败，而不是把一个必然崩溃的镜像推到 Docker Hub。
    max_supported_version = host_api_version()
    min_supported_version = min_supported_host_api_version()
    incompatible = [
        (plugin_id, tag_name, _manifest_host_api_version(plugin_id, content))
        for plugin_id, tag_name, content, _ in downloaded
    ]
    mismatched = [
        f"{plugin_id} tag={tag_name} manifest={declared} "
        f"host=[{min_supported_version},{max_supported_version}]"
        for plugin_id, tag_name, declared in incompatible
        if not min_supported_version <= declared <= max_supported_version
    ]
    if mismatched:
        raise ValueError(
            "bundled provider host_api_version mismatch: " + "; ".join(mismatched)
        )

    output.mkdir(parents=True, exist_ok=True)
    plugins: list[dict[str, str]] = []
    for plugin_id, tag_name, content, sha256 in downloaded:
        filename = f"{plugin_id}.zip"
        (output / filename).write_bytes(content)
        plugins.append({"plugin_id": plugin_id, "filename": filename, "sha256": sha256})
        print(f"bundled {plugin_id} release={tag_name}")
    (output / "official-providers.json").write_text(
        json.dumps({"version": 1, "plugins": plugins}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return plugins


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    package_latest_releases(args.output)


if __name__ == "__main__":
    main()
