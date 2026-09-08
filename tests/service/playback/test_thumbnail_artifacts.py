import pytest
from PIL import Image as PILImage

from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService


@pytest.mark.parametrize(
    "reference",
    [
        "https://images.example.test/thumbnail.webp?token=a%2Fb",
        " https://images.example.test/thumbnail.webp",
        "\udfff",
        "file:///tmp/thumbnail.webp",
        "https://[not-an-ipv6]/thumbnail.webp",
    ],
)
def test_read_dimensions_rejects_nonlocal_references_before_filesystem_io(
    reference, monkeypatch
):
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.media_image_root_path",
        lambda: pytest.fail("constructed a local image path"),
    )
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.PILImage.open",
        lambda *_args, **_kwargs: pytest.fail("opened a nonlocal image reference"),
    )

    with pytest.raises(ValueError, match="thumbnail_image_reference_nonlocal"):
        ThumbnailArtifactService.read_dimensions(reference)


def test_read_dimensions_opens_internal_thumbnail_key_unchanged(tmp_path, monkeypatch):
    thumbnail = tmp_path / "videos/7/media/11/thumbnails/3.webp"
    thumbnail.parent.mkdir(parents=True)
    PILImage.new("RGB", (320, 180)).save(thumbnail, format="WEBP")
    monkeypatch.setattr(
        "src.service.playback.thumbnails.artifacts.media_image_root_path",
        lambda: tmp_path,
    )

    assert ThumbnailArtifactService.read_dimensions(
        "videos/7/media/11/thumbnails/3.webp"
    ) == (320, 180)
