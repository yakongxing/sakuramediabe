"""Bounded coordination for synchronous storage publication."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Lock
from time import monotonic
from weakref import WeakValueDictionary

from .types import StorageUnavailable


@dataclass
class PublicationBudget:
    seconds: float
    deadline: float = field(init=False)

    def __post_init__(self):
        self.deadline = monotonic() + self.seconds

    def remaining(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise StorageUnavailable(
                "Storage publication deadline exceeded", stage="deadline",
                retryable=True, error_code="storage_publication_timeout",
            )
        return remaining


class KeyedLocks:
    """Count waiters as owners so eviction cannot create a second lock."""

    def __init__(self):
        self._guard = Lock()
        self._entries: dict[str, tuple[Lock, int]] = {}

    @contextmanager
    def hold(self, key: str, budget: PublicationBudget | None = None):
        with self._guard:
            lock, references = self._entries.get(key, (Lock(), 0))
            self._entries[key] = (lock, references + 1)
        acquired = False
        try:
            acquired = lock.acquire(timeout=budget.remaining() if budget else -1)
            if not acquired:
                raise StorageUnavailable(
                    "Storage publication lock timed out", stage="queue",
                    retryable=True, error_code="storage_busy",
                )
            yield
        finally:
            if acquired:
                lock.release()
            with self._guard:
                _, references = self._entries[key]
                if references == 1:
                    del self._entries[key]
                else:
                    self._entries[key] = (lock, references - 1)

    def __len__(self):
        with self._guard:
            return len(self._entries)


class _EndpointCapacity:
    def __init__(self):
        self._condition = Condition()
        self._registrations: dict[object, int] = {}
        self._active = 0

    def register(self, token: object, limit: int):
        with self._condition:
            self._registrations[token] = limit
            self._condition.notify_all()

    def unregister(self, token: object):
        with self._condition:
            self._registrations.pop(token, None)
            self._condition.notify_all()

    def acquire(self, blocking=True, timeout=None):
        with self._condition:
            def available():
                return self._active < min(self._registrations.values(), default=1)
            if not blocking:
                if not available():
                    return False
            elif not self._condition.wait_for(available, timeout=timeout):
                return False
            self._active += 1
            return True

    def release(self):
        with self._condition:
            if self._active <= 0:
                raise ValueError("unbalanced upload limiter release")
            self._active -= 1
            self._condition.notify_all()


class UploadLimiter:
    def __init__(self, group: _EndpointCapacity, limit: int):
        self.semaphore = group
        self._token = object()
        group.register(self._token, limit)

    def close(self):
        self.semaphore.unregister(self._token)

    def __del__(self):
        self.close()


_limiter_guard = Lock()
_limiters: WeakValueDictionary = WeakValueDictionary()
path_locks = KeyedLocks()


def upload_limiter(identity: tuple, limit: int) -> UploadLimiter:
    """The strictest live config wins while retired backends drain requests."""
    with _limiter_guard:
        group = _limiters.get(identity)
        if group is None:
            group = _EndpointCapacity()
            _limiters[identity] = group
        return UploadLimiter(group, limit)
