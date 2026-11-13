"""Unit tests for `core.services.data_migration.merge_metadata_ledgers`."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.services.data_migration import (
    merge_metadata_ledgers,
    migrate_data_dir,
    normalize_instrument_case,
    rebuild_metadata_ledger,
)
from storage.tick_format import write_hour_file

_SCHEMA = """
CREATE TABLE hour_status (
    instrument  TEXT NOT NULL,
    hour_utc    TEXT NOT NULL,
    status      TEXT NOT NULL,
    tick_count  INTEGER NOT NULL DEFAULT 0,
    file_path   TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (instrument, hour_utc)
);
CREATE TABLE mt5_custom_symbol (
    symbol         TEXT PRIMARY KEY,
    source_symbol  TEXT NOT NULL DEFAULT '',
    ticks          INTEGER NOT NULL DEFAULT 0,
    first_ms       INTEGER NOT NULL DEFAULT 0,
    last_ms        INTEGER NOT NULL DEFAULT 0,
    range_label    TEXT,
    imported_at    TEXT,
    synced_at      TEXT NOT NULL
);
"""


def _hour(instrument, hour, status="completed", tick_count=1, file_path=None):
    return (instrument, hour, status, tick_count, file_path, 1, None, "2026-11-12T00:00:00")


def _mt5(symbol, source="", ticks=10, first=0, last=0, label=None, imported=None):
    return (symbol, source, ticks, first, last, label, imported, "2026-11-12T00:00:00")


def _build_db(path, hours=(), mt5=()):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        if hours:
            conn.executemany(
                "INSERT INTO hour_status (instrument, hour_utc, status, tick_count, "
                "file_path, attempts, last_error, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                hours,
            )
        if mt5:
            conn.executemany(
                "INSERT INTO mt5_custom_symbol (symbol, source_symbol, ticks, first_ms, "
                "last_ms, range_label, imported_at, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                mt5,
            )
        conn.commit()
    finally:
        conn.close()


class MergeMetadataLedgersTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_disjoint_union_merges_all_rows(self):
        dst = self._dir / "dst.db"
        src = self._dir / "src.db"
        _build_db(
            dst,
            hours=[_hour("A", "2026-01-01T10:00:00")],
            mt5=[_mt5("A_META")],
        )
        _build_db(
            src,
            hours=[
                _hour("B", "2026-01-01T10:00:00"),
                _hour("B", "2026-01-01T11:00:00", status="empty", tick_count=0),
            ],
            mt5=[_mt5("B_META")],
        )

        result = merge_metadata_ledgers(dst, [src])
        self.assertEqual(result["hour_rows"], 2)
        self.assertEqual(result["mt5_rows"], 1)
        self.assertEqual(result["path_rewrites"], 0)

        conn = sqlite3.connect(str(dst))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM hour_status").fetchone()[0], 3
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM mt5_custom_symbol").fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_completed_row_survives_source_failure(self):
        dst = self._dir / "dst.db"
        src = self._dir / "src.db"
        _build_db(dst, hours=[_hour("A", "2026-01-01T10:00:00", tick_count=100)])
        # Same PK in src attempts a downgrade; a second, unique row keeps the
        # source non-empty so the merge proceeds.
        _build_db(
            src,
            hours=[
                _hour("A", "2026-01-01T10:00:00", status="failed", tick_count=0),
                _hour("B", "2026-01-01T10:00:00"),
            ],
        )

        result = merge_metadata_ledgers(dst, [src])
        self.assertEqual(result["hour_rows"], 1)  # only B is new

        conn = sqlite3.connect(str(dst))
        try:
            row = conn.execute(
                "SELECT status, tick_count FROM hour_status "
                "WHERE instrument=? AND hour_utc=?",
                ("A", "2026-01-01T10:00:00"),
            ).fetchone()
            self.assertEqual(row, ("completed", 100))
        finally:
            conn.close()

    def test_path_rewrites_only_match_prefix(self):
        dst = self._dir / "dst.db"
        src = self._dir / "src.db"
        old1 = str(self._dir / "roots" / "one")
        old2 = str(self._dir / "roots" / "two")
        new = str(self._dir / "roots" / "new")
        _build_db(
            dst,
            hours=[
                _hour("A", "2026-01-01T10:00:00", file_path=old1 + "/A/2026/01/01/10.bin"),
                _hour("C", "2026-01-01T10:00:00", file_path="/unrelated/C/2026/01/01/10.bin"),
            ],
        )
        _build_db(
            src,
            hours=[
                _hour("B", "2026-01-01T10:00:00", file_path=old2 + "/B/2026/01/01/10.bin"),
                _hour("D", "2026-01-01T10:00:00", file_path=old2 + "/D/2026/01/01/10.bin"),
            ],
        )

        result = merge_metadata_ledgers(
            dst, [src], path_rewrites=[(old1, new), (old2, new)]
        )
        self.assertEqual(result["hour_rows"], 2)
        self.assertEqual(result["path_rewrites"], 3)  # A, B, D

        conn = sqlite3.connect(str(dst))
        try:
            rows = dict(conn.execute("SELECT instrument, file_path FROM hour_status").fetchall())
            self.assertEqual(rows["A"], new + "/A/2026/01/01/10.bin")
            self.assertEqual(rows["B"], new + "/B/2026/01/01/10.bin")
            self.assertEqual(rows["D"], new + "/D/2026/01/01/10.bin")
            # Unrelated prefix untouched.
            self.assertEqual(rows["C"], "/unrelated/C/2026/01/01/10.bin")
        finally:
            conn.close()

    def test_schema_mismatch_raises(self):
        dst = self._dir / "dst.db"
        src = self._dir / "src.db"
        _build_db(dst, hours=[_hour("A", "2026-01-01T10:00:00")])
        conn = sqlite3.connect(str(src))
        try:
            conn.executescript(_SCHEMA.replace("tick_count  INTEGER NOT NULL DEFAULT 0",
                                              "tick_count  INTEGER NOT NULL DEFAULT 1"))
            conn.execute(
                "INSERT INTO hour_status (instrument, hour_utc, status, tick_count, "
                "file_path, attempts, last_error, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                _hour("B", "2026-01-01T10:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(ValueError) as ctx:
            merge_metadata_ledgers(dst, [src])
        self.assertIn("schema mismatch", str(ctx.exception))

    def test_empty_source_raises(self):
        dst = self._dir / "dst.db"
        src = self._dir / "src.db"
        _build_db(dst, hours=[_hour("A", "2026-01-01T10:00:00")])
        _build_db(src)  # tables exist, zero rows

        with self.assertRaises(ValueError) as ctx:
            merge_metadata_ledgers(dst, [src])
        self.assertIn("contributed no rows", str(ctx.exception))

    def test_missing_source_raises(self):
        dst = self._dir / "dst.db"
        _build_db(dst, hours=[_hour("A", "2026-01-01T10:00:00")])
        with self.assertRaises(ValueError) as ctx:
            merge_metadata_ledgers(dst, [self._dir / "nope.db"])
        self.assertIn("source database not found", str(ctx.exception))

    def test_missing_destination_raises(self):
        with self.assertRaises(ValueError) as ctx:
            merge_metadata_ledgers(self._dir / "missing.db", [])
        self.assertIn("destination database not found", str(ctx.exception))

    def test_no_sources_returns_zeros(self):
        dst = self._dir / "dst.db"
        _build_db(dst, hours=[_hour("A", "2026-01-01T10:00:00")])
        self.assertEqual(
            merge_metadata_ledgers(dst, []),
            {"hour_rows": 0, "mt5_rows": 0, "path_rewrites": 0,
             "missing_dropped": 0},
        )

    def test_drop_missing_deletes_rows_with_missing_files(self):
        dst = self._dir / "dst.db"
        existing = self._dir / "existing.bin"
        existing.write_bytes(b"x" * 16)
        # A row whose file path exists on disk, one whose file is missing,
        # and one NULL file_path (empty hour — must be kept).
        _build_db(
            dst,
            hours=[
                _hour("A", "2026-01-01T10:00:00", file_path=str(existing)),
                _hour("B", "2026-01-01T10:00:00", file_path=str(self._dir / "missing.bin")),
                _hour("C", "2026-01-01T10:00:00", file_path=None),
            ],
        )

        result = merge_metadata_ledgers(dst, [], drop_missing=True)
        self.assertEqual(result["missing_dropped"], 1)

        conn = sqlite3.connect(str(dst))
        try:
            remaining = conn.execute(
                "SELECT instrument FROM hour_status ORDER BY instrument"
            ).fetchall()
            self.assertEqual([r[0] for r in remaining], ["A", "C"])
        finally:
            conn.close()

    def test_drop_missing_default_false_keeps_missing(self):
        dst = self._dir / "dst.db"
        _build_db(
            dst,
            hours=[
                _hour("B", "2026-01-01T10:00:00",
                       file_path=str(self._dir / "missing.bin")),
            ],
        )
        result = merge_metadata_ledgers(dst, [])
        self.assertEqual(result["missing_dropped"], 0)
        self.assertTrue(dst.is_file())


class MigrateDataDirSkipErrorsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._src = self._dir / "src"
        self._dst = self._dir / "dst"
        self._src.mkdir()
        self._dst.mkdir()
        (self._src / "ok.bin").write_bytes(b"ok" * 8)
        self._bad = self._src / "bad.bin"
        self._bad.write_bytes(b"bad" * 8)

    def tearDown(self):
        self._tmp.cleanup()

    def test_skip_errors_skips_unreadable_file(self):
        import os
        import shutil as _shutil
        import core.services.data_migration as dm

        real_copy2 = _shutil.copy2

        def _copy2(src, dst, *a, **k):
            if os.path.basename(os.fspath(src)) == "bad.bin":
                raise OSError("simulated unreadable sector")
            return real_copy2(src, dst, *a, **k)

        with mock.patch.object(dm.shutil, "copy2", side_effect=_copy2):
            stats = migrate_data_dir(self._src, self._dst, skip_errors=True)

        self.assertEqual(stats["files_copied"], 1)
        self.assertEqual(stats["files_verified"], 1)
        self.assertEqual(stats["skipped_files"], [str(self._bad)])
        self.assertFalse((self._dst / "bad.bin").exists())
        self.assertTrue((self._dst / "ok.bin").exists())

    def test_without_skip_errors_raises(self):
        import os
        import shutil as _shutil
        import core.services.data_migration as dm

        real_copy2 = _shutil.copy2

        def _copy2(src, dst, *a, **k):
            if os.path.basename(os.fspath(src)) == "bad.bin":
                raise OSError("simulated unreadable sector")
            return real_copy2(src, dst, *a, **k)

        with mock.patch.object(dm.shutil, "copy2", side_effect=_copy2):
            with self.assertRaises(RuntimeError):
                migrate_data_dir(self._src, self._dst)


class NormalizeInstrumentCaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_folds_uppercase_into_lowercase_preserving_empty_rows(self):
        db = self._dir / "ledger.db"
        # Mixed case: same file hour exists in both cases; the uppercase set
        # also carries a null-path (empty/failed) row not present in lowercase.
        _build_db(
            db,
            hours=[
                _hour("eurusd", "2026-01-01T10:00:00", file_path=r"C:\data\EURUSD\2026\01\01\10.bin"),
                _hour("EURUSD", "2026-01-01T10:00:00", file_path=r"C:\data\EURUSD\2026\01\01\10.bin"),
                _hour("EURUSD", "2026-01-01T11:00:00", status="empty", tick_count=0, file_path=None),
            ],
        )

        deleted = normalize_instrument_case(db)
        self.assertEqual(deleted, 2)

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT instrument, hour_utc, status, tick_count, file_path "
                "FROM hour_status ORDER BY hour_utc"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["eurusd", "eurusd"])
            self.assertTrue(all(r[0] == r[0].lower() for r in rows))
            # Both the file hour and the empty hour survive under lowercase.
            keys = {r[1] for r in rows}
            self.assertEqual(keys, {"2026-01-01T10:00:00", "2026-01-01T11:00:00"})
        finally:
            conn.close()

    def test_already_lowercase_is_noop(self):
        db = self._dir / "ledger.db"
        _build_db(db, hours=[_hour("eurusd", "2026-01-01T10:00:00")])
        self.assertEqual(normalize_instrument_case(db), 0)


class RebuildMetadataLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._tree = self._dir / "tree"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_hour(self, symbol, year, month, day, hour, ticks):
        path = self._tree / symbol / f"{year:04d}" / f"{month:02d}" / f"{day:02d}" / f"{hour:02d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_hour_file(path, list(range(ticks)), [1.0] * ticks, [1.1] * ticks)
        return path

    def test_rebuild_writes_completed_rows(self):
        p1 = self._write_hour("EURUSD", 2025, 1, 2, 14, 120)
        self._write_hour("EURUSD", 2025, 1, 2, 15, 80)
        self._write_hour("GBPUSD", 2024, 12, 31, 23, 5)
        self._write_hour("GBPUSD", 2024, 12, 31, 22, 0)  # empty hour file
        out = self._dir / "rebuilt.db"

        n = rebuild_metadata_ledger(self._tree, out)
        self.assertEqual(n, 4)

        conn = sqlite3.connect(str(out))
        try:
            rows = conn.execute(
                "SELECT instrument, hour_utc, status, tick_count, file_path "
                "FROM hour_status ORDER BY instrument, hour_utc"
            ).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                rows[0],
                ("EURUSD", "2025-01-02T14:00:00", "completed", 120, str(p1)),
            )
            statuses = {r[2] for r in rows}
            self.assertEqual(statuses, {"completed"})
            # A zero-tick file is still a completed (empty) hour with count 0.
            empty = [r for r in rows if r[1] == "2024-12-31T22:00:00"][0]
            self.assertEqual(empty[3], 0)
            self.assertTrue(conn.execute("SELECT COUNT(*) FROM mt5_custom_symbol").fetchone()[0] == 0)
        finally:
            conn.close()

    def test_rebuild_ignores_stray_files(self):
        self._write_hour("EURUSD", 2025, 1, 2, 14, 10)
        stray = self._tree / "EURUSD" / "notes.txt"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("noise", encoding="utf-8")
        deep = self._tree / "EURUSD" / "2025" / "01" / "02" / "14" / "extra.bin"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_bytes(b"\x00" * 8)
        badhour = self._tree / "EURUSD" / "2025" / "01" / "02" / "99.bin"
        badhour.parent.mkdir(parents=True, exist_ok=True)
        write_hour_file(badhour, [1], [1.0], [1.1])

        n = rebuild_metadata_ledger(self._tree, self._dir / "rebuilt.db")
        self.assertEqual(n, 1)  # only the valid hour file

    def test_rebuild_empty_tree_returns_zero(self):
        n = rebuild_metadata_ledger(self._tree, self._dir / "rebuilt.db")
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
