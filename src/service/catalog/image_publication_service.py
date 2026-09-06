"""Durable hand-off and worker for WebDAV catalog image publication."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.config import settings
from src.metadata._providers.models import JavdbMovieDetailResource
from src.model import BackgroundTaskRun, Movie
from src.model.base import get_database
from src.service.catalog.movie_image_service import (
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.system.task_queue_service import (
    FAILURE_CODE_QUEUE_LEASE_EXPIRED,
    INTERNAL_FAILURE_CODE_KEY,
    TaskQueueService,
)

TASK_KEY = "image_publication"


class ImagePublicationService:
    @staticmethod
    def _generation_marker(movie_id: int) -> Path:
        root = (
            Path(settings.storage.image_publication_staging_root).expanduser().resolve()
        )
        return root / f".movie-{int(movie_id)}.generation"

    @classmethod
    def _write_generation_marker(cls, movie_id: int, operation_id: str) -> None:
        marker = cls._generation_marker(movie_id)
        temporary = marker.with_name(f"{marker.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="ascii") as stream:
            stream.write(operation_id)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        root_fd = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

    @staticmethod
    def create_staging_dir() -> Path:
        root = Path(settings.storage.image_publication_staging_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        path = root / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        return path

    @classmethod
    def enqueue_refresh(
        cls,
        *,
        movie_id: int,
        detail: JavdbMovieDetailResource,
        prepared_files: list[PreparedImageFile],
        thin_cover_resolution: ThinCoverResolution,
        actor_image_tasks_by_javdb_id: dict[str, ImagePersistTask] | None = None,
        operation: str = "refresh",
    ) -> BackgroundTaskRun:
        if not prepared_files:
            raise ValueError("image publication requires staged files")
        staging_dir = prepared_files[0].temp_root.resolve()
        items = []
        actor_ids_by_task = {
            id(task): actor_id
            for actor_id, task in (actor_image_tasks_by_javdb_id or {}).items()
        }
        for prepared in prepared_files:
            task = prepared.image_task
            items.append(
                {
                    "image_type": task.image_type,
                    "image_url": task.image_url,
                    "relative_path": task.relative_path,
                    "plot_index": task.plot_index,
                    "actor_javdb_id": actor_ids_by_task.get(id(task)),
                    "staged_name": prepared.temp_path.relative_to(
                        staging_dir
                    ).as_posix(),
                }
            )
        params = {
            "operation_id": uuid.uuid4().hex,
            "movie_id": movie_id,
            "detail": detail.model_dump(mode="json"),
            "staging_dir": str(staging_dir),
            "items": items,
            "thin_cover_key": (
                thin_cover_resolution.generated_task.relative_path
                if thin_cover_resolution.generated_task
                else None
            ),
            "thin_cover_plot_index": thin_cover_resolution.selected_plot_index,
            "attempt": 0,
            "operation": operation,
        }
        manifest_tmp = staging_dir / ".manifest.json.tmp"
        manifest = staging_dir / "manifest.json"
        with manifest_tmp.open("w", encoding="utf-8") as stream:
            json.dump(params, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(manifest_tmp, manifest)
        staging_fd = os.open(staging_dir, os.O_RDONLY)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        # Serialize generation registration against a worker's final DB switch.
        # The manifest exists first, so a crash/rollback leaves recoverable work.
        try:
            database = get_database()
        except RuntimeError:  # allows isolated manifest/queue unit tests
            database = None
        with database.atomic() if database is not None else nullcontext():
            if database is not None:
                Movie.select().where(Movie.id == movie_id).for_update().get()
            generation_marker = cls._generation_marker(movie_id)
            try:
                previous_generation = generation_marker.read_text(
                    encoding="ascii"
                ).strip()
            except FileNotFoundError:
                previous_generation = None
            cls._write_generation_marker(movie_id, params["operation_id"])
            # A startup scanner may have adopted this fsynced manifest while the
            # producer was waiting for the movie lock. Reuse that durable row.
            queued = None
            if database is not None:
                queued = next(
                    (
                        row
                        for row in BackgroundTaskRun.select().where(
                            BackgroundTaskRun.task_key == TASK_KEY,
                            BackgroundTaskRun.state.in_(("pending", "running")),
                        )
                        if (row.params or {}).get("operation_id")
                        == params["operation_id"]
                    ),
                    None,
                )
            if queued is None:
                try:
                    queued = TaskQueueService.enqueue(
                        task_key=TASK_KEY,
                        task_name="Publish catalog images",
                        trigger_type="internal",
                        params=params,
                        serialized=False,
                    )
                except Exception:
                    if previous_generation:
                        cls._write_generation_marker(movie_id, previous_generation)
                    else:
                        generation_marker.unlink(missing_ok=True)
                    raise
        if queued is None:  # unserialized work never coalesces
            raise RuntimeError("image publication was not queued")
        logger.info(
            "image_publication queued task_run_id={} movie_id={} files={} counters={}",
            queued.id,
            movie_id,
            len(items),
            {"queued": 1},
        )
        return queued

    @staticmethod
    def _validated_staging_dir(raw: str) -> Path:
        root = (
            Path(settings.storage.image_publication_staging_root).expanduser().resolve()
        )
        path = Path(raw).resolve()
        if path == root or root not in path.parents:
            raise ValueError("image publication staging path escapes configured root")
        return path

    @classmethod
    def execute(cls, _reporter, params: dict[str, Any]) -> dict[str, int]:
        staging_dir = cls._validated_staging_dir(str(params["staging_dir"]))
        attempt = int(params.get("attempt", 0))
        logger.info(
            "image_publication running movie_id={} attempt={} files={} counters={}",
            params["movie_id"],
            attempt,
            len(params["items"]),
            {"running": 1},
        )
        try:
            detail = JavdbMovieDetailResource.model_validate(params["detail"])
            tasks: list[ImagePersistTask] = []
            prepared_files: list[PreparedImageFile] = []
            by_key: dict[str, ImagePersistTask] = {}
            for item in params["items"]:
                task = ImagePersistTask(
                    image_type=item["image_type"],
                    image_url=item["image_url"],
                    relative_path=item["relative_path"],
                    absolute_path=Path("/unused"),
                    plot_index=item.get("plot_index"),
                )
                staged_path = (staging_dir / item["staged_name"]).resolve()
                if staging_dir not in staged_path.parents or not staged_path.is_file():
                    raise FileNotFoundError(
                        f"staged image missing: {item['staged_name']}"
                    )
                tasks.append(task)
                by_key[task.relative_path] = task
                prepared_files.append(PreparedImageFile(task, staged_path, staging_dir))
            image_service = MovieImageService()
            image_service.finalize_prepared_image_files(prepared_files, cleanup=False)
            from src.service.catalog.catalog_import_service import CatalogImportService

            cover = next((task for task in tasks if task.image_type == "cover"), None)
            plots = [task for task in tasks if task.image_type == "plot"]
            actors = {
                item["actor_javdb_id"]: task
                for item, task in zip(params["items"], tasks, strict=True)
                if item.get("actor_javdb_id")
            }
            thin = ThinCoverResolution(
                generated_task=by_key.get(params.get("thin_cover_key")),
                selected_plot_index=params.get("thin_cover_plot_index"),
            )
            service = CatalogImportService()
            complete = (
                service._complete_staged_import
                if params.get("operation", "refresh") == "import"
                else service._complete_staged_refresh
            )
            with get_database().atomic():
                movie = (
                    Movie.select()
                    .where(Movie.id == int(params["movie_id"]))
                    .for_update()
                    .get()
                )
                if not cls._is_latest_operation(params):
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    logger.info(
                        "image_publication skipped stale generation movie_id={} operation_id={}",
                        params["movie_id"],
                        params.get("operation_id"),
                    )
                    return {"published": 0, "attempt": attempt, "stale": 1}
                _, obsolete_paths, old_plot_image_ids = complete(
                    movie=movie,
                    detail=detail,
                    cover_task=cover,
                    plot_tasks=plots,
                    actor_image_tasks_by_javdb_id=actors,
                    thin_cover_resolution=thin,
                )
            # Nested atomics in the completion helpers are savepoints. Wait for
            # this outer transaction to commit before touching WebDAV/Qdrant.
            if get_database().in_transaction():
                raise RuntimeError(
                    "image publication cannot clean up inside an ambient transaction"
                )
            service._cleanup_staged_publication(obsolete_paths, old_plot_image_ids)
            shutil.rmtree(staging_dir, ignore_errors=True)
            logger.info(
                "image_publication succeeded movie_id={} counters={}",
                params["movie_id"],
                {"succeeded": 1},
            )
            return {"published": len(prepared_files), "attempt": attempt}
        except Exception:
            if attempt < settings.storage.image_publication_retry_limit:
                retry_params = {**params, "attempt": attempt + 1}
                TaskQueueService.enqueue(
                    task_key=TASK_KEY,
                    task_name="Retry catalog image publication",
                    trigger_type="internal",
                    params=retry_params,
                    serialized=False,
                    scheduled_at=utc_now_for_db()
                    + timedelta(seconds=min(300, 2**attempt * 5)),
                )
                logger.warning(
                    "image_publication retry queued movie_id={} attempt={} counters={}",
                    params["movie_id"],
                    attempt + 1,
                    {"failed": 1, "retried": 1},
                )
            else:
                cls._mark_terminal_failure(staging_dir, params)
                logger.exception(
                    "image_publication failed movie_id={} attempt={} counters={}",
                    params["movie_id"],
                    attempt,
                    {"failed": 1},
                )
            raise

    @classmethod
    def _is_latest_operation(cls, params: dict[str, Any]) -> bool:
        operation_id = params.get("operation_id")
        if not operation_id:  # migration compatibility for already queued work
            return True
        movie_id = int(params["movie_id"])
        marker = cls._generation_marker(movie_id)
        try:
            return marker.read_text(encoding="ascii").strip() == operation_id
        except FileNotFoundError:
            # Compatibility for journals produced before generation markers.
            pass
        rows = (
            BackgroundTaskRun.select(BackgroundTaskRun.params)
            .where(BackgroundTaskRun.task_key == TASK_KEY)
            .order_by(BackgroundTaskRun.id.desc())
        )
        for row in rows:
            candidate = row.params or {}
            if int(candidate.get("movie_id", -1)) == movie_id:
                return candidate.get("operation_id") == operation_id
        return True

    @staticmethod
    def _mark_terminal_failure(staging: Path, params: dict[str, Any]) -> None:
        marker = staging / "terminal-failure.json"
        temporary = staging / ".terminal-failure.json.tmp"
        payload = {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "operation_id": params.get("operation_id"),
            "attempt": params.get("attempt", 0),
        }
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)

    @classmethod
    def recover_interrupted(cls) -> dict[str, int]:
        recovered = 0
        rows = BackgroundTaskRun.select().where(
            BackgroundTaskRun.task_key == TASK_KEY,
            BackgroundTaskRun.state == "failed",
        )
        for row in rows:
            params = row.params or {}
            if (row.result_summary or {}).get(
                INTERNAL_FAILURE_CODE_KEY
            ) != FAILURE_CODE_QUEUE_LEASE_EXPIRED:
                continue
            if (
                int(params.get("attempt", 0))
                >= settings.storage.image_publication_retry_limit
            ):
                continue
            try:
                staging = cls._validated_staging_dir(str(params["staging_dir"]))
            except Exception as exc:
                logger.warning(
                    "image_publication recovery skipped invalid stage task_run_id={} detail={}",
                    row.id,
                    exc,
                )
                continue
            if not staging.exists():
                continue
            duplicate = (
                BackgroundTaskRun.select()
                .where(
                    BackgroundTaskRun.task_key == TASK_KEY,
                    BackgroundTaskRun.state.in_(("pending", "running")),
                    BackgroundTaskRun.params == params,
                )
                .exists()
            )
            if duplicate:
                continue
            TaskQueueService.enqueue(
                task_key=TASK_KEY,
                task_name="Recover catalog image publication",
                trigger_type="internal",
                params=params,
                serialized=False,
            )
            recovered += 1
        # Reconcile the filesystem journal too. This closes the crash window
        # after manifest fsync but before the PostgreSQL queue insert commits.
        root = (
            Path(settings.storage.image_publication_staging_root).expanduser().resolve()
        )
        root.mkdir(parents=True, exist_ok=True)
        cleaned = 0
        now = datetime.now(timezone.utc)
        for staging in sorted(root.iterdir(), key=lambda path: path.name):
            if not staging.is_dir():
                continue
            age = max(0.0, now.timestamp() - staging.stat().st_mtime)
            terminal = staging / "terminal-failure.json"
            if terminal.is_file():
                try:
                    failed_at = datetime.fromisoformat(
                        json.loads(terminal.read_text(encoding="utf-8"))["failed_at"]
                    )
                    if failed_at.tzinfo is None:
                        failed_at = failed_at.replace(tzinfo=timezone.utc)
                    age = (now - failed_at.astimezone(timezone.utc)).total_seconds()
                except Exception:
                    pass
                if age >= settings.storage.image_publication_failed_retention_seconds:
                    shutil.rmtree(staging, ignore_errors=True)
                    cleaned += 1
                continue
            try:
                params = json.loads(
                    (staging / "manifest.json").read_text(encoding="utf-8")
                )
                if (
                    cls._validated_staging_dir(str(params["staging_dir"]))
                    != staging.resolve()
                ):
                    raise ValueError("manifest staging path mismatch")
                if not isinstance(params.get("items"), list) or not params["items"]:
                    raise ValueError("manifest has no items")
                for item in params["items"]:
                    candidate = (staging / item["staged_name"]).resolve()
                    if (
                        staging.resolve() not in candidate.parents
                        or not candidate.is_file()
                    ):
                        raise ValueError("manifest staged item is invalid")
                if (
                    not Movie.select()
                    .where(Movie.id == int(params["movie_id"]))
                    .exists()
                ):
                    raise ValueError("manifest movie does not exist")
            except Exception as exc:
                if (
                    age
                    >= settings.storage.image_publication_invalid_stage_grace_seconds
                ):
                    shutil.rmtree(staging, ignore_errors=True)
                    cleaned += 1
                else:
                    logger.warning(
                        "image_publication retained invalid stage path={} detail={}",
                        staging,
                        exc,
                    )
                continue
            operation_id = params.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                if (
                    age
                    >= settings.storage.image_publication_invalid_stage_grace_seconds
                ):
                    shutil.rmtree(staging, ignore_errors=True)
                    cleaned += 1
                else:
                    logger.warning(
                        "image_publication retained manifest without operation identity path={}",
                        staging,
                    )
                continue
            # Serialize orphan adoption with both producers and other recovery
            # processes using the same per-movie row lock as enqueue_refresh.
            with get_database().atomic():
                movie = (
                    Movie.select()
                    .where(Movie.id == int(params["movie_id"]))
                    .for_update()
                    .first()
                )
                if movie is None:
                    if (
                        age
                        >= settings.storage.image_publication_invalid_stage_grace_seconds
                    ):
                        shutil.rmtree(staging, ignore_errors=True)
                        cleaned += 1
                    continue
                active = any(
                    (row.params or {}).get("operation_id") == operation_id
                    for row in BackgroundTaskRun.select(BackgroundTaskRun.params).where(
                        BackgroundTaskRun.task_key == TASK_KEY,
                        BackgroundTaskRun.state.in_(("pending", "running")),
                    )
                )
                if active:
                    continue
                marker = cls._generation_marker(int(params["movie_id"]))
                try:
                    current_operation = marker.read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    current_operation = ""
                if current_operation and current_operation != operation_id:
                    # A later generation was durably registered. This orphan
                    # can never be allowed to make references stale again.
                    shutil.rmtree(staging, ignore_errors=True)
                    cleaned += 1
                    continue
                cls._write_generation_marker(int(params["movie_id"]), operation_id)
                queued = TaskQueueService.enqueue(
                    task_key=TASK_KEY,
                    task_name="Recover orphan catalog image publication",
                    trigger_type="internal",
                    params=params,
                    serialized=False,
                )
            if queued is not None:
                recovered += 1
        return {
            "requeued_image_publications": recovered,
            "cleaned_staging_dirs": cleaned,
        }
