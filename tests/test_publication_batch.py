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
