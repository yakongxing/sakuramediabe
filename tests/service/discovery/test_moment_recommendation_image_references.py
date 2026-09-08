from io import BytesIO
from types import SimpleNamespace

import pytest

from src.service.discovery.moment_recommendation_service import (
    MomentRecommendationService,
)


@pytest.mark.parametrize(
    "reference", [" https://images.example.test/thumbnail.webp", "\udfff"]
)
def test_seed_image_rejects_unsafe_reference_before_filesystem_io(
    reference, monkeypatch
):
    monkeypatch.setattr(
        "src.service.discovery.moment_recommendation_service.asset_storage",
        lambda: pytest.fail(
            f"unsafe discovery reference reached storage: {reference!r}"
        ),
    )
    seed = SimpleNamespace(
        point=SimpleNamespace(id=1),
        thumbnail=SimpleNamespace(image=SimpleNamespace(origin=reference)),
    )

    assert MomentRecommendationService._read_seed_image_bytes(seed) is None


@pytest.mark.parametrize("unavailable", [False, True])
def test_seed_reads_remote_storage(monkeypatch, unavailable):
    from src.storage.types import StorageUnavailable

    stream = BytesIO(b"remote thumbnail")

    def open_image(key):
        assert key == "videos/7/media/11/thumbnails/3.webp"
        if unavailable:
            raise StorageUnavailable("offline")
        return stream

    monkeypatch.setattr(
        "src.service.discovery.moment_recommendation_service.asset_storage",
        lambda: SimpleNamespace(open=open_image),
    )
    seed = SimpleNamespace(
        point=SimpleNamespace(id=1),
        thumbnail=SimpleNamespace(image=SimpleNamespace(origin="videos/7/media/11/thumbnails/3.webp")),
    )
    result = MomentRecommendationService._read_seed_image_bytes(seed)
    assert result == (None if unavailable else b"remote thumbnail")
    if not unavailable:
        assert stream.closed
