"""Data-root resolution and fail-fast validation tests.

Covers plan rows #1 (resolution chain env → .dukascopy/data.json → default,
derived db_path) and #2 (validate_data_dir fail-fast).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import data_config
from config.data_config import (
    DATA_DIR_ENV_VAR,
    clear_configured_data_dir,
    effective_data_dir,
    load_configured_data_dir,
    save_configured_data_dir,
    validate_data_dir,
)
from config.settings import BASE_DIR, Settings


class DataDirResolutionTests(unittest.TestCase):
    """Resolution order: env > file > default; derived db_path."""

    def setUp(self):
        # Start with the env var unset (empty string is treated as unset).
        self._env_patch = mock.patch.dict(os.environ, {DATA_DIR_ENV_VAR: ""})
        self._env_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._cfg = self._tmp_path / "data.json"
        # Redirect the persistent config file into a temp path so tests never
        # touch the real <repo>/.dukascopy/data.json.
        self._cfg_patch = mock.patch.object(data_config, "config_file", return_value=self._cfg)
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()
        self._tmp.cleanup()
        self._env_patch.stop()

    def test_default_root_when_nothing_configured(self):
        settings = Settings()
        self.assertEqual(settings.data_dir, BASE_DIR / "data")
        self.assertEqual(settings.db_path, BASE_DIR / "data" / "metadata.db")
        settings.ensure_directories()
        self.assertTrue((BASE_DIR / "data").is_dir())

    def test_env_overrides_config_file(self):
        save_configured_data_dir(self._tmp_path / "from-file")
        with mock.patch.dict(
            os.environ, {DATA_DIR_ENV_VAR: str(self._tmp_path / "from-env")}
        ):
            settings = Settings()
        self.assertEqual(settings.data_dir, self._tmp_path / "from-env")
        self.assertEqual(settings.db_path, self._tmp_path / "from-env" / "metadata.db")

    def test_config_file_overrides_default(self):
        save_configured_data_dir(self._tmp_path / "from-file")
        settings = Settings()
        self.assertEqual(settings.data_dir, self._tmp_path / "from-file")
        self.assertEqual(settings.db_path, self._tmp_path / "from-file" / "metadata.db")

    def test_relative_config_file_ignored(self):
        self._cfg.write_text(json.dumps({"data_dir": "relative/path"}), encoding="utf-8")
        self.assertIsNone(load_configured_data_dir())
        source, _ = effective_data_dir()
        self.assertEqual(source, BASE_DIR / "data")

    def test_non_string_config_file_ignored(self):
        self._cfg.write_text(json.dumps({"data_dir": 42}), encoding="utf-8")
        self.assertIsNone(load_configured_data_dir())

    def test_malformed_config_file_ignored(self):
        self._cfg.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(load_configured_data_dir())

    def test_relative_save_rejected(self):
        with self.assertRaises(ValueError):
            save_configured_data_dir("relative/path")

    def test_save_roundtrip_and_clear(self):
        target = self._tmp_path / "round"
        self.assertEqual(save_configured_data_dir(target), target)
        self.assertEqual(load_configured_data_dir(), target)
        clear_configured_data_dir()
        self.assertIsNone(load_configured_data_dir())

    def test_source_labels(self):
        self.assertEqual(effective_data_dir()[1], "default")
        save_configured_data_dir(self._tmp_path / "file-root")
        self.assertEqual(effective_data_dir()[1], "file")
        with mock.patch.dict(
            os.environ, {DATA_DIR_ENV_VAR: str(self._tmp_path / "env-root")}
        ):
            self.assertEqual(effective_data_dir()[1], "env")


class DataRootValidationTests(unittest.TestCase):
    """Fail-fast behavior of validate_data_dir / ensure_directories."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_root_when_missing(self):
        root = self._tmp_path / "new-root"
        validate_data_dir(root)
        self.assertTrue(root.is_dir())

    def test_fail_fast_when_root_is_a_file(self):
        blocker = self._tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            validate_data_dir(blocker)

    def test_fail_fast_when_relative(self):
        with self.assertRaises(RuntimeError):
            validate_data_dir(Path("relative"))

    def test_ensure_directories_raises_on_bad_root(self):
        blocker = self._tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            Settings(data_dir=blocker).ensure_directories()


if __name__ == "__main__":
    unittest.main()
