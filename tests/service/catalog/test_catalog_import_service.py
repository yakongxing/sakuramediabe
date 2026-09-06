"""CatalogImportService 三函数语义护栏。

锁住三条导入语义，防止重构把它改回"全能 upsert"：

1. ``import_movie_if_missing``：纯新建——已存在影片一个字段都不写；
2. ``refresh_movie_metadata_strict``：纯覆盖——手动刷新入口，已存在影片全量覆盖；
3. ``update_movie_fields``：指定字段更新——不存在先完整导入，存在只写白名单字段。
"""

import hashlib
import io

import pytest

from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import Actor, Image, Movie
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_image_service import (
    ImagePersistTask,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.storage.types import StorageUnavailable


def _prepared_file(tmp_path, name: str, content: bytes):
    root = tmp_path / name
    root.mkdir()
    path = root / f"{name}.jpg"
    path.write_bytes(content)
    return ImagePersistTask(
        "plot", f"https://example.invalid/{name}.jpg", f"movies/a/{name}.jpg", path
    ), path, root


def _build_detail(
    *,
    javdb_id: str,
    movie_number: str,
    title: str,
    summary: str,
    actors: list[JavdbMovieActorResource] | None = None,
) -> JavdbMovieDetailResource:
    """构造无图片的最小详情：封面/剧照/演员头像全空，导入链路零图片 IO。"""
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=title,
        summary=summary,
        duration_minutes=120,
        release_date="2024-01-01",
        score=9.5,
        score_number=200,
        watched_count=100,
        want_watch_count=10,
        comment_count=5,
        actors=actors or [],
        tags=[],
    )


def _create_local_movie(
    *,
    javdb_id: str,
    movie_number: str,
    title: str,
    summary: str,
    score: float = 1.0,
) -> Movie:
    return Movie.create(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=title,
        summary=summary,
        score=score,
    )


def test_import_movie_if_missing_skips_existing_movie_without_writing(test_db):
    """已存在影片：纯新建跳过，所有字段保持本地值。"""
    _create_local_movie(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="本地标题",
        summary="本地描述",
        score=1.0,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is False
    assert movie.id is not None
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.title == "本地标题"
    assert refreshed.summary == "本地描述"
    assert refreshed.score == 1.0


def test_import_existing_movie_repairs_actor_avatar_missed_earlier(test_db, monkeypatch):
    movie = _create_local_movie(
        javdb_id="javdb-repair", movie_number="REPAIR-001", title="local", summary="local"
    )
    actor = Actor.create(javdb_id="actor-fast-repair", name="演员")
    image = Image.create(origin="repaired.jpg", small="repaired.jpg", medium="repaired.jpg", large="repaired.jpg")
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/repaired.jpg",
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id,
        movie_number=movie.movie_number,
        title="remote",
        summary="remote",
        actors=[resource],
    )
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "persist_image", lambda **kwargs: image)

    returned, created = service.import_movie_if_missing(detail)

    assert created is False
    assert returned.id == movie.id
    assert Actor.get_by_id(actor.id).profile_image_id == image.id


def test_import_existing_movie_repairs_dangling_actor_avatar_file(test_db, monkeypatch):
    from src.service.catalog import catalog_import_service as module

    movie = _create_local_movie(
        javdb_id="javdb-dangling", movie_number="DANGLING-001", title="local", summary="local"
    )
    image = Image.create(
        origin="actors/dangling.jpg",
        small="actors/dangling.jpg",
        medium="actors/dangling.jpg",
        large="actors/dangling.jpg",
    )
    actor = Actor.create(javdb_id="actor-dangling", name="演员", profile_image=image)
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/dangling.jpg",
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id,
        movie_number=movie.movie_number,
        title="remote",
        summary="remote",
        actors=[resource],
    )
    storage = type("Storage", (), {"exists": lambda self, key: False})()
    monkeypatch.setattr(module, "asset_storage", lambda: storage)
    service = CatalogImportService()
    persisted = []
    monkeypatch.setattr(
        service.image_service,
        "persist_image",
        lambda **kwargs: persisted.append(kwargs) or image,
    )

    service.import_movie_if_missing(detail)

    assert len(persisted) == 1
    assert Actor.get_by_id(actor.id).profile_image_id == image.id


@pytest.mark.parametrize("has_profile_image", [True, False])
def test_import_existing_movie_ignores_avatar_storage_probe_unavailable(
    test_db, monkeypatch, has_profile_image
):
    from src.service.catalog import catalog_import_service as module

    movie = _create_local_movie(
        javdb_id="javdb-probe-down",
        movie_number="PROBE-DOWN-001",
        title="local",
        summary="local",
    )
    image = Image.create(
        origin="actors/probe-down.jpg",
        small="actors/probe-down.jpg",
        medium="actors/probe-down.jpg",
        large="actors/probe-down.jpg",
    )
    actor = Actor.create(
        javdb_id="actor-probe-down",
        name="演员",
        profile_image=image if has_profile_image else None,
    )
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/probe-down.jpg",
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id,
        movie_number=movie.movie_number,
        title="remote",
        summary="remote",
        actors=[resource],
    )

    class UnavailableStorage:
        def exists(self, key):
            raise StorageUnavailable("storage probe unavailable")

    monkeypatch.setattr(module, "asset_storage", lambda: UnavailableStorage())
    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda *args: warnings.append(args))

    returned, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is False
    assert returned.id == movie.id
    assert len(warnings) == 1
    assert "avatar repair storage probe failed" in warnings[0][0]


def test_actor_avatar_repair_transaction_failure_removes_owned_file_and_record(
    test_db, monkeypatch
):
    from src.service.catalog import catalog_import_service as module

    actor = Actor.create(javdb_id="actor-compensate", name="演员")
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/new.jpg",
    )
    deleted = []

    class Storage:
        def exists(self, key): return False
        def delete(self, key, *, missing_ok=True): deleted.append(key)

    class FailingLock:
        def __enter__(self): raise RuntimeError("transaction lock failed")
        def __exit__(self, *args): return False

    monkeypatch.setattr(module, "asset_storage", lambda: Storage())
    service = CatalogImportService(persist_lock=FailingLock())
    task = service.image_service._build_image_task(
        "actor", actor.javdb_id, resource.avatar_url
    )
    created_images = []

    def persist_image(**kwargs):
        image = Image.create(
            origin=task.relative_path,
            small=task.relative_path,
            medium=task.relative_path,
            large=task.relative_path,
        )
        created_images.append(image)
        return image

    monkeypatch.setattr(service.image_service, "persist_image", persist_image)

    with pytest.raises(RuntimeError, match="transaction lock failed"):
        service._repair_missing_actor_avatars([resource])

    assert Image.get_or_none(Image.id == created_images[0].id) is None
    # Deterministic avatar keys may have been reused concurrently; leave the object
    # for delayed reference-aware cleanup rather than deleting it eagerly.
    assert deleted == []
    assert Actor.get_by_id(actor.id).profile_image_id is None


def test_actor_avatar_repair_replaces_dangling_record_and_removes_old_unused_image(
    test_db, monkeypatch
):
    from src.service.catalog import catalog_import_service as module

    old_image = Image.create(
        origin="actors/old-dangling.jpg",
        small="actors/old-dangling.jpg",
        medium="actors/old-dangling.jpg",
        large="actors/old-dangling.jpg",
    )
    actor = Actor.create(javdb_id="actor-replace", name="演员", profile_image=old_image)
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/new.jpg",
    )
    new_image = Image.create(
        origin="actors/new.jpg", small="actors/new.jpg", medium="actors/new.jpg", large="actors/new.jpg"
    )
    deleted = []

    class Storage:
        def exists(self, key): return False
        def delete(self, key, *, missing_ok=True): deleted.append(key)

    monkeypatch.setattr(module, "asset_storage", lambda: Storage())
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "persist_image", lambda **kwargs: new_image)
    monkeypatch.setattr(
        service.image_service,
        "delete_obsolete_image_files",
        lambda paths: deleted.extend(sorted(paths)),
    )

    service._repair_missing_actor_avatars([resource])

    assert Actor.get_by_id(actor.id).profile_image_id == new_image.id
    assert Image.get_or_none(Image.id == old_image.id) is None
    assert deleted == [old_image.origin]


def test_actor_avatar_repair_upsert_failure_after_publication_is_nonfatal(test_db, monkeypatch):
    from src.service.catalog import catalog_import_service as module

    actor = Actor.create(javdb_id="actor-upsert-fail", name="演员")
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/new.jpg",
    )

    class Storage:
        def exists(self, key): return False

    monkeypatch.setattr(module, "asset_storage", lambda: Storage())
    service = CatalogImportService()
    monkeypatch.setattr(
        service.image_service,
        "persist_image",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("upsert failed after publish")),
    )

    service._repair_missing_actor_avatars([resource])

    assert Actor.get_by_id(actor.id).profile_image_id is None


def test_import_movie_if_missing_creates_new_movie(test_db):
    """不存在影片：完整导入并返回 created=True。"""
    detail = _build_detail(
        javdb_id="javdb-ABP-456",
        movie_number="ABP-456",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is True
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.title == "JavDB标题"
    assert refreshed.summary == "JavDB描述"
    assert refreshed.score == 9.5
    assert refreshed.watched_count == 100


def test_import_movie_if_missing_does_not_mark_collection(test_db):
    detail = _build_detail(
        javdb_id="javdb-OFJE-456",
        movie_number="OFJE-456",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created = CatalogImportService().import_movie_if_missing(detail)

    assert created is True
    assert Movie.get_by_id(movie.id).is_collection is False


def test_import_movie_if_missing_updates_actor_gender_from_movie_detail(test_db):
    detail = _build_detail(
        javdb_id="javdb-ABP-457",
        movie_number="ABP-457",
        title="JavDB标题",
        summary="JavDB描述",
        actors=[
            JavdbMovieActorResource(
                javdb_id="actor-1",
                name="演员一",
                gender=1,
            )
        ],
    )

    CatalogImportService().import_movie_if_missing(detail)

    assert Actor.get(Actor.javdb_id == "actor-1").gender == 1


def test_actor_upsert_without_gender_update_preserves_existing_gender(test_db):
    actor = Actor.create(
        javdb_id="actor-2",
        name="旧名字",
        gender=1,
    )
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name="新名字",
        gender=0,
    )

    CatalogImportService().upsert_actor_from_javdb_resource(resource)

    refreshed = Actor.get_by_id(actor.id)
    assert refreshed.name == "新名字"
    assert refreshed.gender == 1


def test_strict_actor_refresh_preserves_existing_gender(test_db):
    actor = Actor.create(
        javdb_id="actor-3",
        name="旧名字",
        gender=1,
    )
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name="新名字",
        gender=0,
    )

    CatalogImportService()._refresh_actor_from_javdb_resource_strict(
        actor_resource=resource,
        profile_image_task=None,
    )

    refreshed = Actor.get_by_id(actor.id)
    assert refreshed.name == "新名字"
    assert refreshed.gender == 1


def test_strict_actor_refresh_blank_avatar_preserves_existing_avatar(test_db):
    image = Image.create(origin="actor-old.jpg", small="actor-old.jpg", medium="actor-old.jpg", large="actor-old.jpg")
    actor = Actor.create(javdb_id="actor-avatar", name="旧名字", profile_image=image)
    resource = JavdbMovieActorResource(javdb_id=actor.javdb_id, name="新名字", avatar_url="")

    CatalogImportService()._refresh_actor_from_javdb_resource_strict(
        actor_resource=resource,
        profile_image_task=None,
    )

    assert Actor.get_by_id(actor.id).profile_image_id == image.id


def test_existing_actor_missing_avatar_can_be_repaired(test_db, monkeypatch):
    actor = Actor.create(javdb_id="actor-repair", name="演员")
    image = Image.create(origin="actor-new.jpg", small="actor-new.jpg", medium="actor-new.jpg", large="actor-new.jpg")
    resource = JavdbMovieActorResource(
        javdb_id=actor.javdb_id,
        name=actor.name,
        avatar_url="https://example.invalid/new.jpg",
    )
    task = ImagePersistTask("actor", resource.avatar_url, image.origin, image.origin)
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "persist_refreshed_image_record", lambda value: image)

    service._refresh_actor_from_javdb_resource_strict(
        actor_resource=resource,
        profile_image_task=task,
    )

    assert Actor.get_by_id(actor.id).profile_image_id == image.id


def test_strict_refresh_upload_failure_leaves_database_image_reference_unchanged(test_db, monkeypatch):
    old_image = Image.create(origin="old.jpg", small="old.jpg", medium="old.jpg", large="old.jpg")
    new_image = Image.create(origin="new.jpg", small="new.jpg", medium="new.jpg", large="new.jpg")
    movie = _create_local_movie(
        javdb_id="javdb-upload-fail", movie_number="FAIL-001", title="old", summary="old"
    )
    movie.cover_image = old_image
    movie.save(only=[Movie.cover_image])
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: [])
    monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())
    monkeypatch.setattr(
        service,
        "_refresh_movie_metadata_records_strict",
        lambda **kwargs: (Movie.update(cover_image=new_image).where(Movie.id == movie.id).execute() and Movie.get_by_id(movie.id), set(), []),
    )
    monkeypatch.setattr(
        service.image_service,
        "finalize_prepared_image_files",
        lambda prepared, **kwargs: (_ for _ in ()).throw(StorageUnavailable("publish failed")),
    )

    with pytest.raises(StorageUnavailable, match="publish failed"):
        service.refresh_movie_metadata_strict(movie, detail)

    assert Movie.get_by_id(movie.id).cover_image_id == old_image.id


def test_strict_refresh_partial_upload_failure_preserves_published_immutable_objects(test_db, monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as image_module
    from src.storage.types import ObjectStat, StorageNotFound

    movie = _create_local_movie(
        javdb_id="javdb-partial", movie_number="PARTIAL-001", title="old", summary="old"
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    values = [("reused", b"same"), ("created", b"new"), ("failure", b"boom")]
    prepared = []
    for name, content in values:
        task, path, root = _prepared_file(tmp_path, name, content)
        prepared.append(PreparedImageFile(task, path, root))
    reused_key = f"movies/a/reused-{hashlib.sha256(b'same').hexdigest()}.jpg"

    class FakeStorage:
        def __init__(self):
            self.objects = {reused_key: b"same"}
            self.deleted = []

        def stat(self, key):
            if key not in self.objects:
                raise StorageNotFound(key)
            return ObjectStat(key, len(self.objects[key]))

        def open(self, key): return io.BytesIO(self.objects[key])

        def put_file(self, key, source, *, overwrite=True):
            if "failure-" in key:
                raise StorageUnavailable("third upload failed")
            self.objects[key] = source.read_bytes()
            return self.stat(key)

        def delete(self, key, *, missing_ok=True):
            self.deleted.append(key)
            self.objects.pop(key, None)

    storage = FakeStorage()
    monkeypatch.setattr(image_module, "asset_storage", lambda: storage)
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: prepared)
    monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())

    with pytest.raises(StorageUnavailable, match="third upload failed"):
        service.refresh_movie_metadata_strict(movie, detail)

    created_key = f"movies/a/created-{hashlib.sha256(b'new').hexdigest()}.jpg"
    assert storage.deleted == []
    assert storage.objects == {reused_key: b"same", created_key: b"new"}
    assert all(not item.temp_root.exists() for item in prepared)


def test_strict_refresh_db_rollback_preserves_published_immutable_objects(test_db, monkeypatch, tmp_path):
    from src.service.catalog import movie_image_service as image_module
    from src.storage.types import ObjectStat, StorageNotFound

    movie = _create_local_movie(
        javdb_id="javdb-rollback", movie_number="ROLLBACK-001", title="old", summary="old"
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    task, path, root = _prepared_file(tmp_path, "created", b"new")
    prepared = [PreparedImageFile(task, path, root)]

    class FakeStorage:
        def __init__(self): self.objects = {}; self.deleted = []
        def stat(self, key):
            if key not in self.objects: raise StorageNotFound(key)
            return ObjectStat(key, len(self.objects[key]))
        def open(self, key): return io.BytesIO(self.objects[key])
        def put_file(self, key, source, *, overwrite=True):
            self.objects[key] = source.read_bytes()
            return self.stat(key)
        def delete(self, key, *, missing_ok=True):
            self.deleted.append(key); self.objects.pop(key, None)

    storage = FakeStorage()
    monkeypatch.setattr(image_module, "asset_storage", lambda: storage)
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: prepared)
    monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())

    def fail_records(**kwargs):
        Movie.update(watched_count=999).where(Movie.id == movie.id).execute()
        raise RuntimeError("database write failed")

    monkeypatch.setattr(service, "_refresh_movie_metadata_records_strict", fail_records)

    with pytest.raises(RuntimeError, match="database write failed"):
        service.refresh_movie_metadata_strict(movie, detail)

    created_key = f"movies/a/created-{hashlib.sha256(b'new').hexdigest()}.jpg"
    assert storage.deleted == []
    assert storage.objects == {created_key: b"new"}
    assert Movie.get_by_id(movie.id).watched_count == 0


def test_strict_refresh_rollback_never_deletes_shared_content_addressed_key(
    test_db, monkeypatch, tmp_path
):
    from src.service.catalog import movie_image_service as image_module
    from src.storage.types import ObjectStat, StorageNotFound

    movie = _create_local_movie(
        javdb_id="javdb-shared", movie_number="SHARED-001", title="old", summary="old"
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    task, path, root = _prepared_file(tmp_path, "shared", b"same-content")
    prepared = [PreparedImageFile(task, path, root)]
    deleted = []
    objects = {}

    class FakeStorage:
        def stat(self, key):
            if key not in objects: raise StorageNotFound(key)
            return ObjectStat(key, len(objects[key]))
        def open(self, key): return io.BytesIO(objects[key])
        def put_file(self, key, source, *, overwrite=True):
            objects[key] = source.read_bytes()
            # A concurrent refresh commits a reference to the same immutable key.
            image = Image.create(origin=key, small=key, medium=key, large=key)
            Actor.create(javdb_id="concurrent-actor", name="actor", profile_image=image)
            return self.stat(key)
        def delete(self, key, *, missing_ok=True): deleted.append(key); objects.pop(key, None)

    monkeypatch.setattr(image_module, "asset_storage", lambda: FakeStorage())
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: prepared)
    monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())
    monkeypatch.setattr(
        service,
        "_refresh_movie_metadata_records_strict",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database write failed")),
    )

    with pytest.raises(RuntimeError, match="database write failed"):
        service.refresh_movie_metadata_strict(movie, detail)

    assert deleted == []
    assert len(objects) == 1


def test_strict_refresh_post_commit_storage_cleanup_failure_is_nonfatal(test_db, monkeypatch):
    movie = _create_local_movie(
        javdb_id="javdb-cleanup", movie_number="CLEAN-001", title="old", summary="old"
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: [])
    monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())
    monkeypatch.setattr(
        service,
        "_refresh_movie_metadata_records_strict",
        lambda **kwargs: (Movie.update(watched_count=777).where(Movie.id == movie.id).execute() and Movie.get_by_id(movie.id), {"obsolete.jpg"}, []),
    )
    monkeypatch.setattr(
        service.image_service,
        "delete_obsolete_image_files",
        lambda paths: (_ for _ in ()).throw(StorageUnavailable("WebDAV down")),
    )

    result = service.refresh_movie_metadata_strict(movie, detail)

    assert result.watched_count == 777
    assert Movie.get_by_id(movie.id).watched_count == 777


@pytest.mark.parametrize("failure_stage", ["thin-cover", "hashing"])
def test_strict_refresh_preparation_failure_cleans_temporary_files(
    test_db, monkeypatch, tmp_path, failure_stage
):
    movie = _create_local_movie(
        javdb_id=f"javdb-{failure_stage}",
        movie_number=f"PREP-{failure_stage}",
        title="old",
        summary="old",
    )
    detail = _build_detail(
        javdb_id=movie.javdb_id, movie_number=movie.movie_number, title="new", summary="new"
    )
    task, path, root = _prepared_file(tmp_path, failure_stage, b"image")
    prepared = [PreparedImageFile(task, path, root)]
    service = CatalogImportService()
    monkeypatch.setattr(service.image_service, "build_movie_import_image_tasks", lambda *args: (None, [], {}))
    monkeypatch.setattr(service.image_service, "collect_image_tasks", lambda *args: [])
    monkeypatch.setattr(service.image_service, "download_image_tasks_to_temporary_files", lambda tasks: prepared)
    if failure_stage == "thin-cover":
        monkeypatch.setattr(
            service.image_service,
            "resolve_thin_cover_from_prepared_images",
            lambda *args: (_ for _ in ()).throw(RuntimeError("generation failed")),
        )
    else:
        monkeypatch.setattr(service.image_service, "resolve_thin_cover_from_prepared_images", lambda *args: ThinCoverResolution())
        monkeypatch.setattr(
            service.image_service,
            "version_prepared_image_keys",
            lambda files: (_ for _ in ()).throw(RuntimeError("hashing failed")),
        )

    with pytest.raises(RuntimeError, match="failed"):
        service.refresh_movie_metadata_strict(movie, detail)

    assert not root.exists()


def test_update_movie_fields_updates_only_specified_fields_on_existing_movie(test_db):
    """已存在影片：只更新指定字段，其余字段保持本地值。"""
    _create_local_movie(
        javdb_id="javdb-ABP-789",
        movie_number="ABP-789",
        title="本地标题",
        summary="本地描述",
        score=1.0,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-789",
        movie_number="ABP-789",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("score", "watched_count"),
    )

    assert created is False
    assert updated_fields == ("score", "watched_count")
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.score == 9.5
    assert refreshed.watched_count == 100
    # 未指定字段保持本地值。
    assert refreshed.title == "本地标题"
    assert refreshed.summary == "本地描述"
    assert refreshed.want_watch_count == 0


def test_update_movie_fields_creates_missing_movie_before_updating(test_db):
    """不存在影片：先完整导入，再应用指定字段。"""
    detail = _build_detail(
        javdb_id="javdb-ABP-000",
        movie_number="ABP-000",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("comment_count",),
    )

    assert created is True
    # 完整导入已写入 detail 取值，变更检测判定无字段再变化。
    assert updated_fields == ()
    refreshed = Movie.get_by_id(movie.id)
    # 完整导入已写入全部字段。
    assert refreshed.title == "JavDB标题"
    assert refreshed.comment_count == 5


def test_update_movie_fields_skips_unchanged_values(test_db):
    """值无变化的字段不写库，updated_fields 为空（updated/unchanged 计数语义护栏）。"""
    _create_local_movie(
        javdb_id="javdb-ABP-002",
        movie_number="ABP-002",
        title="本地标题",
        summary="本地描述",
        score=9.5,
    )
    detail = _build_detail(
        javdb_id="javdb-ABP-002",
        movie_number="ABP-002",
        title="JavDB标题",
        summary="JavDB描述",
    )

    movie, created, updated_fields = CatalogImportService().update_movie_fields(
        detail,
        ("score", "title"),
    )

    assert created is False
    # score 与本地一致跳过；title 不同则写入。
    assert updated_fields == ("title",)
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.score == 9.5
    assert refreshed.title == "JavDB标题"
    assert refreshed.summary == "本地描述"


def test_update_movie_fields_rejects_fields_outside_whitelist(test_db):
    detail = _build_detail(
        javdb_id="javdb-ABP-001",
        movie_number="ABP-001",
        title="JavDB标题",
        summary="JavDB描述",
    )

    with pytest.raises(ValueError, match="不支持的字段"):
        CatalogImportService().update_movie_fields(detail, ("heat",))

    with pytest.raises(ValueError, match="fields 不能为空"):
        CatalogImportService().update_movie_fields(detail, ())
