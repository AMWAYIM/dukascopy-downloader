"""CLI tests for `data-config` (row #3) and `migrate-data` (row #4)."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cli.commands import _acquire_data_dir_lock, main
from config import data_config
from config.data_config import DATA_DIR_ENV_VAR, load_configured_data_dir


class _CliHarness(unittest.TestCase):
    """Points the persistent config file at a temp path; resets the env var."""

    def setUp(self):
        self._env_patch = mock.patch.dict(os.environ, {DATA_DIR_ENV_VAR: ""})
        self._env_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._cfg = self._tmp_path / "data.json"
        self._cfg_patch = mock.patch.object(data_config, "config_file", return_value=self._cfg)
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()
        self._tmp.cleanup()
        self._env_patch.stop()

    def _run(self, argv):
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            code = main(argv)
        return code, out_buf.getvalue(), err_buf.getvalue()


class DataConfigCliTests(_CliHarness):
    def test_show_default(self):
        code, out, _ = self._run(["data-config", "--show"])
        self.assertEqual(code, 0)
        self.assertIn("effective data root", out)
        self.assertIn("source              : default", out)

    def test_set_persists_and_show_reports_file(self):
        target = self._tmp_path / "newroot"
        code, out, _ = self._run(["data-config", "--set", str(target)])
        self.assertEqual(code, 0)
        self.assertIn("data root saved", out)
        self.assertEqual(load_configured_data_dir(), target)
        self.assertTrue(target.is_dir(), "validate_data_dir should create the root")

        code, out, _ = self._run(["data-config", "--show"])
        self.assertIn(str(target), out)
        self.assertIn("source              : file", out)

    def test_set_rejects_relative(self):
        code, _, err = self._run(["data-config", "--set", "relative/path"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIsNone(load_configured_data_dir())

    def test_clear_returns_to_default(self):
        target = self._tmp_path / "newroot"
        code, _, _ = self._run(["data-config", "--set", str(target)])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["data-config", "--clear"])
        self.assertEqual(code, 0)
        self.assertIsNone(load_configured_data_dir())
        self.assertIn("default", out)

    def test_show_env_source(self):
        with mock.patch.dict(
            os.environ, {DATA_DIR_ENV_VAR: str(self._tmp_path / "env-root")}
        ):
            code, out, _ = self._run(["data-config", "--show"])
        self.assertEqual(code, 0)
        self.assertIn("source              : env", out)


class MigrateDataCliTests(_CliHarness):
    """Sets the effective data root to a populated fixture via the env var."""

    def setUp(self):
        super().setUp()
        self.src = self._tmp_path / "old-root"
        sym = self.src / "EURUSD" / "2026" / "10" / "09"
        sym.mkdir(parents=True)
        (sym / "14.bin").write_bytes(b"tick-data")
        (self.src / "metadata.db").write_bytes(b"sqlite")
        (self.src / ".web.lock").write_bytes(b"\n9999")
        self.dst = self._tmp_path / "new-root"
        self.dst.mkdir(parents=True)
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ, {DATA_DIR_ENV_VAR: str(self.src)}
        )
        self._env_patch.start()

    def test_migrate_copies_verifies_and_deletes(self):
        code, out, err = self._run(
            ["migrate-data", "--to", str(self.dst), "--delete-source"]
        )
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.dst / "EURUSD" / "2026" / "10" / "09" / "14.bin").is_file())
        self.assertFalse((self.dst / ".web.lock").exists())
        self.assertFalse(self.src.exists(), "source should be deleted")
        self.assertIn("Verified 2 files", out)

    def test_migrate_without_delete_source_keeps_source(self):
        code, out, err = self._run(["migrate-data", "--to", str(self.dst)])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.src.exists())
        self.assertIn("--delete-source", out)

    def test_migrate_refuses_non_empty_destination(self):
        (self.dst / "existing.txt").write_text("x", encoding="utf-8")
        code, _, err = self._run(["migrate-data", "--to", str(self.dst)])
        self.assertEqual(code, 1)
        self.assertIn("not empty", err)

    def test_migrate_force_allows_non_empty_destination(self):
        (self.dst / "existing.txt").write_text("x", encoding="utf-8")
        code, out, err = self._run(["migrate-data", "--to", str(self.dst), "--force"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.dst / "EURUSD").is_dir())

    def test_migrate_refuses_while_web_lock_held(self):
        holder = _acquire_data_dir_lock(self.src)
        self.assertIsNotNone(holder)
        try:
            code, _, err = self._run(["migrate-data", "--to", str(self.dst)])
            self.assertEqual(code, 1)
            self.assertIn("web server is running", err)
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
