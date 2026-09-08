import pytest
from pydantic import ValidationError

from src.metadata._providers.models import JavdbMovieActorResource
from src.model import Image
from src.schema.catalog.actors import ImageResource
from src.service.catalog.movie_image_service import MovieImageService


@pytest.mark.parametrize(
    "reference",
    [
        "https://images.example.test/cover.jpg?size=large&token=a%2Fb",
        "http://cdn.example.test/avatar.png",
    ],
)
def test_image_resource_preserves_external_urls(reference):
    resource = ImageResource(
        id=1,
        origin=reference,
        small=reference,
        medium=reference,
        large=reference,
    )

    assert resource.origin == reference
    assert resource.small == reference
    assert resource.medium == reference
    assert resource.large == reference


def test_image_resource_signs_internal_key_and_preserves_signed_path():
    resource = ImageResource(
        id=1,
        origin="movies/ab/AB-001/cover.jpg",
        small="/files/images/already-signed?expires=1&signature=x",
        medium="movies/ab/AB-001/cover.jpg",
        large="movies/ab/AB-001/cover.jpg",
    )

    assert resource.origin.startswith("/files/images/movies/ab/AB-001/cover.jpg?")
    assert resource.small == "/files/images/already-signed?expires=1&signature=x"


@pytest.mark.parametrize(
    "reference",
    [
        "javascript:alert(1)",
        "file:///tmp/image.jpg",
        "https://white space.example/image.jpg",
        "https://example.test:not-a-port/image.jpg",
        "https://[not-an-ipv6]/image.jpg",
    ],
)
def test_image_resource_rejects_malformed_url_like_persisted_references(
    reference, monkeypatch
):
    signed_values = []
    monkeypatch.setattr(
        "src.schema.catalog.actors.build_signed_image_url",
        lambda value: signed_values.append(value) or f"signed:{value}",
    )

    with pytest.raises(ValidationError):
        ImageResource(
            id=1,
            origin=reference,
            small="movies/ab/AB-001/small.jpg",
            medium="movies/ab/AB-001/medium.jpg",
            large="movies/ab/AB-001/large.jpg",
        )
    assert reference not in signed_values


@pytest.mark.parametrize(
    "reference",
    [
        "javascript:alert(1)",
        "data:image/png;base64,abc",
        "file:///tmp/image.jpg",
        "//example.test/image.jpg",
        "https://user:pass@example.test/image.jpg",
        "https://example.test/image.jpg#fragment",
        "https:///image.jpg",
        "https://example.test/image.jpg\nnext",
        "https://white space.example/image.jpg",
        "https://example.test:not-a-port/image.jpg",
        "https://example.test:99999/image.jpg",
        "https://./image.jpg",
        "https://%65xample.test/image.jpg",
        "https://[not-an-ipv6]/image.jpg",
        "https://[::1/image.jpg",
    ],
)
def test_external_image_reference_validator_rejects_unsafe_values(reference):
    from src.common.image_references import validate_external_image_url

    with pytest.raises(ValueError):
        validate_external_image_url(reference)


@pytest.mark.parametrize(
    "reference",
    [
        " https://example.test/image.jpg",
        "https://example.test/image.jpg ",
        "https://example.test/a b/image.jpg",
        "https://example.test/image.jpg?token=a\tb",
        "https://example.test\\image.jpg",
        "https://example.test/a\\b?token=c\\d",
        "https://example.test:/image.jpg",
        "https://example.test/image.jpg?token=%",
        "https://example.test/image.jpg?token=%2",
        "https://example.test/image.jpg?token=%GG",
        "https://example.test/\ud800.jpg",
        "https://example.test/image.jpg?token=\udfff",
    ],
)
def test_external_image_reference_validator_rejects_parser_ambiguous_values(
    reference,
):
    from src.common.image_references import (
        is_external_image_reference,
        validate_external_image_url,
    )

    with pytest.raises(ValueError):
        validate_external_image_url(reference)
    assert is_external_image_reference(reference) is False


def _url_with_utf8_size(size: int) -> str:
    prefix = "https://example.test/"
    remaining = size - len(prefix.encode("utf-8"))
    multibyte_characters, ascii_characters = divmod(remaining, len("界".encode()))
    return prefix + ("界" * multibyte_characters) + ("x" * ascii_characters)


@pytest.mark.parametrize("multibyte", [False, True])
def test_external_image_reference_validator_enforces_exact_utf8_byte_boundary(
    multibyte,
):
    from src.common.image_references import validate_external_image_url

    prefix = "https://example.test/"
    at_limit = (
        _url_with_utf8_size(2048)
        if multibyte
        else prefix + ("x" * (2048 - len(prefix.encode("utf-8"))))
    )
    over_limit = at_limit + "x"

    assert len(at_limit.encode("utf-8")) == 2048
    assert len(over_limit.encode("utf-8")) == 2049
    assert (len(at_limit) < 2048) is multibyte
    assert validate_external_image_url(at_limit) == at_limit
    with pytest.raises(ValueError):
        validate_external_image_url(over_limit)


def test_external_image_reference_validator_preserves_valid_unicode_url():
    from src.common.image_references import validate_external_image_url

    reference = "https://例え.test/画像/表紙.jpg?検索=桜&token=a%2Fb"

    assert validate_external_image_url(reference) == reference


def test_external_image_reference_validator_preserves_query_bytes():
    from src.common.image_references import validate_external_image_url

    reference = "https://images.example.test/image.jpg?token=a%2Fb+z&x=%25&x="

    assert validate_external_image_url(reference) == reference


@pytest.mark.parametrize(
    "reference", ["https://[", "https://]", "https://[::1", "https://example.test:bad"]
)
def test_nonlocal_image_reference_is_total_and_fail_closed(reference):
    from src.common.image_references import is_nonlocal_image_reference

    assert is_nonlocal_image_reference(reference) is True


@pytest.mark.parametrize(
    "reference",
    [
        " https://example.test/a.jpg",
        "https://example.test/a.jpg ",
        "\udfff",
        "movies/ab/ABC-001/cover\x1f.jpg",
        "http:/example.test/a.jpg",
        "https:///a.jpg",
        "//example.test/a.jpg",
        "/movies/ab/ABC-001/cover.jpg",
        "../movies/ab/ABC-001/cover.jpg",
        "movies/ab/ABC-001/../cover.jpg",
        "movies\\ab\\ABC-001\\cover.jpg",
        " movies/ab/ABC-001/cover.jpg",
        "movies/ab/ABC-001/cover.jpg ",
        "movies//ab/ABC-001/cover.jpg",
        "movies/./ABC-001/cover.jpg",
        "movies/%2e%2e/ABC-001/cover.jpg",
        "",
    ],
)
def test_nonlocal_image_reference_rejects_every_noncanonical_or_unsafe_key(reference):
    from src.common.image_references import is_nonlocal_image_reference

    assert is_nonlocal_image_reference(reference) is True


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("movies/ab/ABC-001/cover.jpg", False),
        ("videos/7/media/11/thumbnails/3.webp", False),
        ("https://example.test/a.jpg", True),
        ("http://127.0.0.1:8080/a.jpg?size=large", True),
    ],
)
def test_nonlocal_image_reference_accepts_only_canonical_internal_keys(
    reference, expected
):
    from src.common.image_references import is_nonlocal_image_reference

    assert is_nonlocal_image_reference(reference) is expected


@pytest.mark.parametrize("reference", ["https://example.test/\ud800", "\udfff"])
def test_image_reference_classifiers_are_total_for_invalid_unicode(reference):
    from src.common.image_references import (
        is_external_image_reference,
        is_nonlocal_image_reference,
    )

    assert is_external_image_reference(reference) is False
    assert is_nonlocal_image_reference(reference) is True


@pytest.mark.parametrize(
    "reference",
    ["javascript:alert(1)", "file:///tmp/image.jpg", "https://[not-an-ipv6]/x"],
)
def test_signed_image_file_boundary_rejects_url_like_references(reference):
    from src.api.exception.errors import ApiError
    from src.common.file_signatures import build_signed_image_url

    with pytest.raises(ApiError, match="文件路径非法"):
        build_signed_image_url(reference)


def test_image_fields_allow_long_external_urls():
    assert all(
        field.max_length >= 2048
        for field in (Image.origin, Image.small, Image.medium, Image.large)
    )


def test_image_fields_round_trip_long_external_url(test_db):
    reference = "https://images.example.test/cover.jpg?token=" + ("x" * 1800)

    image = Image.create(
        origin=reference,
        small=reference,
        medium=reference,
        large=reference,
    )
    current = Image.get_by_id(image.id)

    assert (current.origin, current.small, current.medium, current.large) == (
        reference,
        reference,
        reference,
        reference,
    )


def test_catalog_image_tasks_keep_direct_urls_and_reject_invalid_cover():
    cover = "https://images.example.test/cover.jpg?token=a%2Fb"
    plot = "https://images.example.test/plot.jpg"
    service = MovieImageService(image_downloader=lambda *_: pytest.fail("downloaded"))

    cover_task, plot_tasks, actor_tasks = service.build_catalog_direct_image_tasks(
        "AB-001", cover, [plot], []
    )

    assert cover_task.relative_path == cover
    assert plot_tasks[0].relative_path == plot
    assert actor_tasks == {}
    with pytest.raises(ValueError):
        service.build_catalog_direct_image_tasks(
            "AB-001", "file:///tmp/cover.jpg", [], []
        )


def test_catalog_image_tasks_skip_invalid_optional_urls_and_reject_invalid_cover():
    actor = JavdbMovieActorResource(
        javdb_id="actor-invalid-url",
        name="Actor",
        avatar_url="https://example.test/avatar\\bad.jpg",
    )
    service = MovieImageService(image_downloader=lambda *_: pytest.fail("downloaded"))

    with pytest.raises(ValueError):
        service.build_catalog_direct_image_tasks(
            "AB-001", "https://example.test:/cover.jpg", [], []
        )

    cover_task, plot_tasks, actor_tasks = service.build_catalog_direct_image_tasks(
        "AB-001",
        "https://example.test/cover.jpg",
        ["https://example.test/plot bad.jpg"],
        [actor],
    )

    assert cover_task is not None
    assert plot_tasks == []
    assert actor_tasks == {}
