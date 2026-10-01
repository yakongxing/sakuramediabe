"""Bounded publication batches that drain in-flight work before reporting failure."""

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Generic, TypeVar

Item = TypeVar("Item")
Result = TypeVar("Result")


@dataclass
class PublicationBatch(Generic[Item, Result]):
    published: list[tuple[Item, Result]] = field(default_factory=list)
    errors: list[tuple[Item, Exception]] = field(default_factory=list)

    def raise_for_errors(self) -> None:
        if not self.errors:
            return
        # A failed sibling must not hide an upload whose outcome needs reconciliation.
        for _, error in self.errors:
            if getattr(error, "publication_possible", False):
                raise error
        raise self.errors[0][1]


def publish_batch(
    items: Iterable[Item],
    publish: Callable[[Item], Result],
    *,
    max_workers: int,
    thread_name_prefix: str,
    stop_on_error: bool = True,
    on_complete: Callable[[Item, Result | None, Exception | None], None] | None = None,
) -> PublicationBatch[Item, Result]:
    if max_workers < 1:
        raise ValueError("publication max_workers must be positive")
    batch: PublicationBatch[Item, Result] = PublicationBatch()
    iterator = iter(items)
    with ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix=thread_name_prefix,
    ) as executor:
        pending = {}

        def fill() -> None:
            while len(pending) < max_workers:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending[executor.submit(copy_context().run, publish, item)] = item

        fill()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                item = pending.pop(future)
                result, error = None, None
                try:
                    result = future.result()
                except Exception as exc:
                    error = exc
                    batch.errors.append((item, exc))
                else:
                    batch.published.append((item, result))
                # Callback failures are coordinator failures, not upload failures.
                # Let them stop scheduling while the executor drains running work.
                if on_complete is not None:
                    on_complete(item, result, error)
            if not stop_on_error or not batch.errors:
                fill()
    return batch
