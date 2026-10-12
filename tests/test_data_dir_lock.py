"""Lock probe tests: missing / free / held data-directory lock."""
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from cli.commands import _acquire_data_dir_lock, is_data_dir_locked


def _hold_lock_in_child(data_dir: str, ready: "multiprocessing.Queue", done: "multiprocessing.Queue") -> None:
    holder = _acquire_data_dir_lock(Path(data_dir))
    if holder is None:
        ready.put(False)
        return
    ready.put(True)
    done.get()
    holder.close()


class DataDirLockProbeTests(unittest.TestCase):
    def test_missing_lock_file_is_not_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_data_dir_locked(Path(tmp)))

    def test_free_lock_file_is_not_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".web.lock").write_bytes(b"\n1234")
            self.assertFalse(is_data_dir_locked(Path(tmp)))

    def test_held_lock_reports_locked_and_release_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = _acquire_data_dir_lock(Path(tmp))
            self.assertIsNotNone(holder)
            try:
                self.assertTrue(is_data_dir_locked(Path(tmp)))
            finally:
                holder.close()
            self.assertFalse(is_data_dir_locked(Path(tmp)))

    def test_held_lock_in_child_process_reports_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            ready = ctx.Queue()
            done = ctx.Queue()
            proc = ctx.Process(target=_hold_lock_in_child, args=(tmp, ready, done))
            proc.start()
            try:
                self.assertTrue(ready.get(timeout=15), "child failed to acquire lock")
                self.assertTrue(is_data_dir_locked(Path(tmp)))
                try:
                    lock_bytes = (Path(tmp) / ".web.lock").read_bytes()
                except PermissionError:
                    # Expected on Windows: the holder's mandatory lock can
                    # refuse reads outright — itself proof of contention.
                    lock_bytes = None
            finally:
                done.put(True)
                proc.join(timeout=15)
            self.assertFalse(proc.is_alive(), "lock-holder child did not exit")
            self.assertEqual(proc.exitcode, 0)
            # Holder content (leading newline + PID) is preserved, not truncated.
            if lock_bytes is not None:
                self.assertTrue(lock_bytes.startswith(b"\n"))
            self.assertFalse(is_data_dir_locked(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
