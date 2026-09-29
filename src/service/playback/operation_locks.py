"""Short-lived ownership of media I/O and library configuration/inventory."""

import time
from contextlib import contextmanager

from src.api.exception.errors import ApiError
from src.model import get_database

MEDIA_LOCK = 17001
LIBRARY_LOCK = 17002
SUBTITLE_LOCK = 17003
CLIP_LOCK = 17004


class MediaOperationBusy(ApiError):
    def __init__(self):
        super().__init__(
            409, "media_operation_busy", "媒体或媒体库正在处理，请稍后重试"
        )


class SubtitleOperationBusy(ApiError):
    retryable = True

    def __init__(self):
        super().__init__(
            409, "subtitle_operation_busy", "该影片字幕正在处理，请稍后重试"
        )


@contextmanager
def media_operation_lock(namespace: int, resource_id: int):
    with _operation_lock(namespace, resource_id, MediaOperationBusy) as check_connection:
        yield check_connection


@contextmanager
def subtitle_operation_lock(movie_id: int, *, timeout_seconds: float = 10.0):
    with _operation_lock(
        SUBTITLE_LOCK, movie_id, SubtitleOperationBusy,
        timeout_seconds=timeout_seconds,
    ) as check_connection:
        yield check_connection


@contextmanager
def _operation_lock(namespace, resource_id, busy_error, *, timeout_seconds=0.0):
    # Resource ids use PostgreSQL serial (signed int32), with separate namespaces.
    if not 0 < resource_id < 2**31:
        raise ValueError("invalid media operation lock id")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    database = get_database()
    with database.pinned_connection() as connection:

        def check_connection():
            if (
                connection.closed
                or database.is_closed()
                or database.connection() is not connection
            ):
                raise RuntimeError("media_operation_connection_lost")
            with database.cursor() as cursor:
                cursor.execute("SELECT 1")

        while True:
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)", (namespace, resource_id)
                )
                if cursor.fetchone()[0]:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise busy_error()
            time.sleep(min(0.05, remaining))
            check_connection()
        try:
            yield check_connection
        finally:
            # Release on the original session only, even when the body failed.
            if not connection.closed:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (namespace, resource_id),
                        )
                except Exception:
                    connection.close()
