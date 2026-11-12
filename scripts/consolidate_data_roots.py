#!/usr/bin/env python3
"""Consolidate multiple Dukascopy data roots into one target root.

Primary use case: move an older default-root dataset (repo `data/`) plus the
currently configured root (e.g. `H:/.../Dukascopy`) into a single new root,
merging their SQLite ledgers and rewriting stored absolute tick paths so the
app keeps working from the new location.

    python scripts/consolidate_data_roots.py \
        --target C:/.../TickData/Dukascopy \
        --primary H:/.../Dukascopy \
        --secondary C:/.../dukascopy-downloader/data

Semantics:

- The **primary** source is copied wholesale (ticks, `metadata.db`, staging,
  web settings) using the same copy -> verify-by-size logic as
  `python main.py migrate-data`. Its `metadata.db` becomes the merge base.
- Each **secondary** source's tree is copied into the target, skipping its own
  `metadata.db` (and WAL sidecars/locks); directories deep-merge, existing
  files are overwritten with `shutil.copy2`.
- The secondary ledgers are unioned into the target `metadata.db` with
  `INSERT OR IGNORE` (never overwrites/downgrades existing rows), then every
  stored tick path under the old roots is rewritten to the target root.
- Originals are never deleted. Re-runnable: pass `--force` to copy into a
  partially-filled target (e.g. after an interrupted run).

Stop the web server before running.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.data_config import validate_data_dir
from core.services.data_migration import merge_metadata_ledgers, migrate_data_dir

# Top-level names never copied from a secondary source (its ledger is merged,
# not copied).
_SECONDARY_SKIP_TOP = {
    ".web.lock",
    ".write_probe",
    "metadata.db",
    "metadata.db-wal",
    "metadata.db-shm",
}
_PRIMARY_SKIP_TOP = {".web.lock", ".write_probe"}


def _checkpoint_db(db: Path) -> None:
    """Fold WAL sidecars into the main file so copies are self-contained."""
    if not db.is_file():
        return
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()


def _iter_files(root: Path, skip_top: set[str]):
    """Yield ``(abs_path, relative_str)`` for every file under `root`.

    Depth-first via ``os.scandir`` (much faster than ``pathlib.rglob`` on
    roots with hundreds of thousands of files). Files whose top-level path
    component is in `skip_top` are omitted.
    """
    root_str = str(root)
    stack = [root_str]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    rel = os.path.relpath(e.path, root_str)
                    first = rel.split(os.sep, 1)[0]
                    if first in skip_top:
                        continue
                    yield e.path, rel
            except OSError:
                continue


def _count_files(root: Path, skip_top: set[str]) -> tuple[int, int]:
    files = 0
    total = 0
    for path, _rel in _iter_files(root, skip_top):
        files += 1
        total += os.path.getsize(path)
    return files, total


def _copy_tree_verified(src: Path, dst: Path, skip_top: set[str]) -> dict:
    pairs: list[tuple[str, str]] = []
    total_bytes = 0
    for path, rel in _iter_files(src, skip_top):
        pairs.append((path, rel))
        total_bytes += os.path.getsize(path)
    for path, rel in pairs:
        target = Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path, rel in pairs:
        target = Path(dst) / rel
        if not target.is_file() or target.stat().st_size != os.path.getsize(path):
            raise RuntimeError(f"verification failed for {target}")
    return {"files_copied": len(pairs), "bytes_copied": total_bytes}


def _validate_disjoint(target: Path, roots: list[Path]) -> None:
    for r in roots:
        if r == target:
            raise ValueError(f"root equals target: {r}")
        if r.is_relative_to(target) or target.is_relative_to(r):
            raise ValueError(f"root cannot contain the target (or vice versa): {r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="destination data root")
    parser.add_argument("--primary", type=Path, required=True, help="primary source (copied wholesale)")
    parser.add_argument("--secondary", type=Path, action="append", default=[],
                        help="secondary source (tree-copied, ledger merged)")
    parser.add_argument("--force", action="store_true",
                        help="allow copying into a non-empty target (resume)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report planned counts without writing anything")
    args = parser.parse_args(argv)

    target = args.target.resolve()
    primary = args.primary.resolve()
    secondaries = [p.resolve() for p in args.secondary]
    _validate_disjoint(target, [primary, *secondaries])
    for r in [primary, *secondaries]:
        if not r.is_dir():
            parser.error(f"source root not found: {r}")

    if args.dry_run:
        p_files, p_bytes = _count_files(primary, _PRIMARY_SKIP_TOP)
        s_files = s_bytes = 0
        for sec in secondaries:
            f, b = _count_files(sec, _SECONDARY_SKIP_TOP)
            s_files += f
            s_bytes += b
        print("DRY RUN — no changes written")
        print(f"target     : {target}")
        print(f"primary    : {primary} ({p_files:,} files, {p_bytes:,} bytes)")
        for sec in secondaries:
            f, b = _count_files(sec, _SECONDARY_SKIP_TOP)
            print(f"secondary  : {sec} ({f:,} files, {b:,} bytes)")
        print(f"estimated  : {p_files + s_files:,} files, {p_bytes + s_bytes:,} bytes in {target}")
        print("merge base : " + str(target / "metadata.db") + " (from primary)")
        print("rewrites   : " + "; ".join(f"{old} -> {target}" for old in [primary, *secondaries]))
        return 0

    validate_data_dir(target)
    print(f"target  : {target}")
    print(f"primary : {primary}")

    # 1. Checkpoint source DBs (fold WAL into the main file).
    for r in [primary, *secondaries]:
        _checkpoint_db(r / "metadata.db")

    # 2. Copy the primary wholesale (includes its metadata.db = merge base).
    p_stats = migrate_data_dir(primary, target, delete_source=False, force=args.force)
    print(f"primary copied : {p_stats['files_copied']:,} files "
          f"({p_stats['bytes_copied']:,} bytes), verified "
          f"{p_stats['files_verified']:,}")

    # 3. Copy secondaries, skipping their ledger/sidecar/artifact files.
    for sec in secondaries:
        s_stats = _copy_tree_verified(sec, target, _SECONDARY_SKIP_TOP)
        print(f"secondary copied: {sec} ({s_stats['files_copied']:,} files, "
              f"{s_stats['bytes_copied']:,} bytes), verified")

    # 4. Drop any stale SQLite sidecars that came along with the primary copy.
    for side in ("metadata.db-wal", "metadata.db-shm"):
        (target / side).unlink(missing_ok=True)

    # 5. Union the secondary ledgers into the target and rewrite stored tick
    #    paths so exports/MT5 publish resolve inside the new root.
    src_dbs = [sec / "metadata.db" for sec in secondaries if (sec / "metadata.db").is_file()]
    rewrites = [(str(primary), str(target))] + [
        (str(sec), str(target)) for sec in secondaries
    ]
    merged = merge_metadata_ledgers(target / "metadata.db", src_dbs, path_rewrites=rewrites)
    print(f"merged ledger : {merged['hour_rows']:,} hour rows, "
          f"{merged['mt5_rows']:,} mt5 symbol rows imported; "
          f"{merged['path_rewrites']:,} file paths rewritten")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
