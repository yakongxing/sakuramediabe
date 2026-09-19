from unittest.mock import MagicMock

from src.model.postgres import DatabaseUnavailable
from src.scheduler.worker import TaskWorker


def test_claim_loop_survives_initial_connection_failure(monkeypatch):
    worker = TaskWorker()
    ready = MagicMock(side_effect=[DatabaseUnavailable("offline"), None])
    monkeypatch.setattr("src.scheduler.worker.ensure_database_ready", ready)
    claim = MagicMock(side_effect=lambda **kwargs: worker.stop())
    monkeypatch.setattr("src.scheduler.worker.TaskQueueService.claim_next", claim)
    monkeypatch.setattr(worker._stop, "wait", lambda _: None)

    worker._claim_loop()

    assert ready.call_count == 2
    claim.assert_called_once()


def test_claim_loop_survives_execution_cleanup_failure(monkeypatch):
    worker = TaskWorker()
    monkeypatch.setattr("src.scheduler.worker.ensure_database_ready", lambda: None)
    claim = MagicMock(side_effect=[object(), None])
    monkeypatch.setattr("src.scheduler.worker.TaskQueueService.claim_next", claim)
    execute = MagicMock(side_effect=DatabaseUnavailable("offline"))
    monkeypatch.setattr(worker, "_execute", execute)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            worker.stop()

    monkeypatch.setattr(worker._stop, "wait", wait)
    worker._claim_loop()
    assert claim.call_count == 2
    execute.assert_called_once()


def test_housekeeping_survives_initial_connection_failure(monkeypatch):
    worker = TaskWorker()
    ready = MagicMock(side_effect=[DatabaseUnavailable("offline"), None])
    monkeypatch.setattr("src.scheduler.worker.ensure_database_ready", ready)
    work = MagicMock()
    monkeypatch.setattr(worker, "_run_housekeeping_once", work)
    monkeypatch.setattr(
        worker._stop, "wait", MagicMock(side_effect=[False, False, True])
    )

    worker._housekeeping_loop()

    assert ready.call_count == 2
    work.assert_called_once()
