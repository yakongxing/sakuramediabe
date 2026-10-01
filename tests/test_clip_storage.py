import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Response
from pydantic import ValidationError
from starlette.requests import Request

from src.common.file_signatures import build_signed_clip_url
from src.config.config import Settings, Storage, settings
from src.model import Image, Media, MediaClip, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY, ClipArtifact
from src.schema.playback.clips import MediaClipCreateRequest
from src.service.playback.media_clip_service import MediaClipService
from src.storage import factory
from src.storage.local import LocalStorageBackend
from src.storage.types import ObjectStat, PublicationResult, StorageNotFound
from src.storage.webdav import WebDAVStorageBackend


@pytest.mark.parametrize("value,expected", [("inherit", "inherit"), (" LOCAL ", "local"), ("WebDAV", "webdav")])
def test_clip_backend_values_are_normalized(value, expected):
    config = Storage(clips_backend=value, webdav_base_url="https://dav.example.test/root/")
    assert config.clips_backend == expected
    assert Storage().clips_backend == "inherit"


@pytest.mark.parametrize("value", ["", "s3", "remote", "inherit-local", None])
def test_clip_backend_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        Storage(clips_backend=value)


@pytest.mark.parametrize("url", ["", "/dav", "ftp://dav.example.test", "https://user:pass@dav.example.test", "https://dav.example.test?token=x", "https://dav.example.test#fragment"])
def test_remote_clips_require_valid_webdav_configuration_even_with_local_assets(url):
    with pytest.raises(ValidationError):
        Storage(backend="local", clips_backend="webdav", webdav_base_url=url)


def test_local_clips_do_not_disable_webdav_validation_for_remote_assets():
    with pytest.raises(ValidationError):
        Storage(backend="webdav", clips_backend="local")


@pytest.mark.parametrize("prefix", ["SAKURAMEDIA_", ""])
def test_clip_settings_environment_overrides_toml(monkeypatch, tmp_path, prefix):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[storage]\nclips_backend = "inherit"\n[media]\nmedia_clip_root_path = "/from-file"\n')
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    monkeypatch.setenv(f"{prefix}STORAGE__CLIPS_BACKEND", "local")
    monkeypatch.setenv(f"{prefix}MEDIA__MEDIA_CLIP_ROOT_PATH", "/mnt/clips")
    configured = Settings()
    assert configured.storage.clips_backend == "local"
    assert configured.media.media_clip_root_path == "/mnt/clips"


def test_clip_deployment_environment_has_precedence(monkeypatch, tmp_path):
    monkeypatch.setitem(Settings.model_config, "toml_file", tmp_path / "missing.toml")
    monkeypatch.setenv("SAKURAMEDIA_STORAGE__CLIPS_BACKEND", "inherit")
    monkeypatch.setenv("SAKURAMEDIA_MEDIA__MEDIA_CLIP_ROOT_PATH", "/prefixed")
    monkeypatch.setenv("STORAGE__CLIPS_BACKEND", "local")
    monkeypatch.setenv("MEDIA__MEDIA_CLIP_ROOT_PATH", "/deployment")
    configured = Settings()
    assert configured.storage.clips_backend == "local"
    assert configured.media.media_clip_root_path == "/deployment"


def test_clip_settings_load_from_toml(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[storage]\nbackend = "local"\nclips_backend = "webdav"\nwebdav_base_url = "https://dav.example.test/root/"\n[media]\nmedia_clip_root_path = "/saved/clips"\n')
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    configured = Settings()
    assert configured.storage.clips_backend == "webdav"
    assert configured.storage.webdav_base_url == "https://dav.example.test/root"
    assert configured.media.media_clip_root_path == "/saved/clips"


@pytest.mark.parametrize("backend", ["local", "webdav"])
@pytest.mark.parametrize("clips_backend", ["inherit", "local", "webdav"])
def test_clip_backend_matrix_is_independent_of_assets_and_subtitles(monkeypatch, tmp_path, backend, clips_backend):
    monkeypatch.setattr(settings, "storage", Storage(
        backend=backend, clips_backend=clips_backend,
        webdav_base_url="https://dav.example.test/root", root_prefix="tenant",
    ))
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    assets = factory.asset_storage()
    clips = factory.clip_storage()
    effective = backend if clips_backend == "inherit" else clips_backend
    assert isinstance(assets, LocalStorageBackend if backend == "local" else WebDAVStorageBackend)
    assert isinstance(clips, LocalStorageBackend if effective == "local" else WebDAVStorageBackend)
    assert assets is factory.asset_storage()
    assert clips is factory.clip_storage()
    assert assets is not clips
    assert factory.subtitle_storage() is assets
    if effective == "local":
        assert clips.root == tmp_path / "clips"
        assert clips.local_path("CLIP-001/1.mp4") == tmp_path / "clips/CLIP-001/1.mp4"
    else:
        assert clips.prefix == "tenant/clips"
        assert clips.base_url == "https://dav.example.test/root"
        assert clips.local_path("CLIP-001/1.mp4") is None
    if backend == "webdav":
        assert assets.prefix == "tenant/assets"


def test_clip_root_changes_take_effect_after_backend_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage", Storage(clips_backend="local"))
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "old"))
    original = factory.clip_storage()
    original.put_bytes("CLIP-001/1.mp4", b"old")
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "new"))
    assert factory.clip_storage() is original
    factory.reset_storage_backends()
    replacement = factory.clip_storage()
    assert replacement.root == tmp_path / "new"
    assert not replacement.exists("CLIP-001/1.mp4")
    assert original.local_path("CLIP-001/1.mp4").read_bytes() == b"old"


class RemoteClips:
    def __init__(self, base_url, namespace, **kwargs):
        self.namespace = namespace
        self.objects = {}
        self.ranges = []
        self.closed = False

    def put_file(self, key, source, *, overwrite):
        assert overwrite is False
        self.objects[key] = source.read_bytes()
        return PublicationResult(key, len(self.objects[key]), disposition="created")

    def stat(self, key):
        if key not in self.objects:
            raise StorageNotFound(key)
        return ObjectStat(key, len(self.objects[key]))

    def open(self, key):
        self.stat(key)
        return io.BytesIO(self.objects[key])

    def local_path(self, key):
        return None

    def range_response(self, key, range_header, content_type):
        self.ranges.append((key, range_header, content_type))
        return Response(self.objects[key][:5], status_code=206, media_type=content_type)

    def delete(self, key):
        self.objects.pop(key)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("clips_backend", ["local", "webdav"])
def test_mixed_backend_clip_create_stream_and_delete(test_db, monkeypatch, tmp_path, clips_backend):
    from src.api.routers.playback.media_clips import stream_media_clip
    from src.service.playback import media_clip_service

    assets_backend = "webdav" if clips_backend == "local" else "local"
    monkeypatch.setattr(settings, "storage", Storage(
        backend=assets_backend, clips_backend=clips_backend,
        webdav_base_url="https://dav.example.test/root",
    ))
    local_root = tmp_path / "custom-clips"
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(local_root))
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    monkeypatch.setattr(factory, "WebDAVStorageBackend", RemoteClips)
    library = MediaLibrary.create(name="mixed", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="CLIP-001", javdb_id="mixed", title="movie")
    media = Media.create(movie=movie, library=library, file_name="video.mp4")
    start = MediaThumbnail.create(media=media, image=Image.create(origin="first.webp"), offset=0)
    end = MediaThumbnail.create(media=media, image=Image.create(origin="last.webp"), offset=10)
    payload = MediaClipCreateRequest(start_thumbnail_id=start.id, end_thumbnail_id=end.id, title="clip")
    generated = []

    class Provider:
        def create_clip(self, *, workspace, **kwargs):
            generated.append(True)
            (workspace / "clip.mp4").write_bytes(b"valid video clip")
            return ClipArtifact(relative_path="clip.mp4")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda handle: Provider())
    monkeypatch.setattr(media_clip_service.MediaMetadataProbeService, "probe_file", lambda path: SimpleNamespace(duration_seconds=10))
    resource, created = MediaClipService.create_clip(media.id, payload)
    assert created
    clip = MediaClip.get_by_id(resource.clip_id)
    selected = factory.clip_storage()
    assert selected.stat(clip.file_path).size == len(b"valid video clip")
    assert MediaClipService.create_clip(media.id, payload)[1] is False
    assert generated == [True]
    url = urlsplit(build_signed_clip_url(clip.id))
    query = parse_qs(url.query)
    request = Request({"type": "http", "method": "GET", "path": url.path, "headers": [(b"range", b"bytes=0-4")]})
    response = stream_media_clip(request, clip.id, int(query["expires"][0]), query["signature"][0])
    assert response.status_code == 206
    if clips_backend == "local":
        assert (local_root / clip.file_path).read_bytes() == b"valid video clip"
        assets = factory.asset_storage()
        assert assets.objects == {} and assets.ranges == []
    else:
        assert selected.ranges == [(clip.file_path, "bytes=0-4", "video/mp4")]
        assert response.body == b"valid"
        assert not local_root.exists()
        assert isinstance(factory.asset_storage(), LocalStorageBackend)
    MediaClipService.delete_clip(clip.id)
    assert MediaClip.get_or_none(MediaClip.id == clip.id) is None
    with pytest.raises(StorageNotFound):
        selected.stat(clip.file_path)
    factory.reset_storage_backends()
    if clips_backend == "webdav":
        assert selected.closed


def test_local_clip_directory_can_be_saved_through_config_service(monkeypatch, tmp_path):
    import toml

    from src.service.system.config_service import ConfigService

    config_path = tmp_path / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    directory = str(tmp_path / "saved-clips")
    result = ConfigService.update_config({"media": {"media_clip_root_path": directory}})
    assert result.restart_required == ["api", "aps"]
    assert toml.load(config_path)["media"]["media_clip_root_path"] == directory
    assert Settings().media.media_clip_root_path == directory
