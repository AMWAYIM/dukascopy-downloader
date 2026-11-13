"""Copy → verify → delete migration of a whole data root.

Moves an existing populated data root (ticks, metadata.db, staging, logs,
web settings) to a new location. The source is only removed after every file
has been copied and verified by size, so an interrupted migration leaves both
roots intact and can be re-run.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.data_config import validate_data_dir
from core.models.task import TaskStatus
from storage.fast_delete import fast_remove_tree
from storage.metadata_db import _SCHEMA
from storage.tick_format import read_hour_tick_count

# Transient artifacts never copied to the destination.
_SKIP_NAMES = {".web.lock", ".write_probe"}


def migrate_data_dir(
    src: Path,
    dst: Path,
    *,
    delete_source: bool = False,
    force: bool = False,
    on_progress: "callable[[int, int], None] | None" = None,
    skip_errors: bool = False,
) -> dict:
    """Copy src → dst, verify, and optionally delete src.

    Raises ValueError for invalid arguments and RuntimeError when the
    destination is unusable or verification fails. When ``skip_errors`` is
    true, files that fail to copy (e.g. unreadable sectors on a failing
    drive) are skipped and reported in ``skipped_files`` instead of aborting.
    """
    src = src.resolve()
    dst = dst.resolve()
    if src == dst:
        raise ValueError("source and destination are the same directory")
    if src.is_relative_to(dst) or dst.is_relative_to(src):
        raise ValueError("destination cannot be inside the source (or vice versa)")
    if not src.is_dir():
        raise ValueError(f"source data root not found: {src}")

    validate_data_dir(dst)
    if not force:
        existing = [p for p in dst.iterdir() if p.name not in _SKIP_NAMES]
        if existing:
            raise RuntimeError(
                f"destination is not empty: {dst} (pass --force to copy into it)"
            )

    source_files: list[tuple[Path, Path]] = []
    total_bytes = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] in _SKIP_NAMES:
            continue
        source_files.append((path, dst / rel))
        total_bytes += path.stat().st_size

    copied = 0
    skipped: list[Path] = []
    for path, target in source_files:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError as exc:
            if skip_errors:
                skipped.append(path)
                continue
            raise RuntimeError(f"copy failed for {path}: {exc}") from exc
        copied += 1
        if on_progress is not None:
            on_progress(copied, len(source_files))

    # Verify: every source file exists at the destination with the same size.
    for path, target in source_files:
        if path in skipped:
            continue
        if not target.is_file() or target.stat().st_size != path.stat().st_size:
            raise RuntimeError(f"verification failed for {target}")

    source_deleted = False
    if delete_source:
        fast_remove_tree(src)
        source_deleted = not src.exists()

    return {
        "files_copied": copied,
        "bytes_copied": total_bytes,
        "files_verified": len(source_files) - len(skipped),
        "skipped_files": [str(p) for p in skipped],
        "source_deleted": source_deleted,
    }


# Tables in the metadata ledger that are unioned across data roots. The column
# list must match `storage/metadata_db.py`'s `_SCHEMA` exactly.
_LEDGER_TABLES = ("hour_status", "mt5_custom_symbol")

_LEDGER_HOUR_COLUMNS = (
    "instrument", "hour_utc", "status", "tick_count", "file_path",
    "attempts", "last_error", "updated_at",
)
_LEDGER_MT5_COLUMNS = (
    "symbol", "source_symbol", "ticks", "first_ms", "last_ms",
    "range_label", "imported_at", "synced_at",
)


def _table_columns(
    conn: sqlite3.Connection, table: str, attach: str | None = None
) -> list[tuple]:
    """Full column definitions of `table`, optionally in an attached schema."""
    schema = f"{attach}." if attach else ""
    return conn.execute(f"PRAGMA {schema}table_info({table})").fetchall()


def merge_metadata_ledgers(
    dst_db: Path,
    src_dbs: list[Path],
    *,
    path_rewrites: list[tuple[str, str]] | None = None,
    drop_missing: bool = False,
) -> dict:
    """Union the metadata ledgers of `src_dbs` into `dst_db`.

    The destination database is the base ledger (typically the freshest root).
    Rows are imported with ``INSERT OR IGNORE``, so an existing primary key —
    including a completed hour — is never overwritten or downgraded. After
    importing, ``hour_status.file_path`` values matching any ``(old, new)``
    prefix pair are rewritten in order (each old prefix is replaced once).
    When ``drop_missing`` is true, rows whose (rewritten) ``file_path`` does
    not exist on disk are deleted afterwards, so those hours are re-downloaded
    by a later ``gaps --repair`` run rather than left pointing at nothing.
    The destination is WAL-checkpointed before returning, so the result is a
    self-contained ``metadata.db`` with no sidecar files.

    Returns ``{"hour_rows": int, "mt5_rows": int, "path_rewrites": int,
    "missing_dropped": int}``.

    Raises ValueError when a source is missing, has a schema that differs from
    the destination, or contributes no rows at all.
    """
    if not dst_db.is_file():
        raise ValueError(f"destination database not found: {dst_db}")

    conn = sqlite3.connect(str(dst_db), timeout=30.0)
    try:
        dst_cols = {t: _table_columns(conn, t) for t in _LEDGER_TABLES}
        total_hour = 0
        total_mt5 = 0

        for idx, src in enumerate(src_dbs):
            src = Path(src)
            if not src.is_file():
                raise ValueError(f"source database not found: {src}")
            attach = f"src{idx}"
            conn.execute(f"ATTACH DATABASE ? AS {attach}", (str(src),))
            try:
                for table in _LEDGER_TABLES:
                    col_defs = _table_columns(conn, table, attach=attach)
                    if col_defs != dst_cols[table]:
                        raise ValueError(
                            f"schema mismatch in {src} for {table}: "
                            f"expected {dst_cols[table]}, got {col_defs}"
                        )

                overlap = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT instrument FROM hour_status "
                        f"INTERSECT SELECT DISTINCT instrument FROM {attach}.hour_status"
                    )
                ]
                if overlap:
                    shown = ", ".join(sorted(overlap)[:10])
                    extra = " …" if len(overlap) > 10 else ""
                    print(
                        "warning: overlapping instruments between "
                        f"{dst_db} and {src}: {shown}{extra} "
                        "(destination rows win)",
                        file=sys.stderr,
                    )

                cur = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO hour_status
                        ({", ".join(_LEDGER_HOUR_COLUMNS)})
                    SELECT {", ".join(_LEDGER_HOUR_COLUMNS)}
                    FROM {attach}.hour_status
                    """
                )
                hour_added = cur.rowcount
                cur = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO mt5_custom_symbol
                        ({", ".join(_LEDGER_MT5_COLUMNS)})
                    SELECT {", ".join(_LEDGER_MT5_COLUMNS)}
                    FROM {attach}.mt5_custom_symbol
                    """
                )
                mt5_added = cur.rowcount
                if hour_added == 0 and mt5_added == 0:
                    raise ValueError(f"source contributed no rows: {src}")
                total_hour += hour_added
                total_mt5 += mt5_added
                # Commit before detaching: SQLite refuses to DETACH a
                # database that is in use by an open write transaction.
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                try:
                    conn.execute(f"DETACH DATABASE {attach}")
                except sqlite3.OperationalError:
                    # Non-critical: the connection close cleans up the schema.
                    pass

        rewritten = 0
        for old, new in path_rewrites or []:
            if not old or not new or old == new:
                continue
            pattern = old + "%"
            before = conn.execute(
                "SELECT COUNT(*) FROM hour_status WHERE file_path LIKE ?",
                (pattern,),
            ).fetchone()[0]
            if before == 0:
                continue
            conn.execute(
                "UPDATE hour_status SET file_path = REPLACE(file_path, ?, ?) "
                "WHERE file_path LIKE ?",
                (old, new, pattern),
            )
            after = conn.execute(
                "SELECT COUNT(*) FROM hour_status WHERE file_path LIKE ?",
                (pattern,),
            ).fetchone()[0]
            rewritten += before - after

        dropped = 0
        if drop_missing:
            rows = conn.execute(
                "SELECT instrument, hour_utc, file_path FROM hour_status "
                "WHERE file_path IS NOT NULL AND file_path != ''"
            ).fetchall()
            gone = [
                (inst, key)
                for inst, key, fp in rows
                if not Path(fp).is_file()
            ]
            if gone:
                conn.executemany(
                    "DELETE FROM hour_status WHERE instrument=? AND hour_utc=?",
                    gone,
                )
                dropped = len(gone)

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    return {
        "hour_rows": total_hour,
        "mt5_rows": total_mt5,
        "path_rewrites": rewritten,
        "missing_dropped": dropped,
    }


def normalize_instrument_case(db_path: Path) -> int:
    """Collapse mixed-case instrument rows to uppercase in a ledger.

    Some roots store instrument ids in lowercase in ``hour_status`` while the
    on-disk symbol directories (and the app's instrument catalog) are
    uppercase. A merge can then end up with the same instrument twice (e.g.
    ``deuidxeur`` and ``DEUIDXEUR``). This folds every lowercase row into its
    uppercase counterpart — inserting rows whose ``(instrument, hour_utc)``
    key is not already present (preserving empty/failed null-path records)
    and deleting the lowercase originals. Returns the number of lowercase
    rows deleted.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    deleted = 0
    try:
        lower_instruments = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT instrument FROM hour_status "
                "WHERE instrument != upper(instrument)"
            )
        ]
        for inst in lower_instruments:
            up = inst.upper()
            conn.execute(
                "INSERT OR IGNORE INTO hour_status "
                "(instrument, hour_utc, status, tick_count, file_path, "
                "attempts, last_error, updated_at) "
                "SELECT ?, hour_utc, status, tick_count, file_path, "
                "attempts, last_error, updated_at FROM hour_status "
                "WHERE instrument=?",
                (up, inst),
            )
            cur = conn.execute("DELETE FROM hour_status WHERE instrument=?", (inst,))
            deleted += cur.rowcount
        if lower_instruments:
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return deleted


def rebuild_metadata_ledger(
    tree: Path, out_db: Path, *, skip_instruments: set[str] | None = None
) -> int:
    """Rebuild an ``hour_status`` ledger from a tree of bin_v1 hour files.

    Scans ``<tree>/<SYMBOL>/<year>/<month>/<day>/<hour>.bin`` files, reads
    each file's tick count from its 4-byte header, and writes one completed
    row per file to `out_db` (created fresh with the ledger schema). Returns
    the number of rows written.

    This recovers completed-hour state for a data root whose original
    ``metadata.db`` is unavailable (e.g. already deleted). Empty/failed hour
    states are not reconstructable and are simply absent, so those hours will
    be treated as not-yet-attempted. Pass ``skip_instruments`` (e.g. symbols
    already present in the merge-base ledger) to exclude those instruments
    from the scan.
    """
    skip = frozenset(skip_instruments or ())
    conn = sqlite3.connect(str(out_db), timeout=30.0)
    inserted = 0
    try:
        conn.executescript(_SCHEMA)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for dirpath, _dirnames, filenames in os.walk(tree):
            rel = Path(dirpath).relative_to(tree)
            parts = rel.parts
            if len(parts) != 4:
                continue
            symbol, year, month, day = parts
            if symbol in skip:
                continue
            try:
                y, mo, d = int(year), int(month), int(day)
            except ValueError:
                continue
            if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            for fn in filenames:
                if not fn.endswith(".bin"):
                    continue
                try:
                    h = int(fn[:-4])
                except ValueError:
                    continue
                if not 0 <= h <= 23:
                    continue
                path = Path(dirpath) / fn
                ticks = read_hour_tick_count(path)
                hour_key = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:00:00"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO hour_status "
                    "(instrument, hour_utc, status, tick_count, file_path, "
                    "attempts, last_error, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        symbol, hour_key, TaskStatus.COMPLETED.value, ticks,
                        str(path), 1, None, now,
                    ),
                )
                inserted += cur.rowcount
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return inserted
