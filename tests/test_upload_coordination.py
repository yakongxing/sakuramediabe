"""Resource bounds and lifecycle checks independent of a live DAV service."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, current_thread

import httpx
import pytest

from src.config.config import Storage
from src.storage.local import LocalStorageBackend
from src.storage.types import ObjectStat, PublicationResult, StorageUnavailable
from src.storage.upload import KeyedLocks, PublicationBudget, upload_limiter
from src.storage.webdav import WebDAVStorageBackend


def test_key_locks_are_reclaimed_after_distinct_publications():
    locks = KeyedLocks()
    for index in range(2000):
        with locks.hold(str(index)):
            assert len(locks) == 1
    assert len(locks) == 0


def test_waiter_keeps_the_same_key_lock_alive():
    locks = KeyedLocks()
    registered, acquired, release = Event(), Event(), Event()

    class WaitingBudget:
        def remaining(self):
            registered.set()
            return 5

    def waiter():
        with locks.hold("same", WaitingBudget()):
            acquired.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with locks.hold("same"):
                future = executor.submit(waiter)
                assert registered.wait(5)
                assert not acquired.is_set()
                assert len(locks) == 1
            assert acquired.wait(5)
            assert len(locks) == 1
            with pytest.raises(StorageUnavailable), locks.hold("same", PublicationBudget(0.01)):
                pytest.fail("a second lock admitted the same key")
            assert len(locks) == 1
        finally:
            release.set()
        future.result(timeout=5)
    assert len(locks) == 0


def test_endpoint_limits_are_shared_and_configuration_changes_drain_safely():
    first = upload_limiter(("test-capacity", "user"), 2)
    second = upload_limiter(("test-capacity", "user"), 1)
    unrelated = upload_limiter(("other-capacity", "user"), 1)
    try:
        assert first.semaphore is second.semaphore
        assert first.semaphore.acquire(blocking=False)
        assert not second.semaphore.acquire(blocking=False)
        assert unrelated.semaphore.acquire(blocking=False)
        unrelated.semaphore.release()
        second.close()
        assert first.semaphore.acquire(blocking=False)
        assert not first.semaphore.acquire(blocking=False)
        first.semaphore.release()
        first.semaphore.release()
    finally:
        first.close()
        second.close()
        unrelated.close()


def test_same_key_waiter_does_not_block_an_unrelated_publication(monkeypatch):
    backend = WebDAVStorageBackend("https://key-queue.test/dav", "assets", publication_concurrency_limit=1)
    first_ready, contender_queued, other_finished, release = Event(), Event(), Event(), Event()
    results, errors = {}, []
    remaining = PublicationBudget.remaining

    def tracked_remaining(budget):
        if current_thread().name == "contender":
            contender_queued.set()
        return remaining(budget)

    def staged(key, *_args):
        with backend._network():
            return key + ".temporary"

    def publish(_temporary, key, size, *_args, **_kwargs):
        if current_thread().name == "first":
            first_ready.set()
            assert release.wait(5)
        return ObjectStat(key, size), "created"

    def run(name, key):
        try:
            results[name] = backend.put_bytes(key, b"image")
            if name == "other":
                other_finished.set()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(PublicationBudget, "remaining", tracked_remaining)
    monkeypatch.setattr(backend, "_ensure_parents", lambda *_args: None)
    monkeypatch.setattr(backend, "_upload_temporary", staged)
    monkeypatch.setattr(backend, "_stat_visible", lambda key, **_kwargs: ObjectStat(key, 5))
    monkeypatch.setattr(backend, "_move_with_retry", publish)
    first = Thread(name="first", target=run, args=("first", "shared.webp"))
    contender = Thread(name="contender", target=run, args=("contender", "shared.webp"))
    other = Thread(name="other", target=run, args=("other", "other.webp"))
    try:
        first.start()
        assert first_ready.wait(5)
        contender.start()
        assert contender_queued.wait(5)
        other.start()
        assert other_finished.wait(5)
        assert not release.is_set()
    finally:
        release.set()
        for thread in (first, contender, other):
            if thread.ident is not None:
                thread.join(5)
        backend.close()
    assert not errors
    assert all(not thread.is_alive() for thread in (first, contender, other))
    assert set(results) == {"first", "contender", "other"}


def test_expired_budget_after_slot_acquisition_does_not_leak_capacity():
    from src.storage.webdav import _budget

    backend = WebDAVStorageBackend("https://expired-slot.test/dav", "assets", publication_concurrency_limit=1)

    class ExpiringBudget:
        calls = 0

        def remaining(self):
            self.calls += 1
            if self.calls > 1:
                raise StorageUnavailable("deadline", retryable=True)
            return 1

    token = _budget.set(ExpiringBudget())
    try:
        with pytest.raises(StorageUnavailable, match="deadline"), backend._network():
            pytest.fail("expired request reached the network")
        assert backend._publication_semaphore.acquire(blocking=False)
        backend._publication_semaphore.release()
    finally:
        _budget.reset(token)
        backend.close()


def test_retiring_backend_does_not_close_active_publication(monkeypatch):
    backend = WebDAVStorageBackend("https://lifecycle.test/dav", "assets")
    started, finish = Event(), Event()

    def publish(key, *args, **kwargs):
        started.set()
        assert finish.wait(5)
        return PublicationResult(key, 5, operation_id="test", disposition="created")

    monkeypatch.setattr(backend, "_publish", publish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            result = executor.submit(backend.put_bytes, "image.webp", b"image")
            assert started.wait(5)
            backend.close()
            assert not backend.http.is_closed
            assert not backend.client.http.is_closed
            with pytest.raises(StorageUnavailable, match="closing"):
                backend.put_bytes("new.webp", b"new")
        finally:
            finish.set()
        assert result.result(timeout=5).created
    assert backend.http.is_closed
    assert backend.client.http.is_closed
    backend.close()
    assert backend._active == 0


@pytest.mark.asyncio
async def test_retiring_backend_keeps_range_response_alive_until_consumed():
    backend = WebDAVStorageBackend("https://range-lifecycle.test/dav", "assets")
    backend.http.close()
    backend.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"image")))
    try:
        response = backend.range_response("a.webp", None, "image/webp")
        backend.close()
        assert not backend.http.is_closed
        assert b"".join([part async for part in response.body_iterator]) == b"image"
        await response.background()
        assert backend._active == 0
        assert backend.http.is_closed
        assert backend.client.http.is_closed
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_unconsumed_range_response_background_releases_lease():
    backend = WebDAVStorageBackend("https://range-background.test/dav", "assets")
    backend.http.close()
    backend.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"image")))
    try:
        response = backend.range_response("a.webp", None, "image/webp")
        backend.close()
        await response.background()
        await response.background()
        assert backend._active == 0
        assert backend.http.is_closed
    finally:
        backend.close()


def test_factory_constructs_one_backend_on_concurrent_cache_misses(monkeypatch):
    from src.storage import factory

    started, proceed = Event(), Event()
    instances = []

    class Backend:
        def __init__(self, *args, **kwargs):
            self.closed = False
            instances.append(self)
            started.set()
            assert proceed.wait(5)

        def close(self):
            self.closed = True

    factory.reset_storage_backends()
    monkeypatch.setattr(factory.settings, "storage", Storage(backend="webdav", webdav_base_url="https://factory.test/dav"))
    monkeypatch.setattr(factory, "WebDAVStorageBackend", Backend)
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(factory.asset_storage) for _ in range(8)]
            try:
                assert started.wait(5)
            finally:
                proceed.set()
            backends = [future.result(timeout=5) for future in futures]
        assert len(instances) == 1
        assert all(backend is instances[0] for backend in backends)
        factory.reset_storage_backends()
        assert instances[0].closed
    finally:
        proceed.set()
        factory.reset_storage_backends()


def test_local_immutable_receipt_distinguishes_creation_and_reuse(tmp_path):
    storage = LocalStorageBackend(tmp_path)
    created = storage.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    reused = storage.put_bytes("a.webp", b"image", immutable=True, overwrite=False)
    assert created.created
    assert reused.disposition == "reused"
    assert not reused.created
    with pytest.raises(FileExistsError):
        storage.put_bytes("a.webp", b"other", immutable=True, overwrite=False)
    with pytest.raises(FileExistsError):
        storage.put_bytes("a.webp", b"image", overwrite=False)
    assert (tmp_path / "a.webp").read_bytes() == b"image"


@pytest.mark.parametrize("field", ["webdav_upload_retry_seconds", "webdav_final_visibility_retry_seconds"])
@pytest.mark.parametrize("value", [(float("nan"),), (float("inf"),), (-1,), (61,), (0,) * 17])
def test_retry_configuration_is_bounded_and_finite(field, value):
    with pytest.raises(ValueError):
        Storage(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1])
def test_publication_deadline_configuration_is_finite(value):
    with pytest.raises(ValueError):
        Storage(webdav_publication_timeout_seconds=value)


def test_unprefixed_retry_and_budget_environment_are_parsed(monkeypatch):
    from src.config.config import Settings

    monkeypatch.setenv("STORAGE__WEBDAV_UPLOAD_RETRY_SECONDS", "[0.2, 0.8]")
    monkeypatch.setenv("STORAGE__WEBDAV_FINAL_VISIBILITY_RETRY_SECONDS", "[1, 3]")
    monkeypatch.setenv("STORAGE__WEBDAV_PUBLICATION_TIMEOUT_SECONDS", "120")
    configured = Settings(_env_file=None).storage
    assert configured.webdav_upload_retry_seconds == (0.2, 0.8)
    assert configured.webdav_final_visibility_retry_seconds == (1, 3)
    assert configured.webdav_publication_timeout_seconds == 120
