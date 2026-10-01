from threading import Event, Lock

import pytest

from src.storage.batch import publish_batch
from src.storage.types import StoragePublicationUnknown, StorageUnavailable


def test_batch_stops_submitting_and_drains_later_success(monkeypatch):
    from src.storage import batch as module

    later_started = Event()
    failure_observed = Event()
    calls = []
    original_wait = module.wait

    def wait_for_completion(*args, **kwargs):
        completed, pending = original_wait(*args, **kwargs)
        if any(future.exception() is not None for future in completed):
            failure_observed.set()
        return completed, pending

    monkeypatch.setattr(module, "wait", wait_for_completion)

    def publish(item):
        calls.append(item)
        if item == 0:
            assert later_started.wait(5)
            raise StorageUnavailable("failed")
        assert item == 1, "unstarted work must not be submitted after failure"
        later_started.set()
        assert failure_observed.wait(5)
        return "uploaded"

    batch = publish_batch(
        range(20), publish, max_workers=2, thread_name_prefix="test-publication",
    )
    assert sorted(calls) == [0, 1]
    assert batch.published == [(1, "uploaded")]
    assert len(batch.errors) == 1
    with pytest.raises(StorageUnavailable, match="failed"):
        batch.raise_for_errors()


def test_batch_prioritizes_unknown_outcome_without_losing_other_errors():
    second_started = Event()
    ordinary_error = StorageUnavailable("offline")
    unknown_error = StoragePublicationUnknown("unknown.webp", "unknown")

    def publish(item):
        if item == 0:
            assert second_started.wait(5)
            raise ordinary_error
        second_started.set()
        raise unknown_error

    batch = publish_batch(
        [0, 1], publish, max_workers=2, thread_name_prefix="test-publication",
    )
    assert {error for _, error in batch.errors} == {ordinary_error, unknown_error}
    with pytest.raises(StoragePublicationUnknown) as caught:
        batch.raise_for_errors()
    assert caught.value is unknown_error


def test_batch_never_has_more_than_max_workers_in_flight():
    lock = Lock()
    all_started = Event()
    active = maximum_active = 0

    def publish(item):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 3:
                all_started.set()
        assert all_started.wait(5)
        with lock:
            active -= 1
        return item * 2

    batch = publish_batch(
        range(30), publish, max_workers=3, thread_name_prefix="test-publication",
    )
    batch.raise_for_errors()
    assert maximum_active == 3
    assert sorted(batch.published) == [(item, item * 2) for item in range(30)]


def test_empty_batch_does_not_invoke_publisher():
    batch = publish_batch(
        [], lambda item: pytest.fail("unexpected publication"),
        max_workers=2, thread_name_prefix="test-publication",
    )
    assert batch.published == batch.errors == []
    batch.raise_for_errors()


def test_batch_rejects_invalid_worker_count():
    with pytest.raises(ValueError):
        publish_batch([], lambda item: item, max_workers=0, thread_name_prefix="test")


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_can_continue_after_individual_failures(workers):
    from threading import get_ident

    coordinator = get_ident()
    completed = []

    def publish(item):
        if item in {1, 3, 7}:
            raise StorageUnavailable(f"failed {item}")
        return item * 2

    def report(item, result, error):
        assert get_ident() == coordinator
        completed.append((item, result, error))

    batch = publish_batch(
        range(10), publish, max_workers=workers, thread_name_prefix="test",
        stop_on_error=False, on_complete=report,
    )
    assert sorted(item for item, _, _ in completed) == list(range(10))
    assert {item for item, _ in batch.errors} == {1, 3, 7}
    assert sorted(batch.published) == [(item, item * 2) for item in range(10) if item not in {1, 3, 7}]


def test_completion_callback_failure_stops_scheduling():
    attempted = []

    def publish(item):
        attempted.append(item)
        return item

    def broken_callback(*_args):
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        publish_batch(
            range(5), publish, max_workers=1, thread_name_prefix="test",
            stop_on_error=False, on_complete=broken_callback,
        )
    assert attempted == [0]


def test_each_upload_inherits_an_independent_task_context():
    from contextvars import ContextVar

    from loguru import logger

    marker = ContextVar("upload_test_marker", default="missing")
    token = marker.set("caller")
    records = []
    sink = logger.add(lambda message: records.append(message.record), filter=lambda record: record["message"] == "upload context")
    try:
        def publish(item):
            assert marker.get() == "caller"
            marker.set(str(item))
            logger.info("upload context")
            return item

        with logger.contextualize(task="test-thumbnail-publication"):
            publish_batch(range(5), publish, max_workers=2, thread_name_prefix="test").raise_for_errors()
        assert marker.get() == "caller"
        assert len(records) == 5
        assert all(record["extra"]["task"] == "test-thumbnail-publication" for record in records)
    finally:
        logger.remove(sink)
        marker.reset(token)
