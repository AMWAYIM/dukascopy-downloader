"""Retry/reopen behavior for transient SQLite I/O errors.

These tests verify that MetadataDB operations recover from momentary
``sqlite3.OperationalError: disk I/O error`` by retrying on a reopened
connection, and that persistent I/O errors still surface after the retry
budget is exhausted (they must not be swallowed).

CPython's ``sqlite3.Connection.execute`` is a read-only slot, so failure
injection is done by wrapping the connection in a delegating proxy rather
than monkeypatching the method.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import storage.metadata_db as metadata_db_module
from storage.metadata_db import MetadataDB


class _FlakyConn:
    """Delegating proxy that fails the first N execute() calls."""

    def __init__(self, real, fail_for: int = 0, error: str = "disk I/O error"):
        self._real = real
        self._remaining_failures = fail_for
        self._error = error
        self.execute_calls = 0
        self.closed = False

    def execute(self, sql, params=None):
        self.execute_calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise sqlite3.OperationalError(self._error)
        if params is None:
            return self._real.execute(sql)
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.closed = True
        return self._real.close()


class _AlwaysFailConn:
    """A connection whose execute always raises OperationalError('disk I/O error')."""

    def __init__(self):
        self.closed = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class RetryIoErrorTests(unittest.TestCase):
    def setUp(self):
        # Keep the retry backoff instant in tests.
        self._orig_sleep = metadata_db_module.time.sleep
        metadata_db_module.time.sleep = lambda seconds: None

    def tearDown(self):
        metadata_db_module.time.sleep = self._orig_sleep

    def test_transient_io_error_recovers_via_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MetadataDB(Path(tmp) / "meta.db")
            original = db._conn
            try:
                flaky = _FlakyConn(original, fail_for=2)
                db._conn = flaky
                self.assertEqual(db.list_instruments(), [])
                # first attempt failed (wrapper), then the connection was
                # reopened and the read succeeded on the fresh connection.
                self.assertEqual(flaky.execute_calls, 1)
                self.assertTrue(flaky.closed, "wedged connection must be closed")
                self.assertIsNot(db._conn, original)
                self.assertIs(type(db._conn), sqlite3.Connection)
            finally:
                db.close()

    def test_persistent_io_error_raises_after_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MetadataDB(Path(tmp) / "meta.db")
            original = db._conn
            try:
                opened = []

                def fake_connect():
                    conn = _AlwaysFailConn()
                    opened.append(conn)
                    return conn

                db._connect = fake_connect
                db._conn = db._connect()
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    db.list_instruments()
                self.assertIn("disk I/O error", str(ctx.exception))
                # initial + one reopen per retry attempt (budget is
                # _LOCKED_RETRIES + 1 attempts in total).
                self.assertEqual(len(opened), 4)
                self.assertTrue(all(c.closed for c in opened[:-1]), "retried conns closed")
                self.assertFalse(opened[-1].closed, "final conn stays open for use")
            finally:
                db.close()
                try:
                    original.close()
                except sqlite3.Error:
                    pass

    def test_locked_error_still_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MetadataDB(Path(tmp) / "meta.db")
            original = db._conn
            try:
                flaky = _FlakyConn(original, fail_for=2, error="database is locked")
                db._conn = flaky
                self.assertEqual(db.list_instruments(), [])
                self.assertEqual(flaky.execute_calls, 3)
                self.assertFalse(flaky.closed, "locked errors do not reopen")
            finally:
                db.close()

    def test_non_transient_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MetadataDB(Path(tmp) / "meta.db")
            original = db._conn
            try:
                flaky = _FlakyConn(
                    original, fail_for=1, error="no such table: nope"
                )
                db._conn = flaky
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    db.list_instruments()
                self.assertIn("no such table", str(ctx.exception))
                self.assertEqual(flaky.execute_calls, 1, "no retry expected")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
