import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest

from src.common import build_signed_image_url, file_signatures
from src.config.config import settings


def test_image_file_route_cache_does_not_outlive_signature(
    client, monkeypatch, tmp_path
):
    now = 1700000000
    monkeypatch.setattr(file_signatures, "_now_timestamp", lambda: now)
    image_root = tmp_path / "assets"
    monkeypatch.setattr(
        settings.media, "import_image_root_path", str(image_root)
    )
    target = image_root / "movies" / "aa" / "AAA-001" / "cover.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake-image-bytes")

    url = build_signed_image_url("movies/aa/AAA-001/cover.jpg")
    response = client.get(url)

    assert response.status_code == 200
    assert response.content == b"fake-image-bytes"
    expires = int(parse_qs(urlsplit(url).query)["expires"][0])
    max_age = int(response.headers["cache-control"].split("max-age=")[1])
    assert max_age == expires - now
    assert max_age >= 0


@pytest.mark.parametrize("remaining,expected", [(1, 1), (0, 0), (-1, 0)])
def test_signed_file_cache_control_near_expiration(monkeypatch, remaining, expected):
    now = 1700000000
    monkeypatch.setattr(file_signatures, "_now_timestamp", lambda: now)
    assert file_signatures.build_signed_file_cache_control(now + remaining) == f"public, max-age={expected}"


def test_image_file_route_serves_packed_thumbnail(client, monkeypatch, tmp_path):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    relative_path = "movies/aa/AAA-001/media/9/thumbnails/10.webp"
    thumbnails_dir = (
        image_root / "movies" / "aa" / "AAA-001" / "media" / "9" / "thumbnails"
    )
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        thumbnails_dir.with_name("thumbnails.zip"), "w", zipfile.ZIP_STORED
    ) as archive:
        archive.writestr("10.webp", b"packed-thumb-bytes")

    response = client.get(build_signed_image_url(relative_path))

    assert response.status_code == 200
    assert response.content == b"packed-thumb-bytes"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == file_signatures.build_signed_file_cache_control(file_signatures.build_signature_expires())


def test_image_file_route_serves_packed_movie_cover(client, monkeypatch, tmp_path):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    movie_dir = image_root / "movies" / "ab" / "AAA-001"
    movie_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(movie_dir / "assets.zip", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("cover.jpg", b"packed-cover-bytes")

    response = client.get(build_signed_image_url("movies/ab/AAA-001/cover.jpg"))

    assert response.status_code == 200
    assert response.content == b"packed-cover-bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == file_signatures.build_signed_file_cache_control(file_signatures.build_signature_expires())


def test_image_file_route_returns_404_when_pack_entry_missing(
    client, monkeypatch, tmp_path
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    relative_path = "movies/aa/AAA-001/media/9/thumbnails/10.webp"
    thumbnails_dir = (
        image_root / "movies" / "aa" / "AAA-001" / "media" / "9" / "thumbnails"
    )
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        thumbnails_dir.with_name("thumbnails.zip"), "w", zipfile.ZIP_STORED
    ) as archive:
        archive.writestr("20.webp", b"other-thumb-bytes")

    response = client.get(build_signed_image_url(relative_path))

    assert response.status_code == 404
