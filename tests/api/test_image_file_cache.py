from fastapi import Response
from starlette.requests import Request

from src.api.routers.files import images as image_router


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def test_local_signed_image_is_browser_cacheable(monkeypatch, tmp_path):
    image_path = tmp_path / "cover.webp"
    image_path.write_bytes(b"image")

    class Storage:
        @staticmethod
        def local_path(_key):
            return image_path

    monkeypatch.setattr(image_router, "asset_storage", lambda: Storage())
    monkeypatch.setattr(image_router, "verify_image_signature", lambda *_args: "cover.webp")

    response = image_router.get_image_file(
        _request(),
        "cover.webp",
        expires=1_700_003_600,
        signature="signature",
    )

    assert response.headers["cache-control"] == "public, max-age=3600"


def test_remote_signed_image_is_browser_cacheable(monkeypatch):
    class Storage:
        @staticmethod
        def local_path(_key):
            return None

        @staticmethod
        def range_response(_key, _range_header, _content_type):
            return Response(content=b"image", headers={"ETag": '"remote-etag"'})

    monkeypatch.setattr(image_router, "asset_storage", lambda: Storage())
    monkeypatch.setattr(image_router, "verify_image_signature", lambda *_args: "cover.webp")

    response = image_router.get_image_file(
        _request(),
        "cover.webp",
        expires=1_700_003_600,
        signature="signature",
    )

    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["etag"] == '"remote-etag"'
