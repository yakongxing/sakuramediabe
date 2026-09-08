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
        "src.service.discovery.moment_recommendation_service.resolve_image_file_path",
        lambda origin: pytest.fail(
            f"unsafe discovery reference reached filesystem: {origin!r}"
        ),
    )
    seed = SimpleNamespace(
        point=SimpleNamespace(id=1),
        thumbnail=SimpleNamespace(image=SimpleNamespace(origin=reference)),
    )

    assert MomentRecommendationService._read_seed_image_bytes(seed) is None
