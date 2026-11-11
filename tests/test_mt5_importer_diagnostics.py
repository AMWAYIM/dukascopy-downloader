from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from export.mt5_importer import (
    Mt5Settings,
    parse_fresh_error_progress,
    root_progress_candidates,
    terminal_log_tail,
    validate_preset_job_id,
)


def _make_terminal(root: Path) -> Path:
    exe = root / "terminal64.exe"
    exe.write_bytes(b"fake")
    return exe


class RootProgressCandidatesTests(unittest.TestCase):
    def test_portable_returns_primary_and_common(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            appdata = root / "appdata"
            appdata.mkdir()
            mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": True})
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                candidates = root_progress_candidates(mt5)
            self.assertEqual(
                candidates[0],
                exe.parent / "MQL5" / "Files" / "dukascopy_jobs" / "progress.txt",
            )
            self.assertIn(
                appdata / "MetaQuotes" / "Terminal" / "Common" / "Files"
                / "dukascopy_jobs" / "progress.txt",
                candidates,
            )

    def test_roaming_returns_common_and_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            data = root / "roaming-id"
            data.mkdir()
            appdata = root / "appdata"
            appdata.mkdir()
            mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "data_path": str(data)})
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                candidates = root_progress_candidates(mt5)
            self.assertEqual(
                candidates[0],
                appdata / "MetaQuotes" / "Terminal" / "Common" / "Files"
                / "dukascopy_jobs" / "progress.txt",
            )
            self.assertIn(
                data / "MQL5" / "Files" / "dukascopy_jobs" / "progress.txt",
                candidates,
            )


class ParseFreshErrorProgressTests(unittest.TestCase):
    def _write_progress(self, path: Path, state: str, message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"state={state}\nphase=config\nmessage={message}\nerror_code=1\n",
            encoding="utf-8",
        )

    def test_returns_error_when_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.txt"
            self._write_progress(path, "error", "JobId input is required")
            data = parse_fresh_error_progress([path], min_mtime=0.0)
            self.assertEqual(data["state"], "error")
            self.assertEqual(data["message"], "JobId input is required")

    def test_ignores_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.txt"
            self._write_progress(path, "error", "JobId input is required")
            future = time.time() + 3600
            data = parse_fresh_error_progress([path], min_mtime=future)
            self.assertEqual(data, {})

    def test_ignores_non_error_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "progress.txt"
            self._write_progress(path, "done", "Import complete")
            data = parse_fresh_error_progress([path], min_mtime=0.0)
            self.assertEqual(data, {})

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.txt"
            data = parse_fresh_error_progress([path], min_mtime=0.0)
            self.assertEqual(data, {})


class TerminalLogTailTests(unittest.TestCase):
    def test_filters_importer_lines_and_picks_newest_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logs = root / "MQL5" / "Logs"
            logs.mkdir(parents=True)
            (logs / "20261110.log").write_text(
                "other noise\n"
                "DukascopyTickImport (BTCUSD,M1) JobId input is required\n",
                encoding="utf-8",
            )
            (logs / "20261111.log").write_text(
                "JQ 0 20:05:58 DukascopyTickImport (BTCUSD,M1) JobId input is required\n"
                "QE 0 20:18:51 DukascopyTickImport (EURUSD,M1) OnStart v1.23\n",
                encoding="utf-8",
            )
            tail = terminal_log_tail(root, max_lines=10)
            self.assertIn("JobId input is required", tail)
            self.assertIn("OnStart v1.23", tail)
            self.assertNotIn("other noise", tail)

    def test_no_logs_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(terminal_log_tail(Path(temp_dir)), "")


class ValidatePresetJobIdTests(unittest.TestCase):
    def test_valid_preset_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preset = Path(temp_dir) / "dukascopy_import.set"
            preset.write_text("; generated\nJobId=abc-123\n", encoding="utf-8")
            validate_preset_job_id(preset, "abc-123")  # should not raise

    def test_missing_preset_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preset = Path(temp_dir) / "missing.set"
            with self.assertRaises(ValueError):
                validate_preset_job_id(preset, "abc-123")

    def test_wrong_job_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preset = Path(temp_dir) / "dukascopy_import.set"
            preset.write_text("JobId=other\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_preset_job_id(preset, "abc-123")


if __name__ == "__main__":
    os.environ.setdefault("APPDATA", tempfile.gettempdir())
    unittest.main()
