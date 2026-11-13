"""Tests for gap scanner full-list repair and `gaps --all-symbols` batch mode."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest import mock

from core.models.instrument import Instrument
from core.services.gap_scanner import GapReport, GapScanner
from storage.metadata_db import _hour_key


def _hours(n, start=datetime(2021, 1, 1, 10)):
    return [start + timedelta(hours=i) for i in range(n)]


def _fake_settings():
    return SimpleNamespace(
        db_path="unused.db",
        data_dir="unused",
        for_job=lambda workers=None: SimpleNamespace(max_workers=15),
    )


def _fake_md():
    md = mock.MagicMock()
    md.list_instruments.return_value = ["eurusd", "xauusd"]
    return md


class _FakeDB:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def status_map(self, instrument_id, start, end):
        return self.statuses


class FullRepairListTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("eurusd", "EUR/USD", "", 100000)
        self.scanner = GapScanner.__new__(GapScanner)
        self.scanner.db = _FakeDB()

    def test_missing_hours_not_sampled_and_feed_repair(self):
        hours = _hours(250)
        report = self.scanner._scan_hours(self.instrument, hours)
        self.assertEqual(report.missing_count, 250)
        self.assertEqual(len(report.missing_hours), 250)
        tasks = self.scanner.build_repair_tasks(self.instrument, report)
        self.assertEqual(len(tasks), 250)

    def test_empty_hours_still_sampled_for_display(self):
        statuses = {_hour_key(h): "empty" for h in _hours(60)}
        self.scanner.db = _FakeDB(statuses)
        report = self.scanner._scan_hours(self.instrument, _hours(60))
        self.assertEqual(report.empty, 60)
        self.assertEqual(len(report.empty_hours), 32)


class AllSymbolsBatchTests(unittest.TestCase):
    def setUp(self):
        self.inst_a = Instrument("eurusd", "EUR/USD", "", 100000)
        self.inst_b = Instrument("xauusd", "XAU/USD", "", 100000)

    def _report(self, missing=0):
        r = GapReport(instrument_id="x")
        r.missing_count = missing
        if missing:
            r.missing_hours = _hours(missing)
        return r

    def _catalog(self):
        catalog = mock.MagicMock()

        def _get(symbol):
            return self.inst_a if symbol == "EURUSD" else self.inst_b

        catalog.get.side_effect = _get
        return catalog

    def test_report_mode_returns_1_if_any_missing(self):
        import cli.commands as commands
        with mock.patch.object(commands, "MetadataDB", return_value=_fake_md()), \
             mock.patch.object(commands, "GapScanner") as gs:
            gs.return_value.scan_all.side_effect = [
                self._report(missing=5), self._report(),
            ]
            args = SimpleNamespace(all_symbols=True, repair=False, start=None, end=None,
                                   workers=None, refetch_empty=False,
                                   max_passes=5, limit=None)
            code = commands.cmd_gaps_all_symbols(_fake_settings(), self._catalog(), args)
            self.assertEqual(code, 1)

    def test_report_mode_returns_0_when_all_complete(self):
        import cli.commands as commands
        with mock.patch.object(commands, "MetadataDB", return_value=_fake_md()), \
             mock.patch.object(commands, "GapScanner") as gs:
            gs.return_value.scan_all.return_value = self._report()
            args = SimpleNamespace(all_symbols=True, repair=False, start=None, end=None,
                                   workers=None, refetch_empty=False,
                                   max_passes=5, limit=None)
            code = commands.cmd_gaps_all_symbols(_fake_settings(), self._catalog(), args)
            self.assertEqual(code, 0)

    def test_repair_loops_until_complete(self):
        import cli.commands as commands
        with mock.patch.object(commands, "MetadataDB", return_value=_fake_md()), \
             mock.patch.object(commands, "GapScanner") as gs, \
             mock.patch.object(commands, "TickStorage"), \
             mock.patch.object(commands, "DownloadEngine") as eng:
            scan_calls = {"n": 0}

            def fake_scan(inst):
                scan_calls["n"] += 1
                return self._report(missing=5) if scan_calls["n"] == 1 else self._report()

            gs.return_value.scan_all.side_effect = fake_scan
            eng.return_value.run.return_value = SimpleNamespace(
                completed=5, empty=0, failed=0, ticks=120)
            args = SimpleNamespace(all_symbols=True, repair=True, start=None, end=None,
                                   workers=None, refetch_empty=False,
                                   max_passes=5, limit=None)
            code = commands.cmd_gaps_all_symbols(_fake_settings(), self._catalog(), args)
            self.assertEqual(code, 0)
            # Two symbols, each scanned at least twice (first pass missing, then complete).
            self.assertGreaterEqual(scan_calls["n"], 4)

    def test_max_passes_terminates_with_failure(self):
        import cli.commands as commands
        with mock.patch.object(commands, "MetadataDB", return_value=_fake_md()), \
             mock.patch.object(commands, "GapScanner") as gs, \
             mock.patch.object(commands, "TickStorage"), \
             mock.patch.object(commands, "DownloadEngine") as eng:
            gs.return_value.scan_all.return_value = self._report(missing=5)
            eng.return_value.run.return_value = SimpleNamespace(
                completed=0, empty=0, failed=5, ticks=0)
            args = SimpleNamespace(all_symbols=True, repair=True, start=None, end=None,
                                   workers=None, refetch_empty=False,
                                   max_passes=2, limit=1)
            code = commands.cmd_gaps_all_symbols(_fake_settings(), self._catalog(), args)
            self.assertEqual(code, 1)

    def test_parser_wiring(self):
        from cli.commands import _build_parser
        p = _build_parser()
        args = p.parse_args(["gaps", "--all-symbols"])
        self.assertTrue(args.all_symbols)
        self.assertIsNone(args.symbol)
        self.assertEqual(args.max_passes, 5)
        self.assertIsNone(args.limit)
        self.assertFalse(args.repair)


if __name__ == "__main__":
    unittest.main()
