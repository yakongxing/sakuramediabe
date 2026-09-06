from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import Actor, BackgroundTaskRun, Image, Movie
from src.service.catalog.image_publication_service import ImagePublicationService
from src.service.catalog.movie_image_service import (
    ImagePersistTask,
    PreparedImageFile,
    ThinCoverResolution,
)


def _publication_detail(movie: Movie) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=movie.javdb_id,
        movie_number=movie.movie_number,
        title=movie.title,
        summary="summary",
        duration_minutes=120,
        release_date="2024-01-01",
        score=9,
        score_number=1,
        watched_count=1,
        want_watch_count=1,
        comment_count=1,
        actors=[],
        tags=[],
    )


def _cover_publication_params(stage: Path, movie: Movie, operation: str) -> dict:
    key = "movies/replay/cover-content-addressed.jpg"
    (stage / "cover.jpg").write_bytes(b"same-image")
    return {
        "operation_id": "same-operation",
        "movie_id": movie.id,
        "detail": _publication_detail(movie).model_dump(mode="json"),
        "staging_dir": str(stage),
        "attempt": 0,
        "operation": operation,
        "thin_cover_key": None,
        "thin_cover_plot_index": None,
        "items": [
            {
                "image_type": "cover",
                "image_url": "https://example.invalid/cover.jpg",
                "relative_path": key,
                "plot_index": None,
                "actor_javdb_id": None,
                "staged_name": "cover.jpg",
            }
        ],
    }


@pytest.mark.parametrize("operation", ["refresh", "import"])
def test_successful_manifest_replay_keeps_every_current_object(
    test_db, monkeypatch, tmp_path, operation
):
    from src.service.catalog import image_cleanup_service as cleanup_module
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(
        javdb_id=f"replay-{operation}", movie_number="R-1", title="movie"
    )
    stage = ImagePublicationService.create_staging_dir()
    params = _cover_publication_params(stage, movie, operation)
    objects: set[str] = set()

    class Storage:
        def delete(self, key, *, missing_ok):
            objects.discard(key)

    def publish(_self, prepared, **_kwargs):
        objects.update(item.image_task.relative_path for item in prepared)

    monkeypatch.setattr(
        module.MovieImageService, "finalize_prepared_image_files", publish
    )
    monkeypatch.setattr(cleanup_module, "asset_storage", lambda: Storage())
    monkeypatch.setattr(module.shutil, "rmtree", lambda *args, **kwargs: None)

    ImagePublicationService.execute(None, params)
    ImagePublicationService.execute(None, params)

    current = Movie.get_by_id(movie.id).cover_image
    assert current is not None
    assert current.origin in objects


@pytest.mark.parametrize("operation", ["refresh", "import"])
def test_outer_transaction_failure_preserves_old_refs_and_objects(
    test_db, monkeypatch, tmp_path, operation
):
    from src.service.catalog import image_publication_service as module
    from src.service.catalog.catalog_import_service import CatalogImportService

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    monkeypatch.setattr(module.settings.storage, "image_publication_retry_limit", 1)
    old_key = f"movies/{operation}/old.jpg"
    old_image = Image.create(
        origin=old_key, small=old_key, medium=old_key, large=old_key
    )
    movie = Movie.create(
        javdb_id=f"rollback-{operation}",
        movie_number="RB-1",
        title="movie",
        cover_image=old_image,
    )
    stage = ImagePublicationService.create_staging_dir()
    params = _cover_publication_params(stage, movie, operation)
    objects = {old_key}

    def publish(_self, prepared, **_kwargs):
        objects.update(item.image_task.relative_path for item in prepared)

    method_name = f"_complete_staged_{operation}"
    original = getattr(CatalogImportService, method_name)

    def fail_after_completion(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("outer transaction failure")

    monkeypatch.setattr(
        module.MovieImageService, "finalize_prepared_image_files", publish
    )
    monkeypatch.setattr(CatalogImportService, method_name, fail_after_completion)
    monkeypatch.setattr(
        module.TaskQueueService, "enqueue", lambda **kwargs: SimpleNamespace(id=1)
    )

    with pytest.raises(RuntimeError, match="outer transaction failure"):
        ImagePublicationService.execute(None, params)

    assert Movie.get_by_id(movie.id).cover_image_id == old_image.id
    assert old_key in objects


def test_enqueue_refresh_persists_manifest_before_durable_queue_handoff(
    test_db, monkeypatch, tmp_path
):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(
        javdb_id="enqueue-manifest", movie_number="EM-001", title="movie"
    )
    stage = ImagePublicationService.create_staging_dir()
    staged = stage / "cover.jpg"
    staged.write_bytes(b"image")
    task = ImagePersistTask(
        "cover", "https://invalid/cover.jpg", "movies/a/cover-hash.jpg", Path("/unused")
    )
    prepared = PreparedImageFile(task, staged, stage)
    observed = {}

    def enqueue(**kwargs):
        observed.update(kwargs)
        assert (stage / "manifest.json").is_file()
        return SimpleNamespace(id=17)

    monkeypatch.setattr(module.TaskQueueService, "enqueue", enqueue)
    detail = SimpleNamespace(model_dump=lambda mode: {"movie_number": "A"})

    run = ImagePublicationService.enqueue_refresh(
        movie_id=movie.id,
        detail=detail,
        prepared_files=[prepared],
        thin_cover_resolution=ThinCoverResolution(),
    )

    assert run.id == 17
    assert observed["serialized"] is False
    assert observed["params"]["staging_dir"] == str(stage.resolve())


def test_failed_publication_keeps_stage_and_enqueues_observable_retry(
    monkeypatch, tmp_path
):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    monkeypatch.setattr(module.settings.storage, "image_publication_retry_limit", 2)
    stage = ImagePublicationService.create_staging_dir()
    (stage / "cover.jpg").write_bytes(b"image")
    params = {
        "movie_id": 3,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "thin_cover_key": None,
        "thin_cover_plot_index": None,
        "items": [
            {
                "image_type": "cover",
                "image_url": "u",
                "relative_path": "cover-hash.jpg",
                "plot_index": None,
                "staged_name": "cover.jpg",
            }
        ],
    }
    monkeypatch.setattr(
        module.JavdbMovieDetailResource,
        "model_validate",
        lambda value: SimpleNamespace(),
    )
    monkeypatch.setattr(
        module.MovieImageService,
        "finalize_prepared_image_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    retries = []
    monkeypatch.setattr(
        module.TaskQueueService,
        "enqueue",
        lambda **kwargs: retries.append(kwargs) or SimpleNamespace(id=18),
    )

    with pytest.raises(RuntimeError, match="offline"):
        ImagePublicationService.execute(None, params)

    assert stage.is_dir()
    assert retries[0]["params"]["attempt"] == 1
    assert retries[0]["trigger_type"] == "internal"
    assert retries[0]["scheduled_at"] > module.utc_now_for_db()


def test_enqueue_persists_actor_identity_independent_of_shared_url(
    test_db, monkeypatch, tmp_path
):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(
        javdb_id="enqueue-actor-identity", movie_number="EAI-001", title="movie"
    )
    stage = ImagePublicationService.create_staging_dir()
    shared_url = "https://example.invalid/shared.jpg"
    tasks = {
        "actor-a": ImagePersistTask("actor", shared_url, "a.jpg", Path("/unused")),
        "actor-b": ImagePersistTask("actor", shared_url, "b.jpg", Path("/unused")),
    }
    prepared = []
    for actor_id, task in tasks.items():
        path = stage / f"{actor_id}.jpg"
        path.write_bytes(actor_id.encode())
        prepared.append(PreparedImageFile(task, path, stage))
    captured = {}
    monkeypatch.setattr(
        module.TaskQueueService,
        "enqueue",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(id=19),
    )

    ImagePublicationService.enqueue_refresh(
        movie_id=movie.id,
        detail=SimpleNamespace(model_dump=lambda mode: {}),
        prepared_files=prepared,
        thin_cover_resolution=ThinCoverResolution(),
        actor_image_tasks_by_javdb_id=tasks,
    )

    assert [item["actor_javdb_id"] for item in captured["params"]["items"]] == [
        "actor-a",
        "actor-b",
    ]


def test_worker_uses_persisted_actor_ids_and_switches_refs_only_after_verification(
    test_db, monkeypatch, tmp_path
):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(javdb_id="worker-import", movie_number="WI-001", title="movie")
    actor_a = Actor.create(javdb_id="actor-a", name="A")
    actor_b = Actor.create(javdb_id="actor-b", name="B")
    shared_url = "https://example.invalid/shared.jpg"
    detail = SimpleNamespace(
        actors=[
            JavdbMovieActorResource(
                javdb_id="actor-a", name="A", avatar_url=shared_url
            ),
            JavdbMovieActorResource(
                javdb_id="actor-b", name="B", avatar_url=shared_url
            ),
        ]
    )
    monkeypatch.setattr(
        module.JavdbMovieDetailResource, "model_validate", lambda value: detail
    )
    stage = ImagePublicationService.create_staging_dir()
    (stage / "a.jpg").write_bytes(b"a")
    (stage / "b.jpg").write_bytes(b"b")
    params = {
        "movie_id": movie.id,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "operation": "import",
        "thin_cover_key": None,
        "thin_cover_plot_index": None,
        "items": [
            {
                "image_type": "actor",
                "image_url": shared_url,
                "relative_path": "actor-a.jpg",
                "plot_index": None,
                "actor_javdb_id": "actor-a",
                "staged_name": "a.jpg",
            },
            {
                "image_type": "actor",
                "image_url": shared_url,
                "relative_path": "actor-b.jpg",
                "plot_index": None,
                "actor_javdb_id": "actor-b",
                "staged_name": "b.jpg",
            },
        ],
    }

    def verified(self, prepared, **kwargs):
        assert Actor.get_by_id(actor_a.id).profile_image_id is None
        assert Actor.get_by_id(actor_b.id).profile_image_id is None

    monkeypatch.setattr(
        module.MovieImageService, "finalize_prepared_image_files", verified
    )

    result = ImagePublicationService.execute(None, params)

    assert result["published"] == 2
    assert (
        Image.get_by_id(Actor.get_by_id(actor_a.id).profile_image_id).origin
        == "actor-a.jpg"
    )
    assert (
        Image.get_by_id(Actor.get_by_id(actor_b.id).profile_image_id).origin
        == "actor-b.jpg"
    )


def test_recovery_enqueues_valid_orphan_manifest(test_db, monkeypatch, tmp_path):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(javdb_id="orphan", movie_number="OR-1", title="orphan")
    stage = ImagePublicationService.create_staging_dir()
    (stage / "cover.jpg").write_bytes(b"image")
    params = {
        "operation_id": "orphan-operation",
        "movie_id": movie.id,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "items": [{"staged_name": "cover.jpg"}],
    }
    (stage / "manifest.json").write_text(
        __import__("json").dumps(params), encoding="utf-8"
    )

    result = ImagePublicationService.recover_interrupted()

    assert result["requeued_image_publications"] == 1
    queued = BackgroundTaskRun.get(BackgroundTaskRun.task_key == "image_publication")
    assert queued.params["operation_id"] == "orphan-operation"
    assert (
        ImagePublicationService.recover_interrupted()["requeued_image_publications"]
        == 0
    )


def test_worker_startup_recovers_orphan_without_expired_row_and_cleans_stages(
    test_db, monkeypatch, tmp_path
):
    from src.scheduler.worker import TaskWorker
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    monkeypatch.setattr(
        module.settings.storage, "image_publication_failed_retention_seconds", 0
    )
    monkeypatch.setattr(
        module.settings.storage, "image_publication_invalid_stage_grace_seconds", 0
    )
    movie = Movie.create(javdb_id="startup-orphan", movie_number="OR-2", title="orphan")
    stage = ImagePublicationService.create_staging_dir()
    (stage / "cover.jpg").write_bytes(b"image")
    params = {
        "operation_id": "startup-orphan-operation",
        "movie_id": movie.id,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "items": [{"staged_name": "cover.jpg"}],
    }
    (stage / "manifest.json").write_text(
        __import__("json").dumps(params), encoding="utf-8"
    )
    terminal_stage = ImagePublicationService.create_staging_dir()
    (terminal_stage / "terminal-failure.json").write_text(
        '{"failed_at":"2000-01-01T00:00:00+00:00"}', encoding="utf-8"
    )
    invalid_stage = ImagePublicationService.create_staging_dir()

    assert BackgroundTaskRun.select().count() == 0
    monkeypatch.setattr(
        "src.scheduler.worker.threading.Thread.start", lambda self: None
    )

    TaskWorker(lanes={}).start()

    queued = BackgroundTaskRun.get(BackgroundTaskRun.task_key == "image_publication")
    assert queued.params["operation_id"] == "startup-orphan-operation"
    assert queued.state == "pending"
    assert not terminal_stage.exists()
    assert not invalid_stage.exists()


def test_periodic_housekeeping_recovers_orphan_without_expired_row(
    test_db, monkeypatch, tmp_path
):
    from src.scheduler.worker import TaskWorker
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(
        javdb_id="periodic-orphan", movie_number="OR-3", title="orphan"
    )
    stage = ImagePublicationService.create_staging_dir()
    (stage / "cover.jpg").write_bytes(b"image")
    params = {
        "operation_id": "periodic-orphan-operation",
        "movie_id": movie.id,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "items": [{"staged_name": "cover.jpg"}],
    }
    (stage / "manifest.json").write_text(
        __import__("json").dumps(params), encoding="utf-8"
    )

    assert BackgroundTaskRun.select().count() == 0
    TaskWorker(lanes={})._run_housekeeping_once()

    queued = BackgroundTaskRun.get(BackgroundTaskRun.task_key == "image_publication")
    assert queued.params["operation_id"] == "periodic-orphan-operation"


def test_concurrent_orphan_scans_enqueue_operation_once(test_db, monkeypatch, tmp_path):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    movie = Movie.create(
        javdb_id="concurrent-orphan", movie_number="OR-4", title="orphan"
    )
    stage = ImagePublicationService.create_staging_dir()
    (stage / "cover.jpg").write_bytes(b"image")
    params = {
        "operation_id": "concurrent-orphan-operation",
        "movie_id": movie.id,
        "detail": {},
        "staging_dir": str(stage),
        "attempt": 0,
        "items": [{"staged_name": "cover.jpg"}],
    }
    (stage / "manifest.json").write_text(
        __import__("json").dumps(params), encoding="utf-8"
    )
    barrier = Barrier(2)

    def recover() -> int:
        with test_db.connection_context():
            barrier.wait()
            return ImagePublicationService.recover_interrupted()[
                "requeued_image_publications"
            ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(recover), pool.submit(recover)]
        assert sorted(result.result() for result in results) == [0, 1]

    assert (
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.task_key == "image_publication")
        .count()
        == 1
    )


def test_terminal_failure_stage_has_bounded_retention(test_db, monkeypatch, tmp_path):
    from src.service.catalog import image_publication_service as module

    monkeypatch.setattr(
        module.settings.storage, "image_publication_staging_root", str(tmp_path)
    )
    monkeypatch.setattr(
        module.settings.storage, "image_publication_failed_retention_seconds", 60
    )
    stage = ImagePublicationService.create_staging_dir()
    marker = stage / "terminal-failure.json"
    marker.write_text('{"failed_at":"2000-01-01T00:00:00+00:00"}', encoding="utf-8")

    result = ImagePublicationService.recover_interrupted()

    assert result["cleaned_staging_dirs"] == 1
    assert not stage.exists()


def test_stale_operation_is_older_queue_generation(test_db):
    movie = Movie.create(javdb_id="ordering", movie_number="ORDER-1", title="order")
    old = {"operation_id": "old", "movie_id": movie.id}
    new = {"operation_id": "new", "movie_id": movie.id}
    BackgroundTaskRun.create(
        task_key="image_publication",
        task_name="old",
        trigger_type="internal",
        params=old,
    )
    BackgroundTaskRun.create(
        task_key="image_publication",
        task_name="new",
        trigger_type="internal",
        params=new,
    )

    assert ImagePublicationService._is_latest_operation(old) is False
    assert ImagePublicationService._is_latest_operation(new) is True
