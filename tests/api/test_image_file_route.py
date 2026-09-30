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
