"""Wiring tests for /api/settings/data and /api/migrate-data (rows #5, #7).

Calls the FastAPI view functions directly with stubbed dependencies, following
the convention in test_mt5_refresh_endpoint.py. The migration service itself is
covered by tests/test_data_migration.py.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from web import app as app_module
from web.app import DataMigrationRequest, DataSettingsUpdate
from web.app import patch_data_settings, start_data_migration, get_settings


def _run(coro):
    return asyncio.run(coro)


class DataSettingsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._store = SimpleNamespace(
            get_ui=lambda: {"theme": "dark"},
            get_mt5=lambda: {"portable": False},
            list_automations=lambda: [],
        )
        self._patches = [
            mock.patch.object(app_module, "settings_store", return_value=self._store),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_settings_includes_data_block(self):
        root = self._tmp_path / "data"
        with mock.patch.object(app_module, "effective_data_dir", return_value=(root, "default")), \
             mock.patch.object(app_module, "load_configured_data_dir", return_value=None):
            res = _run(get_settings())
        self.assertEqual(res["data"]["dir"], str(root))
        self.assertEqual(res["data"]["source"], "default")
        self.assertFalse(res["data"]["restart_required"])

    def test_get_settings_marks_restart_required_when_configured_differs(self):
        root = self._tmp_path / "data"
        configured = self._tmp_path / "elsewhere"
        with mock.patch.object(app_module, "effective_data_dir", return_value=(root, "default")), \
             mock.patch.object(app_module, "load_configured_data_dir", return_value=configured):
            res = _run(get_settings())
        self.assertTrue(res["data"]["restart_required"])
        self.assertEqual(res["data"]["configured"], str(configured))

    def test_patch_data_settings_saves_and_reports_restart(self):
        target = self._tmp_path / "new-root"
        saved = {}
        with mock.patch.object(app_module, "validate_data_dir") as mock_validate, \
             mock.patch.object(app_module, "save_configured_data_dir") as mock_save, \
             mock.patch.object(app_module, "effective_data_dir", return_value=(self._tmp_path / "old", "default")), \
             mock.patch.object(app_module, "load_configured_data_dir", return_value=target):
            mock_save.side_effect = lambda p: saved.update(path=Path(p)) or Path(p)
            res = _run(patch_data_settings(DataSettingsUpdate(dir=str(target))))
        mock_validate.assert_called_once_with(target)
        self.assertEqual(saved["path"], target)
        self.assertTrue(res["data"]["restart_required"])

    def test_patch_data_settings_rejects_relative(self):
        from fastapi import HTTPException

        with mock.patch.object(app_module, "validate_data_dir", side_effect=RuntimeError("must be absolute")):
            with self.assertRaises(HTTPException) as ctx:
                _run(patch_data_settings(DataSettingsUpdate(dir="relative/path")))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("absolute", str(ctx.exception.detail))

    def test_patch_data_settings_rejects_blank(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            _run(patch_data_settings(DataSettingsUpdate(dir="  ")))
        self.assertEqual(ctx.exception.status_code, 400)


class DataMigrationEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._cfg = SimpleNamespace(data_dir=self._tmp_path / "current")
        self._cfg.data_dir.mkdir(parents=True)
        self._patch_settings = mock.patch.object(app_module, "settings", return_value=self._cfg)
        self._patch_settings.start()
        self.addCleanup(self._patch_settings.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_data_migration_submits_job(self):
        dst = self._tmp_path / "new-root"
        captured = {}

        def fake_worker(job):
            captured["kind"] = job.kind
            captured["params"] = dict(job.params)
            job.finish({"files_copied": 3, "message": "ok"})

        with mock.patch.object(app_module.jobs, "has_active_data_operation", return_value=False), \
             mock.patch.object(app_module, "run_data_migration_job", side_effect=fake_worker):
            res = _run(start_data_migration(DataMigrationRequest(to=str(dst))))
        self.assertEqual(res["kind"], "data_migration")
        self.assertEqual(captured["kind"], "data_migration")
        self.assertEqual(Path(captured["params"]["to"]), dst)
        self.assertEqual(Path(captured["params"]["src"]), self._cfg.data_dir)
        self.assertFalse(captured["params"]["force"])

    def test_start_data_migration_refuses_current_root(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            _run(start_data_migration(DataMigrationRequest(to=str(self._cfg.data_dir))))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_start_data_migration_refuses_active_jobs(self):
        from fastapi import HTTPException

        with mock.patch.object(app_module.jobs, "has_active_data_operation", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                _run(start_data_migration(DataMigrationRequest(to=str(self._tmp_path / "dst"))))
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
