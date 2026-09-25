import time
from urllib.parse import parse_qs, urlsplit

from src.common import build_signed_image_url
from src.config.config import settings


def test_image_file_route_cache_does_not_outlive_signature(
    client, monkeypatch, tmp_path
):
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
    assert 0 <= max_age <= expires - time.time() + 2
