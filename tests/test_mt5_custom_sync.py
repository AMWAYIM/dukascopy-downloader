from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from export.mt5_symbol_manager import _parse_symbols_file
from storage.metadata_db import MetadataDB


class Mt5CustomSyncTests(unittest.TestCase):
    def _db(self, tmp: str) -> MetadataDB:
        return MetadataDB(Path(tmp) / "meta.db")

    def test_parse_symbols_file_supports_optional_path_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "symbols.txt"
            path.write_text(
                "USDJPY.DUK|10|1000|2000|Custom\\dukascopy\n"
                "EURUSD.DUK|5|3000|4000\n",
                encoding="utf-8",
            )
            parsed = _parse_symbols_file(path)
            self.assertEqual(len(parsed), 2)
            by_sym = {s.symbol: s for s in parsed}
            self.assertEqual(by_sym["USDJPY.DUK"].symbol_path, "Custom\\dukascopy")
            self.assertEqual(by_sym["EURUSD.DUK"].symbol_path, "")
            self.assertEqual(by_sym["USDJPY.DUK"].to_dict()["symbol_path"], "Custom\\dukascopy")

    def test_sync_prunes_stale_and_preserves_import_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._db(temp_dir)
            try:
                db.upsert_mt5_custom_symbol(
                    symbol="USDJPY.DUK", source_symbol="USDJPY",
                    ticks=7, first_ms=1, last_ms=2,
                    range_label="2024-01-01 -> 2024-01-02", mark_imported=True,
                )
                db.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", source_symbol="XAUUSD", ticks=3)
                db.flush()

                removed, pruned = db.sync_mt5_custom_symbols([
                    {"symbol": "USDJPY.DUK", "source_symbol": "USDJPY",
                     "ticks": 9, "first_ms": 10, "last_ms": 20},
                    {"symbol": "EURUSD.DUK", "source_symbol": "EURUSD",
                     "ticks": 5, "first_ms": 30, "last_ms": 40},
                ])
                db.flush()

                self.assertEqual(removed, ["XAUUSD.DUK"])
                self.assertTrue(pruned)
                symbols = {s["symbol"]: s for s in db.list_mt5_custom_symbols()}
                self.assertEqual(sorted(symbols), ["EURUSD.DUK", "USDJPY.DUK"])
                # Surviving row keeps its import metadata; tick stats refresh.
                self.assertEqual(symbols["USDJPY.DUK"]["ticks"], 9)
                self.assertEqual(symbols["USDJPY.DUK"]["range_label"], "2024-01-01 -> 2024-01-02")
                self.assertIsNotNone(symbols["USDJPY.DUK"]["imported_at"])
                self.assertIsNotNone(symbols["USDJPY.DUK"]["synced_at"])
            finally:
                db.close()

    def test_sync_to_empty_needs_explicit_prune_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._db(temp_dir)
            try:
                db.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", ticks=1)
                db.flush()
                # Guard: an empty live read must not wipe the cache — a wrong
                # suffix/filter would otherwise destroy import history.
                removed, pruned = db.sync_mt5_custom_symbols([])
                self.assertEqual((removed, pruned), ([], False))
                self.assertEqual(
                    [s["symbol"] for s in db.list_mt5_custom_symbols()],
                    ["XAUUSD.DUK"],
                )
                # Explicit confirmation clears.
                removed, pruned = db.sync_mt5_custom_symbols([], prune_empty=True)
                self.assertEqual((removed, pruned), (["XAUUSD.DUK"], True))
                self.assertEqual(db.list_mt5_custom_symbols(), [])
            finally:
                db.close()

    def test_sync_to_empty_on_empty_cache_prunes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._db(temp_dir)
            try:
                removed, pruned = db.sync_mt5_custom_symbols([])
                self.assertEqual((removed, pruned), ([], True))
                self.assertEqual(db.list_mt5_custom_symbols(), [])
            finally:
                db.close()

    def test_sync_persists_without_explicit_flush(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "meta.db"
            db = MetadataDB(db_path)
            db.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", ticks=1)
            db.sync_mt5_custom_symbols([
                {"symbol": "EURUSD.DUK", "source_symbol": "EURUSD", "ticks": 5},
            ])
            db.close()  # no flush() — close must not roll back the sync
            reopened = MetadataDB(db_path)
            try:
                self.assertEqual(
                    [s["symbol"] for s in reopened.list_mt5_custom_symbols()],
                    ["EURUSD.DUK"],
                )
            finally:
                reopened.close()

    def test_sync_survives_wal_sidecar_loss(self) -> None:
        """Prune must live in the main db file, not just the -wal sidecar."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "meta.db"
            db = MetadataDB(db_path)
            db.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", ticks=1)
            db.sync_mt5_custom_symbols([
                {"symbol": "EURUSD.DUK", "source_symbol": "EURUSD", "ticks": 5},
            ])
            db.close()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(db_path) + suffix)
                if sidecar.is_file():
                    sidecar.unlink()
            reopened = MetadataDB(db_path)
            try:
                self.assertEqual(
                    [s["symbol"] for s in reopened.list_mt5_custom_symbols()],
                    ["EURUSD.DUK"],
                )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
