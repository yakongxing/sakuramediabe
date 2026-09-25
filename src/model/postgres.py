"""Recover idle PostgreSQL sessions without replaying business operations."""

from contextlib import contextmanager

import psycopg2
from loguru import logger
from peewee import OperationalError, PostgresqlDatabase
from psycopg2.extensions import TRANSACTION_STATUS_IDLE


class _UnwrappedOperationalError(OperationalError):
    """Keep Peewee's DB-API wrapper from recasting recovery failures."""


class DatabaseUnavailable(_UnwrappedOperationalError):
    """A connection failed; the interrupted operation must not be replayed."""


def _is_disconnect(exc, connection=None):
    if connection is not None and connection.closed:
        return True
    code = getattr(exc, "pgcode", None)
    return bool(code and (code.startswith("08") or code in {"57P01", "57P02", "57P03"}))


class _RecoveryCursor:
    """Translate driver failures, including failures during result fetching."""

    def __init__(self, database, cursor):
        self._database = database
        self._cursor = cursor

    def _call(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except psycopg2.Error as exc:
            self._database._raise_if_disconnected(exc, self._cursor.connection)
            raise

    def __getattr__(self, name):
        value = getattr(self._cursor, name)
        if callable(value):
            return lambda *args, **kwargs: self._call(value, *args, **kwargs)
        return value

    def __iter__(self):
        return self

    def __next__(self):
        return self._call(next, self._cursor)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecoveringPostgresqlDatabase(PostgresqlDatabase):
    def _mark_unavailable(self):
        if not getattr(self._state, "recovery_needed", False):
            logger.warning("PostgreSQL connection unavailable")
        self._state.recovery_needed = True

    def _raise_if_disconnected(self, exc, connection):
        if _is_disconnect(exc, connection):
            if connection is self._state.conn:
                self._mark_unavailable()
            raise DatabaseUnavailable("Database temporarily unavailable") from exc

    def _connect(self):
        try:
            connection = super()._connect()
        except psycopg2.Error as exc:
            # Establishment failures have no live session. Network failures
            # usually have no SQLSTATE; server SQL errors keep their semantics.
            if _is_disconnect(exc) or (
                isinstance(exc, psycopg2.OperationalError) and not exc.pgcode
            ):
                self._mark_unavailable()
                logger.warning("PostgreSQL connection attempt failed")
                raise DatabaseUnavailable("Database temporarily unavailable") from exc
            raise
        if getattr(self._state, "recovery_needed", False):
            logger.info("PostgreSQL connection restored")
        self._state.recovery_needed = False
        return connection

    def _ensure_connection(self):
        # Never replace a session inside a transaction or while it owns a
        # session-level advisory lock. Peewee must unwind its own transaction stack.
        protected = self.in_transaction() or getattr(self._state, "recovery_pins", 0)
        if protected:
            if self.is_closed() or getattr(self._state, "recovery_needed", False):
                raise DatabaseUnavailable("Database temporarily unavailable")
            return
        if self.is_closed():
            self.connect()
            return
        connection = self._state.conn
        if connection.closed or getattr(self._state, "recovery_needed", False):
            self._mark_unavailable()
            self.close()
            self.connect()
            return
        # Also respect transactions started directly through a driver cursor.
        if connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except psycopg2.Error as exc:
            if not _is_disconnect(exc, connection):
                raise
            self._mark_unavailable()
            self.close()
            self.connect()

    def cursor(self, named_cursor=None):
        self._ensure_connection()
        try:
            cursor = super().cursor(named_cursor=named_cursor)
        except psycopg2.Error as exc:
            self._raise_if_disconnected(exc, self._state.conn)
            raise
        return _RecoveryCursor(self, cursor)

    def rollback(self):
        # Peewee 4 attempts rollback after a failed COMMIT, after popping its
        # transaction. A dead connection has nothing to roll back; reconnect
        # only when the next independent operation starts.
        if getattr(self._state, "recovery_needed", False):
            return
        return super().rollback()

    @contextmanager
    def pinned_connection(self):
        """Keep session locks valid by forbidding recovery inside their scope."""
        self._ensure_connection()
        self._state.recovery_pins = getattr(self._state, "recovery_pins", 0) + 1
        try:
            yield self.connection()
        finally:
            self._state.recovery_pins -= 1
