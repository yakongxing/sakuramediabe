from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier
from unittest.mock import MagicMock

import psycopg2
import pytest
from peewee import IntegrityError, OperationalError, ProgrammingError
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INTRANS

from src.config.config import Database, settings
from src.model.base import create_database
from src.model.postgres import (
    DatabaseUnavailable,
    RecoveringPostgresqlDatabase,
    _is_disconnect,
)
from src.service.playback.operation_locks import MEDIA_LOCK, media_operation_lock


def fake_connection():
    connection = MagicMock()
    connection.closed = 0
    connection.server_version = 150000
    connection.get_transaction_status.return_value = TRANSACTION_STATUS_IDLE
    cursor = connection.cursor.return_value
    cursor.connection = connection
    cursor.__enter__.return_value = cursor
    return connection


@pytest.fixture
def fake_db(monkeypatch):
    connections = [fake_connection(), fake_connection()]
    connect = MagicMock(side_effect=connections)
    monkeypatch.setattr(psycopg2, "connect", connect)
    database = RecoveringPostgresqlDatabase("test", register_unicode=False)
    database.connect()
    yield database, connections, connect
    if not database.is_closed():
        database.close()


def fail_connection(connection):
    connection.closed = 1
    raise psycopg2.OperationalError("server closed the connection unexpectedly")


def test_timeout_default_and_url_override():
    assert (
        create_database(Database(url="postgresql://localhost/test")).connect_params[
            "connect_timeout"
        ]
        == 5
    )
    assert (
        create_database(
            Database(url="postgresql://localhost/test?connect_timeout=9")
        ).connect_params["connect_timeout"]
        == "9"
    )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("08006", True),
        ("57P01", True),
        ("57P02", True),
        ("57P03", True),
        ("40001", False),
        ("40P01", False),
        ("57014", False),
        ("23505", False),
    ],
)
def test_disconnect_sqlstates(code, expected):
    error = MagicMock(pgcode=code)
    assert _is_disconnect(error, fake_connection()) is expected


def test_probe_replaces_stale_connection_before_business_sql(fake_db):
    database, (old, new), connect = fake_db
    old.cursor.return_value.execute.side_effect = lambda *args: fail_connection(old)

    database.execute_sql("SELECT 42")

    assert connect.call_count == 2
    old.cursor.return_value.execute.assert_called_once_with("SELECT 1")
    new.cursor.return_value.execute.assert_called_once_with("SELECT 42", ())


@pytest.mark.parametrize("disconnect", ["closed", "probe", "marked"])
@pytest.mark.parametrize("depth", [1, 2])
def test_recovery_preserves_connection_context_stack(fake_db, disconnect, depth):
    database, (old, new), connect = fake_db
    with ExitStack() as stack:
        for _ in range(depth):
            stack.enter_context(database.connection_context())
        contexts = list(database._state.ctx)
        if disconnect == "closed":
            old.closed = 1
        elif disconnect == "probe":
            old.cursor.return_value.execute.side_effect = lambda *args: fail_connection(old)
        else:
            database._mark_unavailable()
        database.execute_sql("SELECT 42")
        assert database._state.ctx == contexts
        assert database.connection() is new
        assert connect.call_count == 2
        new.close.assert_not_called()
    assert database.is_closed()
    assert database._state.ctx == []
    new.close.assert_called_once()


@pytest.mark.parametrize("depth", [1, 2])
def test_failed_recovery_preserves_context_exit_and_original_error(fake_db, depth):
    database, (old, new), connect = fake_db
    failure = psycopg2.OperationalError("connection refused")
    connect.side_effect = [failure, new]
    with pytest.raises(DatabaseUnavailable) as caught, ExitStack() as stack:
        for _ in range(depth):
            stack.enter_context(database.connection_context())
        old.closed = 1
        database.execute_sql("SELECT 42")
    assert caught.value.__cause__ is failure
    assert database.is_closed()
    assert database._state.ctx == []
    with database.connection_context():
        database.execute_sql("SELECT 43")
        assert database.connection() is new
    assert database.is_closed()
    new.close.assert_called_once()


def test_recovery_can_retry_inside_still_active_connection_context(fake_db):
    database, (old, new), connect = fake_db
    connect.side_effect = [psycopg2.OperationalError("connection refused"), new]
    with database.connection_context():
        contexts = list(database._state.ctx)
        old.closed = 1
        with pytest.raises(DatabaseUnavailable):
            database.execute_sql("SELECT 42")
        assert database._state.ctx == contexts
        assert database.is_closed()
        database.execute_sql("SELECT 43")
        assert database._state.ctx == contexts
        assert database.connection() is new
        new.close.assert_not_called()
    assert database.is_closed()
    new.close.assert_called_once()


@pytest.mark.parametrize("entry", ["nested_context", "connection"])
def test_failed_recovery_allows_reopening_within_outer_context(fake_db, entry):
    database, (old, new), connect = fake_db
    connect.side_effect = [psycopg2.OperationalError("connection refused"), new]
    with database.connection_context():
        contexts = list(database._state.ctx)
        old.closed = 1
        with pytest.raises(DatabaseUnavailable):
            database.execute_sql("SELECT 42")
        if entry == "nested_context":
            with database.connection_context():
                assert database.connection() is new
                assert database._state.ctx[:-1] == contexts
        else:
            assert database.connection() is new
        assert database._state.ctx == contexts
        new.close.assert_not_called()
    assert database.is_closed()
    new.close.assert_called_once()


def test_recovered_inner_context_does_not_close_outer_connection(fake_db):
    database, (old, new), _ = fake_db
    with pytest.raises(RuntimeError, match="business error"), database.connection_context():
        with database.connection_context():
            old.closed = 1
            database.execute_sql("SELECT 42")
        assert not database.is_closed()
        assert len(database._state.ctx) == 1
        assert database.connection() is new
        new.close.assert_not_called()
        raise RuntimeError("business error")
    assert database.is_closed()
    new.close.assert_called_once()


@pytest.mark.parametrize("sql", ["INSERT INTO item VALUES (1)", "BEGIN", "COMMIT"])
def test_disconnect_during_execution_never_replays_sql(fake_db, sql):
    database, (old, new), connect = fake_db
    statements = []

    def execute(statement, *args):
        statements.append(statement)
        if statement == sql:
            fail_connection(old)

    old.cursor.return_value.execute.side_effect = execute
    with pytest.raises(DatabaseUnavailable):
        if sql == "BEGIN":
            with database.atomic():
                pytest.fail("transaction must not start")
        elif sql == "COMMIT":
            with database.atomic():
                pass
        else:
            database.execute_sql(sql)
    assert statements.count(sql) == 1
    assert connect.call_count == 1
    assert database.transaction_depth() == 0

    database.execute_sql("SELECT 42")
    assert connect.call_count == 2
    new.cursor.return_value.execute.assert_called_once_with("SELECT 42", ())


def test_nested_transaction_failure_unwinds_before_reconnecting(fake_db):
    database, (old, _new), connect = fake_db
    with pytest.raises(DatabaseUnavailable), database.atomic(), database.atomic():
        old.closed = 1
        old.cursor.return_value.execute.side_effect = psycopg2.InterfaceError("closed")
        with pytest.raises(DatabaseUnavailable):
            database.execute_sql("SELECT 42")
        assert database.transaction_depth() == 1
        assert connect.call_count == 1
        database.execute_sql("SELECT 43")
    assert database.transaction_depth() == 0
    database.execute_sql("SELECT 44")
    assert connect.call_count == 2


def test_reconnect_failure_can_recover_on_later_call(fake_db):
    database, (old, new), connect = fake_db
    old.closed = 1
    connect.side_effect = [psycopg2.OperationalError("connection refused"), new]
    with pytest.raises(DatabaseUnavailable):
        database.execute_sql("SELECT 42")
    assert database.is_closed()
    database.execute_sql("SELECT 43")
    assert connect.call_count == 3


@pytest.mark.parametrize(
    "error,expected",
    [
        (psycopg2.IntegrityError("constraint"), IntegrityError),
        (psycopg2.ProgrammingError("syntax"), ProgrammingError),
        (psycopg2.errors.QueryCanceled("cancelled"), psycopg2.errors.QueryCanceled),
        (psycopg2.errors.DeadlockDetected("deadlock"), OperationalError),
    ],
)
def test_sql_errors_are_not_connection_failures(fake_db, error, expected):
    database, (old, _new), connect = fake_db

    def execute(sql, *args):
        if sql != "SELECT 1":
            raise error

    old.cursor.return_value.execute.side_effect = execute
    with pytest.raises(expected) as caught:
        database.execute_sql("bad SQL")
    assert not isinstance(caught.value, DatabaseUnavailable)
    assert connect.call_count == 1


def test_fetch_failure_is_translated_without_replay(fake_db):
    database, (old, _new), connect = fake_db
    cursor = database.execute_sql("SELECT 42")
    old.cursor.return_value.fetchone.side_effect = lambda: fail_connection(old)
    with pytest.raises(DatabaseUnavailable):
        cursor.fetchone()
    assert connect.call_count == 1


def test_pinned_session_never_reconnects(fake_db):
    database, (old, _new), connect = fake_db
    with database.pinned_connection():
        old.closed = 1
        old.cursor.side_effect = psycopg2.InterfaceError("closed")
        with pytest.raises(DatabaseUnavailable):
            database.execute_sql("SELECT 42")
        assert connect.call_count == 1
    database.execute_sql("SELECT 43")
    assert connect.call_count == 2


def test_driver_transaction_is_not_probed(fake_db):
    database, (old, _new), connect = fake_db
    old.get_transaction_status.return_value = TRANSACTION_STATUS_INTRANS
    database.execute_sql("SELECT 42")
    old.cursor.return_value.execute.assert_called_once_with("SELECT 42", ())
    assert connect.call_count == 1


def terminate_connection(connection):
    with psycopg2.connect(settings.database.url) as control:
        control.autocommit = True
        with control.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(%s)", (connection.get_backend_pid(),)
            )
            assert cursor.fetchone() == (True,)


def test_real_idle_connection_recovers_after_termination(test_db):
    old = test_db.connection()
    terminate_connection(old)
    assert test_db.execute_sql("SELECT 42").fetchone() == (42,)
    assert test_db.connection() is not old


def test_real_transaction_is_not_replayed_after_termination(test_db):
    test_db.execute_sql("CREATE TABLE reconnect_transaction_test (value integer)")
    old = test_db.connection()
    try:
        with pytest.raises(DatabaseUnavailable), test_db.atomic():
            test_db.execute_sql("INSERT INTO reconnect_transaction_test VALUES (1)")
            terminate_connection(old)
            test_db.execute_sql("INSERT INTO reconnect_transaction_test VALUES (2)")
        assert test_db.transaction_depth() == 0
        assert test_db.connection() is old
        assert test_db.execute_sql(
            "SELECT count(*) FROM reconnect_transaction_test"
        ).fetchone() == (0,)
    finally:
        test_db.execute_sql("DROP TABLE reconnect_transaction_test")


def test_real_advisory_lock_does_not_silently_change_session(test_db):
    with media_operation_lock(MEDIA_LOCK, 123):
        old = test_db.connection()
        terminate_connection(old)
        with pytest.raises(DatabaseUnavailable):
            test_db.execute_sql("SELECT 42")
        assert test_db.connection() is old
    assert test_db.execute_sql("SELECT 42").fetchone() == (42,)


def test_multiple_long_lived_threads_recover_independently(test_db):
    database = create_database(settings.database)
    barrier = Barrier(3)

    def work():
        with database.connection_context():
            old = database.connection()
            barrier.wait(timeout=10)
            terminate_connection(old)
            assert database.execute_sql("SELECT 42").fetchone() == (42,)
            assert database.connection() is not old
            pid = database.connection().get_backend_pid()
        assert database.is_closed()
        assert database._state.ctx == []
        return pid

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: work(), range(3)))
    assert len(set(results)) == 3
