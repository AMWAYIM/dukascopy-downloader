from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import export.mt5_importer as mt5_importer


def _fake_terminal(root: Path) -> Path:
    exe = root / "terminal64.exe"
    exe.write_bytes(b"fake")
    return exe


class Mt5KillTimeoutTests(unittest.TestCase):
    def test_kill_swallows_timeout_and_returns(self) -> None:
        # Must not raise even when the PowerShell probe times out (best-effort).
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_terminal(Path(td))
            with mock.patch.object(
                mt5_importer.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
            ):
                mt5_importer.kill_mt5_terminal(exe)

    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_kill_probes_with_shared_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_terminal(Path(td))
            with mock.patch.object(
                mt5_importer.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                mt5_importer.kill_mt5_terminal(exe)
                self.assertEqual(
                    run.call_args.kwargs["timeout"],
                    mt5_importer.MT5_PS_TIMEOUT_SEC,
                )


class Mt5RunningTimeoutTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_is_running_delegates_to_wmi_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_terminal(Path(td))
            with mock.patch.object(
                mt5_importer.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
            ), mock.patch.object(
                mt5_importer,
                "_terminal_running_via_wmi",
                return_value=True,
            ) as wmi:
                self.assertTrue(mt5_importer.is_mt5_running(exe))
                wmi.assert_called_once_with(exe)

    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_is_running_probes_with_shared_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = _fake_terminal(Path(td))
            with mock.patch.object(
                mt5_importer.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1),
            ) as run:
                self.assertFalse(mt5_importer.is_mt5_running(exe))
                self.assertEqual(
                    run.call_args.kwargs["timeout"],
                    mt5_importer.MT5_PS_TIMEOUT_SEC,
                )


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell"), "requires Windows PowerShell"
)
class Mt5WmiFallbackTests(unittest.TestCase):
    def _make_terminal(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return _fake_terminal(Path(tmp.name))

    def test_match_returns_true(self) -> None:
        exe = self._make_terminal()
        with mock.patch.object(
            mt5_importer.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=str(exe.resolve()), returncode=0),
        ):
            self.assertTrue(mt5_importer._terminal_running_via_wmi(exe))

    def test_no_match_returns_false(self) -> None:
        exe = self._make_terminal()
        with mock.patch.object(
            mt5_importer.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="", returncode=0),
        ):
            self.assertFalse(mt5_importer._terminal_running_via_wmi(exe))

    def test_timeout_assumes_running(self) -> None:
        exe = self._make_terminal()
        with mock.patch.object(
            mt5_importer.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=5),
        ):
            self.assertTrue(mt5_importer._terminal_running_via_wmi(exe))

    def test_oserror_assumes_running(self) -> None:
        exe = self._make_terminal()
        with mock.patch.object(
            mt5_importer.subprocess, "run", side_effect=OSError()
        ):
            self.assertTrue(mt5_importer._terminal_running_via_wmi(exe))

    def test_uses_wmi_timeout_constant(self) -> None:
        exe = self._make_terminal()
        with mock.patch.object(
            mt5_importer.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="", returncode=0),
        ) as run:
            mt5_importer._terminal_running_via_wmi(exe)
            self.assertEqual(
                run.call_args.kwargs["timeout"],
                mt5_importer.MT5_PS_WMI_TIMEOUT_SEC,
            )


class Mt5ListTimeoutTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows")
    def test_list_terminal_processes_swallows_timeout(self) -> None:
        with mock.patch.object(
            mt5_importer.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
        ):
            self.assertEqual(mt5_importer.list_terminal_processes(), [])


if __name__ == "__main__":
    unittest.main()
