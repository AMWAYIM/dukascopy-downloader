from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export.mt5_importer as mt5_importer
from export.mt5_importer import (
    Mt5Settings,
    build_mt5_launch_args,
    candidate_job_dirs,
    diagnose_mt5,
    files_base_dir,
    job_dir_for,
    m30_cache_candidates,
    parse_progress_file_any,
    resolve_mt5_terminal_exe,
)


def _make_terminal(root: Path) -> Path:
    exe = root / "terminal64.exe"
    exe.write_bytes(b"fake")
    return exe


class Mt5PortableTests(unittest.TestCase):
    def test_portable_defaults_data_to_terminal_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": True})
            self.assertTrue(mt5.portable)
            self.assertEqual(mt5.data_path, exe.parent)

    def test_roaming_default_is_not_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            data = root / "data123"
            data.mkdir()
            mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "data_path": str(data)})
            self.assertFalse(mt5.portable)
            self.assertEqual(mt5.data_path, data)

    def test_portable_accepts_truthy_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            mt5 = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": "yes"})
            self.assertTrue(mt5.portable)

    def test_job_dir_routes_per_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            data = root / "roaming-id"
            data.mkdir()
            roaming = Mt5Settings.from_dict({"terminal_exe": str(exe), "data_path": str(data)})
            portable = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": True})
            self.assertEqual(
                job_dir_for(portable, "j1"),
                portable.data_path / "MQL5" / "Files" / "dukascopy_jobs" / "j1",
            )
            roaming_base = files_base_dir(roaming)
            self.assertEqual(job_dir_for(roaming, "j1"), roaming_base / "dukascopy_jobs" / "j1")
            candidates = candidate_job_dirs(portable, "j1")
            self.assertEqual(len(candidates), 2)
            self.assertIn(job_dir_for(portable, "j1"), candidates)

    def test_launch_args_include_portable_flag(self) -> None:
        exe = Path(r"C:\mt5\terminal64.exe")
        ini = Path(r"C:\work\import.ini")
        self.assertEqual(
            build_mt5_launch_args(exe, ini, portable=False),
            [str(exe), f"/config:{ini}"],
        )
        self.assertEqual(
            build_mt5_launch_args(exe, ini, portable=True),
            [str(exe), f"/config:{ini}", "/portable"],
        )

    def test_progress_poll_reads_fallback_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = root / "primary" / "progress.txt"
            fallback = root / "fallback" / "progress.txt"
            fallback.parent.mkdir(parents=True)
            fallback.write_text("state=done\nphase=import\n", encoding="utf-8")
            data = parse_progress_file_any([primary, fallback])
            self.assertEqual(data.get("state"), "done")

    def test_m30_cache_covers_both_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            portable = Mt5Settings.from_dict({"terminal_exe": str(exe), "portable": True})
            candidates = m30_cache_candidates("EURUSD.DUK", portable)
            self.assertEqual(len(candidates), 2)
            self.assertTrue(all(p.name == "M30_EURUSD.DUK.bin" for p in candidates))

    def test_diagnose_warns_on_exe_folder_without_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            diag = diagnose_mt5({"terminal_exe": str(exe), "data_path": str(root)})
            self.assertTrue(diag["ok"])
            self.assertEqual(diag["mode"], "roaming")
            self.assertTrue(any("portable" in w.lower() for w in diag["warnings"]))

    def test_diagnose_ok_for_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            diag = diagnose_mt5({"terminal_exe": str(exe), "portable": True})
            self.assertTrue(diag["ok"])
            self.assertEqual(diag["mode"], "portable")
            self.assertEqual(diag["launch_args"][-1], "/portable")


class Mt5TerminalAutoDetectTests(unittest.TestCase):
    def _appdata_with_install(self, root: Path, instance_id: str = "ID123") -> Path:
        install_dir = root / "install"
        install_dir.mkdir()
        exe = _make_terminal(install_dir)
        data_id = root / "appdata" / "MetaQuotes" / "Terminal" / instance_id
        data_id.mkdir(parents=True)
        (data_id / "origin.txt").write_text(str(install_dir), encoding="utf-8")
        return exe

    def test_blank_terminal_resolves_via_origin_txt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = self._appdata_with_install(root)
            appdata = root / "appdata"
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=[]):
                    found, source = resolve_mt5_terminal_exe()
            self.assertEqual(found, exe)
            self.assertIn("origin", source)

    def test_blank_terminal_resolves_utf16_origin_txt(self) -> None:
        # Real terminals write origin.txt as UTF-16LE.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = root / "install"
            install_dir.mkdir()
            exe = _make_terminal(install_dir)
            data_id = root / "appdata" / "MetaQuotes" / "Terminal" / "ID123"
            data_id.mkdir(parents=True)
            (data_id / "origin.txt").write_text(str(install_dir), encoding="utf-16")
            appdata = root / "appdata"
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=[]):
                    found, _source = resolve_mt5_terminal_exe()
            self.assertEqual(found, exe)

    def test_blank_terminal_in_from_dict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = self._appdata_with_install(root)
            data_id = root / "appdata" / "MetaQuotes" / "Terminal" / "ID123"
            appdata = root / "appdata"
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=[]):
                    mt5 = Mt5Settings.from_dict({"terminal_exe": "", "data_path": str(data_id)})
            self.assertEqual(mt5.terminal_exe, exe)

    def test_blank_terminal_with_nothing_found_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            appdata = Path(temp_dir) / "empty-appdata"
            appdata.mkdir()
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=[]):
                    with mock.patch.object(mt5_importer, "MT5_DEFAULT_INSTALL_PATHS", ()):
                        with self.assertRaises(ValueError):
                            resolve_mt5_terminal_exe()

    def test_multiple_running_terminals_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a").mkdir(parents=True, exist_ok=True)
            (root / "b").mkdir(parents=True, exist_ok=True)
            exe_a = _make_terminal(root / "a")
            exe_b = _make_terminal(root / "b")
            procs = [
                {"pid": 1, "path": str(exe_a)},
                {"pid": 2, "path": str(exe_b)},
            ]
            with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=procs):
                with self.assertRaises(ValueError):
                    resolve_mt5_terminal_exe()

    def test_single_running_terminal_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = _make_terminal(root)
            procs = [{"pid": 7, "path": str(exe)}]
            with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=procs):
                found, source = resolve_mt5_terminal_exe()
            self.assertEqual(found, exe)
            self.assertEqual(source, "auto:running-process")

    def test_diagnose_reports_auto_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._appdata_with_install(root)
            data_id = root / "appdata" / "MetaQuotes" / "Terminal" / "ID123"
            appdata = root / "appdata"
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                with mock.patch.object(mt5_importer, "list_terminal_processes", return_value=[]):
                    diag = diagnose_mt5({"terminal_exe": "", "data_path": str(data_id)})
            self.assertTrue(diag["ok"])
            self.assertIn("auto", diag["terminal_source"])


class Mt5ValidateConfigTests(unittest.TestCase):
    def test_blank_config_passes_without_probing(self) -> None:
        with mock.patch.object(
            mt5_importer, "resolve_mt5_terminal_exe",
            side_effect=AssertionError("must not probe"),
        ):
            Mt5Settings.validate_config({"terminal_exe": "", "data_path": ""})

    def test_explicit_missing_terminal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Mt5Settings.validate_config({"terminal_exe": r"C:\nope\terminal64.exe"})

    def test_explicit_missing_data_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = _make_terminal(Path(temp_dir))
            with self.assertRaises(ValueError):
                Mt5Settings.validate_config({
                    "terminal_exe": str(exe),
                    "data_path": str(Path(temp_dir) / "missing"),
                })

    def test_diagnose_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = _make_terminal(Path(temp_dir))
            diag = diagnose_mt5({"terminal_exe": str(exe), "portable": True})
            for key in ("ok", "mode", "terminal_exe", "launch_args", "job_base", "warnings"):
                self.assertIn(key, diag)


class Mt5CliTests(unittest.TestCase):
    def test_mt5_config_parser(self) -> None:
        from cli.commands import _build_parser

        args = _build_parser().parse_args(["mt5-config", "--portable", "--suffix", "DUK"])
        self.assertTrue(args.portable)
        self.assertEqual(args.suffix, "DUK")

    def test_mt5_doctor_parser(self) -> None:
        from cli.commands import _build_parser

        args = _build_parser().parse_args(["mt5-doctor"])
        self.assertEqual(args.command, "mt5-doctor")


if __name__ == "__main__":
    os.environ.setdefault("APPDATA", tempfile.gettempdir())
    unittest.main()
