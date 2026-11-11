from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export.mt5_importer as mt5_importer
from export.mt5_importer import (
    Mt5Settings,
    list_terminal_symbols,
    parse_symbols_file_any,
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


def _job_dir(mt5) -> Path:
    return mt5.data_path / "MQL5" / "Files" / "dukascopy_jobs" / JOB_ID


class ParseSymbolsFileTests(unittest.TestCase):
    def test_parses_single_column_sorted_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            p = Path(temp_dir) / "symbols_all.txt"
            p.write_text("EURUSD\nGBPUSD\nEURUSD\n\nusdjpy\n", encoding="utf-8")
            self.assertEqual(
                parse_symbols_file_any([p]),
                ["EURUSD", "GBPUSD", "usdjpy"],
            )

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(
                parse_symbols_file_any([Path(temp_dir) / "nope.txt"]), []
            )


class ListTerminalSymbolsTests(unittest.TestCase):
    def test_returns_symbols_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mt5, appdata = _portable_env(root)
            job_dir = _job_dir(mt5)
            job_dir.mkdir(parents=True)
            (job_dir / "symbols_all.txt").write_text(
                "US30\nEURUSD\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(
                    mt5_importer.time, "time", return_value=JOB_TS
                ):
                    with mock.patch.object(
                        mt5_importer.os, "getpid", return_value=12345
                    ):
                        with mock.patch.object(mt5_importer, "kill_mt5_terminal") as kill:
                            with mock.patch.object(mt5_importer, "wait_mt5_exit"):
                                with mock.patch.object(
                                    mt5_importer, "ensure_script_installed",
                                    return_value=Path("DukascopyTickImport.ex5"),
                                ):
                                    with mock.patch.object(
                                        mt5_importer, "launch_mt5_background"
                                    ) as launch:
                                        with mock.patch.object(
                                            mt5_importer, "write_startup_ini"
                                        ) as write_ini:
                                            with mock.patch.object(
                                                mt5_importer.time, "sleep"
                                            ):
                                                result = list_terminal_symbols(
                                                    {"terminal_exe": str(mt5.terminal_exe),
                                                     "portable": True},
                                                )
            self.assertEqual(result, ["EURUSD", "US30"])
            launch.assert_called_once()
            write_ini.assert_called_once()
            self.assertEqual(write_ini.call_args.kwargs["symbol"], "EURUSD")
            self.assertFalse(job_dir.exists())

    def test_error_progress_raises_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mt5, appdata = _portable_env(root)
            job_dir = _job_dir(mt5)
            job_dir.mkdir(parents=True)
            (job_dir / "progress.txt").write_text(
                "state=error\nphase=list\nmessage=Listing failed\nerror_code=42\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(
                    mt5_importer.time, "time", return_value=JOB_TS
                ):
                    with mock.patch.object(
                        mt5_importer.os, "getpid", return_value=12345
                    ):
                        with mock.patch.object(mt5_importer, "kill_mt5_terminal") as kill:
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
                                                mt5_importer.time,
                                                "monotonic",
                                                side_effect=[0.0, 0.1, 0.2],
                                            ):
                                                with mock.patch.object(
                                                    mt5_importer.time, "sleep"
                                                ):
                                                    with self.assertRaises(RuntimeError) as ctx:
                                                        list_terminal_symbols(
                                                            {"terminal_exe": str(mt5.terminal_exe),
                                                             "portable": True},
                                                        )
            self.assertIn("Listing failed", str(ctx.exception))
            self.assertIn("42", str(ctx.exception))
            self.assertFalse(job_dir.exists())

    def test_timeout_raises_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mt5, appdata = _portable_env(root)
            job_dir = _job_dir(mt5)
            job_dir.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(
                    mt5_importer.time, "time", return_value=JOB_TS
                ):
                    with mock.patch.object(
                        mt5_importer.os, "getpid", return_value=12345
                    ):
                        with mock.patch.object(mt5_importer, "kill_mt5_terminal") as kill:
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
                                                mt5_importer.time,
                                                "monotonic",
                                                side_effect=[0.0, 1000.0],
                                            ):
                                                with mock.patch.object(
                                                    mt5_importer.time, "sleep"
                                                ):
                                                    with self.assertRaises(RuntimeError) as ctx:
                                                        list_terminal_symbols(
                                                            {"terminal_exe": str(mt5.terminal_exe),
                                                             "portable": True},
                                                            timeout_sec=1.0,
                                                        )
            self.assertIn("did not produce the symbol list", str(ctx.exception))
            self.assertFalse(job_dir.exists())


if __name__ == "__main__":
    os.environ.setdefault("APPDATA", tempfile.gettempdir())
    unittest.main()
