from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import export.mt5_importer as mt5_importer
from export.mt5_importer import (
    Mt5Settings,
    list_terminal_symbols,
    load_last_known_symbols,
    save_last_known_symbols,
    warn_if_origin_unknown,
)

JOB_TS = 1700000000.0
JOB_ID = "sym-12345-1700000000000"


def _make_terminal(root: Path) -> Path:
    exe = root / "terminal64.exe"
    exe.write_bytes(b"fake")
    return exe


def _portable_env(root: Path):
    appdata = root / "appdata"
    appdata.mkdir(exist_ok=True)
    exe = _make_terminal(root)
    mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": True})
    return mt5, appdata


class SymbolCacheTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "mt5_symbols_cache.json"
            save_last_known_symbols(cache, ["us30", "EURUSD", "eurusd"])
            self.assertEqual(
                load_last_known_symbols(cache), ["EURUSD", "eurusd", "us30"]
            )

    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(
                load_last_known_symbols(Path(temp_dir) / "nope.json")
            )

    def test_corrupt_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"
            cache.write_text("not json{{", encoding="utf-8")
            self.assertIsNone(load_last_known_symbols(cache))

    def test_stale_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"
            old = datetime.now(timezone.utc) - timedelta(hours=48)
            cache.write_text(
                json.dumps({"symbols": ["EURUSD"], "generated_at": old.isoformat()}),
                encoding="utf-8",
            )
            self.assertIsNone(load_last_known_symbols(cache))

    def test_wrong_shape_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"
            cache.write_text(json.dumps({"symbols": "EURUSD"}), encoding="utf-8")
            self.assertIsNone(load_last_known_symbols(cache))


class OriginWarningTests(unittest.TestCase):
    def _cache_with(self, root: Path, symbols: list[str] | None) -> Path:
        cache = root / "mt5_symbols_cache.json"
        if symbols is not None:
            save_last_known_symbols(cache, symbols)
        return cache

    def test_mismatch_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self._cache_with(Path(temp_dir), ["EURUSD", "US30"])
            msg = warn_if_origin_unknown("GBPUSD", cache)
            self.assertIsNotNone(msg)
            assert msg is not None
            self.assertIn("GBPUSD", msg)
            self.assertIn("last-known", msg)

    def test_match_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self._cache_with(Path(temp_dir), ["EURUSD"])
            self.assertIsNone(warn_if_origin_unknown("EURUSD", cache))

    def test_blank_origin_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self._cache_with(Path(temp_dir), ["EURUSD"])
            self.assertIsNone(warn_if_origin_unknown("", cache))

    def test_disabled_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self._cache_with(Path(temp_dir), ["EURUSD"])
            self.assertIsNone(
                warn_if_origin_unknown("GBPUSD", cache, enabled=False)
            )

    def test_no_cache_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self._cache_with(Path(temp_dir), None)
            self.assertIsNone(warn_if_origin_unknown("GBPUSD", cache))


class ListTerminalSymbolsCacheTests(unittest.TestCase):
    def test_saves_cache_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mt5, appdata = _portable_env(root)
            job_dir = mt5.data_path / "MQL5" / "Files" / "dukascopy_jobs" / JOB_ID
            job_dir.mkdir(parents=True)
            (job_dir / "symbols_all.txt").write_text(
                "US30\nEURUSD\n", encoding="utf-8"
            )
            cache_path = root / "mt5_symbols_cache.json"
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(
                    mt5_importer.time, "time", return_value=JOB_TS
                ):
                    with mock.patch.object(
                        mt5_importer.os, "getpid", return_value=12345
                    ):
                        with mock.patch.object(mt5_importer, "kill_mt5_terminal"):
                            with mock.patch.object(mt5_importer, "wait_mt5_exit"):
                                with mock.patch.object(
                                    mt5_importer, "ensure_script_installed",
                                    return_value=Path("DukascopyTickImport.ex5"),
                                ):
                                    with mock.patch.object(
                                        mt5_importer, "launch_mt5_background"
                                    ):
                                        with mock.patch.object(
                                            mt5_importer, "write_startup_ini"
                                        ):
                                            with mock.patch.object(
                                                mt5_importer.time, "sleep"
                                            ):
                                                result = list_terminal_symbols(
                                                    {"terminal_exe": str(mt5.terminal_exe),
                                                     "portable": True},
                                                    cache_path=cache_path,
                                                )
            self.assertEqual(result, ["EURUSD", "US30"])
            self.assertEqual(
                load_last_known_symbols(cache_path), ["EURUSD", "US30"]
            )


if __name__ == "__main__":
    os.environ.setdefault("APPDATA", tempfile.gettempdir())
    unittest.main()
