"""Tests for the MT5 custom-symbol stats cache and listing merge."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from export import mt5_symbol_manager as mgr
from export.mt5_importer import _hour_file_first_last_ms, _sources_span
from export.mt5_symbol_manager import (
    CustomSymbolInfo,
    _merge_cached_ticks,
    load_symbol_stats,
    prune_symbol_stats,
    save_symbol_stats,
    symbol_stats_path,
)
from storage.tick_format import write_hour_file


class SymbolStatsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_when_missing(self) -> None:
        self.assertEqual(load_symbol_stats(self.data_dir), {})

    def test_save_load_roundtrip(self) -> None:
        save_symbol_stats(self.data_dir, "EURUSD.DUK", 140, 1609106796584, 1791763199157)
        self.assertEqual(
            load_symbol_stats(self.data_dir)["EURUSD.DUK"],
            {"ticks": 140, "first_ms": 1609106796584, "last_ms": 1791763199157},
        )

    def test_append_accumulates_and_widens_span(self) -> None:
        save_symbol_stats(self.data_dir, "XAUUSD.DUK", 100, 5000, 10000)
        save_symbol_stats(self.data_dir, "XAUUSD.DUK", 50, 3000, 12000)
        self.assertEqual(
            load_symbol_stats(self.data_dir)["XAUUSD.DUK"],
            {"ticks": 150, "first_ms": 3000, "last_ms": 12000},
        )

    def test_replace_overwrites_count(self) -> None:
        save_symbol_stats(self.data_dir, "USDJPY.DUK", 100, 1, 2, append=True)
        save_symbol_stats(self.data_dir, "USDJPY.DUK", 42, 7, 9, append=False)
        self.assertEqual(
            load_symbol_stats(self.data_dir)["USDJPY.DUK"],
            {"ticks": 42, "first_ms": 7, "last_ms": 9},
        )

    def test_prune_removes_entry(self) -> None:
        save_symbol_stats(self.data_dir, "EURUSD.DUK", 1, 1, 2)
        prune_symbol_stats(self.data_dir, "EURUSD.DUK")
        self.assertNotIn("EURUSD.DUK", load_symbol_stats(self.data_dir))

    def test_corrupt_file_reads_empty(self) -> None:
        symbol_stats_path(self.data_dir).write_text("{not json", encoding="utf-8")
        self.assertEqual(load_symbol_stats(self.data_dir), {})

    def test_non_dict_root_reads_empty(self) -> None:
        symbol_stats_path(self.data_dir).write_text("[]", encoding="utf-8")
        self.assertEqual(load_symbol_stats(self.data_dir), {})


class MergeCachedTicksTests(unittest.TestCase):
    def test_fills_known_counts_and_leaves_unknown(self) -> None:
        rows = [
            CustomSymbolInfo(symbol="EURUSD.DUK", ticks=0, first_ms=1, last_ms=2),
            CustomSymbolInfo(symbol="GBPUSD.DUK", ticks=0, first_ms=3, last_ms=4),
        ]
        _merge_cached_ticks(rows, {"EURUSD.DUK": {"ticks": 140, "first_ms": 1, "last_ms": 2}})
        self.assertEqual(rows[0].ticks, 140)
        self.assertEqual(rows[1].ticks, 0)

    def test_ignores_zero_cached_count(self) -> None:
        row = CustomSymbolInfo(symbol="EURUSD.DUK", ticks=0, first_ms=1, last_ms=2)
        _merge_cached_ticks([row], {"EURUSD.DUK": {"ticks": 0, "first_ms": 1, "last_ms": 2}})
        self.assertEqual(row.ticks, 0)


class ListMergeIntegrationTests(unittest.TestCase):
    def test_list_merges_cached_counts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data_dir = Path(d)
            job_dir = data_dir / "jobs" / "j1"
            job_dir.mkdir(parents=True)
            (job_dir / "symbols.txt").write_text(
                "EURUSD.DUK|0|1609106796584|1791763199157|Custom\\dukascopy\\EURUSD.DUK\n"
                "UNKNOWN.DUK|0|1|2|Custom\\dukascopy\\UNKNOWN.DUK\n",
                encoding="utf-8",
            )
            save_symbol_stats(data_dir, "EURUSD.DUK", 140, 1609106796584, 1791763199157)
            with mock.patch.object(mgr, "_run_symbol_job"):
                with mock.patch.object(mgr, "candidate_job_dirs", return_value=[job_dir]):
                    symbols = mgr.list_custom_symbols({"custom_suffix": ".DUK"}, data_dir)
            by_name = {s["symbol"]: s for s in symbols}
            self.assertEqual(by_name["EURUSD.DUK"]["ticks"], 140)
            self.assertEqual(by_name["UNKNOWN.DUK"]["ticks"], 0)


class SourceSpanTests(unittest.TestCase):
    def test_span_across_hour_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p1 = root / "h1.bin"
            p2 = root / "h2.bin"
            write_hour_file(p1, [1000, 2000, 3000], [1.0, 1.1, 1.2], [1.2, 1.3, 1.4])
            write_hour_file(p2, [4000, 5000], [1.2, 1.3], [1.4, 1.5])
            self.assertEqual(_hour_file_first_last_ms(p1), (1000, 3000))
            self.assertEqual(_hour_file_first_last_ms(p2), (4000, 5000))
            self.assertEqual(_sources_span([p1, p2]), (1000, 5000))

    def test_empty_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.bin"
            p.write_bytes(b"\x00\x00\x00\x00")
            self.assertIsNone(_hour_file_first_last_ms(p))


if __name__ == "__main__":
    os.environ.setdefault("APPDATA", tempfile.gettempdir())
    unittest.main()
