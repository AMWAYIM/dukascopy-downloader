"""Unit tests for the shared data-migration service (row #4 core)."""
import tempfile
import unittest
from pathlib import Path

from core.services.data_migration import migrate_data_dir


class MigrateDataDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self.src = self._tmp_path / "src-root"
        sym = self.src / "XAUUSD" / "2026" / "10" / "09"
        sym.mkdir(parents=True)
        (sym / "20.bin").write_bytes(b"gold-ticks")
        (self.src / "metadata.db").write_bytes(b"sqlite-ledger")
        (self.src / ".web.lock").write_bytes(b"\n1234")
        (self.src / ".write_probe").write_bytes(b"")
        self.dst = self._tmp_path / "dst-root"
        self.dst.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _src_files(self):
        return {
            p.relative_to(self.src): p
            for p in self.src.rglob("*")
            if p.is_file() and p.relative_to(self.src).parts[0] not in (".web.lock", ".write_probe")
        }

    def test_copies_verifies_and_deletes_source(self):
        expected = sorted(rel for rel, _ in self._src_files().items())
        stats = migrate_data_dir(self.src, self.dst, delete_source=True)
        self.assertEqual(stats["files_copied"], 2)
        self.assertEqual(stats["files_verified"], 2)
        self.assertTrue(stats["source_deleted"])
        for rel in expected:
            self.assertTrue((self.dst / rel).is_file())
        self.assertFalse((self.dst / ".web.lock").exists())
        self.assertFalse((self.dst / ".write_probe").exists())
        self.assertFalse(self.src.exists())

    def test_keeps_source_by_default(self):
        stats = migrate_data_dir(self.src, self.dst)
        self.assertFalse(stats["source_deleted"])
        self.assertTrue(self.src.exists())
        self.assertTrue((self.dst / "metadata.db").is_file())

    def test_missing_source_raises(self):
        with self.assertRaises(ValueError):
            migrate_data_dir(self._tmp_path / "nope", self.dst)

    def test_same_directory_raises(self):
        with self.assertRaises(ValueError):
            migrate_data_dir(self.src, self.src)

    def test_destination_inside_source_raises(self):
        with self.assertRaises(ValueError):
            migrate_data_dir(self.src, self.src / "inside")

    def test_non_empty_destination_refused_without_force(self):
        (self.dst / "existing.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            migrate_data_dir(self.src, self.dst)

    def test_force_allows_non_empty_destination(self):
        (self.dst / "existing.txt").write_text("x", encoding="utf-8")
        stats = migrate_data_dir(self.src, self.dst, force=True)
        self.assertEqual(stats["files_copied"], 2)
        self.assertTrue((self.dst / "XAUUSD").is_dir())

    def test_verification_failure_raises(self):
        # Break the copy by making a target path unwritable: put a directory
        # where a file should land.
        blocker = self.dst / "XAUUSD" / "2026" / "10" / "09" / "20.bin"
        blocker.mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            migrate_data_dir(self.src, self.dst)


if __name__ == "__main__":
    unittest.main()
