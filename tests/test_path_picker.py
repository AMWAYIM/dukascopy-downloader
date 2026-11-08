"""Native path-picker tests (rows #1, #2).

The PowerShell dialog itself cannot run in CI/headless contexts, so the runner
is tested with a mocked subprocess; the endpoint is tested by patching the
runner and simulating loopback/remote clients.
"""
from __future__ import annotations

import ctypes
import io
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from web import app as app_module
from web.app import PathPickRequest, pick_path
from web import path_picker as picker_module


def _fake_proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["powershell"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class PathPickerRunnerTests(unittest.TestCase):
    def setUp(self):
        self._interactive = mock.patch.object(picker_module, "has_interactive_desktop", return_value=True)
        self._interactive.start()
        self.addCleanup(self._interactive.stop)

    def test_folder_mode_returns_path(self):
        picked = Path(r"D:\Trading\MT5\TickData")
        with mock.patch.object(picker_module, "subprocess") as mock_sp:
            mock_sp.run.return_value = _fake_proc(str(picked) + "\n")
            result = picker_module.show_path_picker("folder")
        self.assertEqual(result, picked)
        self.assertEqual(mock_sp.run.call_args.kwargs["env"]["DUKE_PICKER_MODE"], "folder")

    def test_file_mode_sets_initial_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            initial = Path(tmp)
            picked = initial / "terminal64.exe"
            with mock.patch.object(picker_module, "subprocess") as mock_sp:
                mock_sp.run.return_value = _fake_proc(str(picked) + "\n")
                result = picker_module.show_path_picker("file", initial)
            self.assertEqual(result, picked)
            env = mock_sp.run.call_args.kwargs["env"]
            self.assertEqual(env["DUKE_PICKER_MODE"], "file")
            self.assertEqual(env["DUKE_PICKER_INITIAL"], str(initial))

    def test_cancel_returns_none(self):
        with mock.patch.object(picker_module, "subprocess") as mock_sp:
            mock_sp.run.return_value = _fake_proc("__CANCELLED__\n")
            self.assertIsNone(picker_module.show_path_picker("folder"))

    def test_nonzero_exit_raises(self):
        with mock.patch.object(picker_module, "subprocess") as mock_sp:
            mock_sp.run.return_value = _fake_proc("", returncode=1, stderr="boom")
            with self.assertRaises(RuntimeError):
                picker_module.show_path_picker("folder")

    def test_timeout_raises(self):
        with mock.patch.object(picker_module, "subprocess") as mock_sp:
            mock_sp.run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=1)
            with self.assertRaises(RuntimeError):
                picker_module.show_path_picker("folder")

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            picker_module.show_path_picker("weird")  # type: ignore[arg-type]

    def test_non_interactive_session_raises(self):
        with mock.patch.object(picker_module, "has_interactive_desktop", return_value=False):
            with self.assertRaises(RuntimeError):
                picker_module.show_path_picker("folder")


class InteractiveDesktopDetectionTests(unittest.TestCase):
    def test_detects_no_desktop(self):
        with mock.patch.object(ctypes, "windll") as mock_windll:
            mock_windll.user32.OpenInputDesktop.return_value = 0
            self.assertFalse(picker_module.has_interactive_desktop())

    def test_true_when_desktop_present(self):
        with mock.patch.object(ctypes, "windll") as mock_windll:
            mock_windll.user32.OpenInputDesktop.return_value = 12345
            self.assertTrue(picker_module.has_interactive_desktop())


class BusyLockTests(unittest.TestCase):
    def setUp(self):
        self._interactive = mock.patch.object(picker_module, "has_interactive_desktop", return_value=True)
        self._interactive.start()
        self.addCleanup(self._interactive.stop)

    def test_lock_held_means_busy(self):
        picker_module._picker_lock.acquire()
        try:
            self.assertTrue(picker_module.is_picker_busy())
        finally:
            picker_module._picker_lock.release()
        self.assertFalse(picker_module.is_picker_busy())

    def test_show_raises_when_busy(self):
        picker_module._picker_lock.acquire()
        try:
            with mock.patch.object(picker_module, "subprocess") as mock_sp:
                with self.assertRaises(RuntimeError) as ctx:
                    picker_module.show_path_picker("folder")
            self.assertIn("already open", str(ctx.exception))
            mock_sp.run.assert_not_called()
        finally:
            picker_module._picker_lock.release()


class _FakeWorkerProc:
    def __init__(self, reply: str):
        self._reply = reply
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(reply + "\n")

    def poll(self):
        return None


class WorkerPickerTests(unittest.TestCase):
    def setUp(self):
        self._interactive = mock.patch.object(picker_module, "has_interactive_desktop", return_value=True)
        self._interactive.start()
        self.addCleanup(self._interactive.stop)
        self._worker_patch = mock.patch.object(picker_module, "_spawn_worker")
        self._spawn = self._worker_patch.start()
        self.addCleanup(self._worker_patch.stop)

    def test_worker_returns_path(self):
        self._spawn.return_value = _FakeWorkerProc('{"path":"D:\\\\Trading\\\\TickData"}')
        result = picker_module.show_path_picker("folder")
        self.assertEqual(result, Path(r"D:\Trading\TickData"))
        sent = self._spawn.return_value.stdin.getvalue()
        self.assertIn('"mode": "folder"', sent)

    def test_worker_cancel_returns_none(self):
        self._spawn.return_value = _FakeWorkerProc('{"path":null}')
        self.assertIsNone(picker_module.show_path_picker("folder"))

    def test_worker_error_falls_back_to_oneshot(self):
        # Worker returns garbage -> json parse fails -> fallback to one-shot
        # (mocked subprocess.run), which still yields the path.
        self._spawn.return_value = _FakeWorkerProc("not json")
        picked = Path(r"D:\Fallback\Folder")
        with mock.patch.object(picker_module, "subprocess") as mock_sp:
            mock_sp.run.return_value = _fake_proc(str(picked) + "\n")
            result = picker_module.show_path_picker("folder")
        self.assertEqual(result, picked)
        self.assertTrue(mock_sp.run.called)


class PathPickEndpointTests(unittest.TestCase):
    def setUp(self):
        self._interactive = mock.patch.object(app_module, "has_interactive_desktop", return_value=True)
        self._interactive.start()
        self.addCleanup(self._interactive.stop)

    def _request(self, host: str):
        return SimpleNamespace(client=SimpleNamespace(host=host))

    def test_loopback_folder_returns_path(self):
        import asyncio

        picked = Path(r"D:\Trading\TickData")
        with mock.patch.object(app_module, "show_path_picker", return_value=picked) as mock_pick:
            res = asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("127.0.0.1")))
        self.assertEqual(res["path"], str(picked))
        mock_pick.assert_called_once_with("folder", None)

    def test_loopback_ipv6(self):
        import asyncio
        with mock.patch.object(app_module, "show_path_picker", return_value=None):
            res = asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("::1")))
        self.assertIsNone(res["path"])

    def test_remote_host_rejected(self):
        import asyncio
        with mock.patch.object(app_module, "show_path_picker") as mock_pick:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("192.168.1.50")))
        self.assertEqual(ctx.exception.status_code, 403)
        mock_pick.assert_not_called()

    def test_cancel_returns_null_path(self):
        import asyncio
        with mock.patch.object(app_module, "show_path_picker", return_value=None):
            res = asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("127.0.0.1")))
        self.assertIsNone(res["path"])

    def test_runner_error_becomes_500(self):
        import asyncio
        with mock.patch.object(
            app_module, "show_path_picker",
            side_effect=RuntimeError("no powershell"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("127.0.0.1")))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("picker", str(ctx.exception.detail).lower())

    def test_non_interactive_session_returns_409(self):
        import asyncio
        with mock.patch.object(app_module, "has_interactive_desktop", return_value=False), \
             mock.patch.object(app_module, "show_path_picker") as mock_pick:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("127.0.0.1")))
        self.assertEqual(ctx.exception.status_code, 409)
        mock_pick.assert_not_called()

    def test_busy_returns_409(self):
        import asyncio
        with mock.patch.object(app_module, "is_picker_busy", return_value=True), \
             mock.patch.object(app_module, "show_path_picker") as mock_pick:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(pick_path(PathPickRequest(mode="folder"), self._request("127.0.0.1")))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("already open", str(ctx.exception.detail))
        mock_pick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
