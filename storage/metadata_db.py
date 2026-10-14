"""SQLite ledger tracking the state of every instrument-hour.

This is what makes the downloader resumable: completed and empty hours are
never requested again, and a status can never be downgraded (a failure can
never overwrite a completed hour).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core.models.task import TaskStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hour_status (
    instrument  TEXT NOT NULL,
    hour_utc    TEXT NOT NULL,
    status      TEXT NOT NULL,
    tick_count  INTEGER NOT NULL DEFAULT 0,
    file_path   TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (instrument, hour_utc)
);
CREATE INDEX IF NOT EXISTS idx_hour_status_lookup
    ON hour_status (instrument, status);
CREATE TABLE IF NOT EXISTS mt5_custom_symbol (
    symbol         TEXT PRIMARY KEY,
    source_symbol  TEXT NOT NULL DEFAULT '',
    ticks          INTEGER NOT NULL DEFAULT 0,
    first_ms       INTEGER NOT NULL DEFAULT 0,
    last_ms        INTEGER NOT NULL DEFAULT 0,
    range_label    TEXT,
    imported_at    TEXT,
    synced_at      TEXT NOT NULL
);
"""

# Completed hours are never overwritten with a worse outcome.
_COMPLETED = TaskStatus.COMPLETED.value

# Cross-process write contention (e.g. CLI + web server on the same data dir)
# surfaces as "database is locked". SQLite waits `timeout` before giving up;
# web-facing writes additionally retry a few times on top of that.
_BUSY_TIMEOUT_MS = 30_000
_LOCKED_RETRIES = 3


def _is_locked_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _hour_key(hour: datetime) -> str:
    return hour.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00")


class MetadataDB:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._uncommitted = 0
        self._commit_every = 32

    def _with_locked_retry(self, fn) -> None:
        """Run fn(), retrying through momentary cross-process DB locks.

        A failed write leaves a stale snapshot behind; roll back before
        retrying or every later attempt fails instantly with the same error.
        (fn releases self._lock as its exception propagates, so taking it
        here for the rollback cannot deadlock.)
        """
        for attempt in range(_LOCKED_RETRIES + 1):
            try:
                fn()
                return
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc) or attempt >= _LOCKED_RETRIES:
                    raise
                with self._lock:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                time.sleep(2.0 * (attempt + 1))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def mark(
        self,
        instrument_id: str,
        hour: datetime,
        status: TaskStatus,
        tick_count: int = 0,
        file_path: str | None = None,
        error: str | None = None,
    ) -> None:
        key = _hour_key(hour)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def _do_mark() -> None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT status, attempts FROM hour_status WHERE instrument=? AND hour_utc=?",
                    (instrument_id, key),
                ).fetchone()
                if row is not None and row[0] == _COMPLETED and status != TaskStatus.COMPLETED:
                    return  # never downgrade completed data
                attempts = (row[1] if row else 0) + 1
                self._conn.execute(
                    """
                    INSERT INTO hour_status
                        (instrument, hour_utc, status, tick_count, file_path,
                         attempts, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (instrument, hour_utc) DO UPDATE SET
                        status=excluded.status,
                        tick_count=excluded.tick_count,
                        file_path=excluded.file_path,
                        attempts=excluded.attempts,
                        last_error=excluded.last_error,
                        updated_at=excluded.updated_at
                    """,
                    (instrument_id, key, status.value, tick_count, file_path,
                     attempts, error, now),
                )
                self._uncommitted += 1
                if self._uncommitted >= self._commit_every:
                    self._conn.commit()
                    self._uncommitted = 0

        self._with_locked_retry(_do_mark)

    def flush(self) -> None:
        def _do_flush() -> None:
            with self._lock:
                if self._uncommitted:
                    self._conn.commit()
                    self._uncommitted = 0

        self._with_locked_retry(_do_flush)

    # -- reads -------------------------------------------------------------

    def status_map(
        self, instrument_id: str, start_hour: datetime, end_hour: datetime
    ) -> dict[str, str]:
        """{hour_key: status} for all recorded hours in [start_hour, end_hour]."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT hour_utc, status FROM hour_status
                WHERE instrument=? AND hour_utc BETWEEN ? AND ?
                """,
                (instrument_id, _hour_key(start_hour), _hour_key(end_hour)),
            ).fetchall()
        return dict(rows)

    def summary(self, instrument_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, COUNT(*), COALESCE(SUM(tick_count), 0)
                FROM hour_status WHERE instrument=? GROUP BY status
                """,
                (instrument_id,),
            ).fetchall()
            span = self._conn.execute(
                "SELECT MIN(hour_utc), MAX(hour_utc) FROM hour_status WHERE instrument=?",
                (instrument_id,),
            ).fetchone()
        by_status = {status: {"hours": count, "ticks": ticks} for status, count, ticks in rows}
        return {"by_status": by_status, "first_hour": span[0], "last_hour": span[1]}

    def list_instruments(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT instrument FROM hour_status ORDER BY instrument",
            ).fetchall()
        return [row[0] for row in rows]

    def delete_instrument(self, instrument_id: str) -> int:
        """Remove all ledger rows for an instrument. Returns rows deleted."""
        deleted = 0

        def _do_delete() -> None:
            nonlocal deleted
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM hour_status WHERE instrument=?",
                    (instrument_id,),
                )
                self._conn.commit()
                self._uncommitted = 0
                deleted = cur.rowcount

        self._with_locked_retry(_do_delete)
        return deleted

    def list_completed_hours(
        self,
        instrument_id: str,
        start_hour: datetime,
        end_hour: datetime,
    ) -> list[tuple[datetime, int]]:
        """Completed hours with tick counts, sorted ascending."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT hour_utc, tick_count FROM hour_status
                WHERE instrument=? AND status=? AND tick_count > 0
                  AND hour_utc BETWEEN ? AND ?
                ORDER BY hour_utc
                """,
                (
                    instrument_id,
                    _COMPLETED,
                    _hour_key(start_hour),
                    _hour_key(end_hour),
                ),
            ).fetchall()
        return [
            (
                datetime.fromisoformat(hour_utc).replace(tzinfo=timezone.utc),
                int(tick_count),
            )
            for hour_utc, tick_count in rows
        ]

    def list_completed_sources(
        self,
        instrument_id: str,
        start_hour: datetime,
        end_hour: datetime,
    ) -> list[tuple[str, int]]:
        """Tick file paths and tick counts for completed hours (no filesystem checks)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT hour_utc, tick_count, file_path FROM hour_status
                WHERE instrument=? AND status=? AND tick_count > 0
                  AND hour_utc BETWEEN ? AND ?
                ORDER BY hour_utc
                """,
                (
                    instrument_id,
                    _COMPLETED,
                    _hour_key(start_hour),
                    _hour_key(end_hour),
                ),
            ).fetchall()
        return [
            (
                file_path or "",
                int(tick_count),
                hour_utc,
            )
            for hour_utc, tick_count, file_path in rows
        ]

    def sum_completed_ticks(
        self,
        instrument_id: str,
        start_hour: datetime,
        end_hour: datetime,
    ) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(tick_count), 0) FROM hour_status
                WHERE instrument=? AND status=? AND tick_count > 0
                  AND hour_utc BETWEEN ? AND ?
                """,
                (
                    instrument_id,
                    _COMPLETED,
                    _hour_key(start_hour),
                    _hour_key(end_hour),
                ),
            ).fetchone()
        return int(row[0] if row else 0)

    def replace_file_path(self, old_path: str, new_path: str) -> int:
        """Update ledger rows that reference old_path. Returns rows changed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE hour_status SET file_path=? WHERE file_path=?",
                (new_path, old_path),
            )
            self._uncommitted += 1
        return cur.rowcount

    def rewrite_parquet_paths(self) -> int:
        """Rewrite any remaining .parquet paths in the ledger to .bin."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE hour_status
                SET file_path = REPLACE(file_path, '.parquet', '.bin')
                WHERE file_path LIKE '%.parquet'
                """,
            )
            self._uncommitted += 1
        return cur.rowcount

    def recorded_span(self, instrument_id: str) -> tuple[datetime, datetime] | None:
        """First and last hour recorded in the ledger for this instrument."""
        summary = self.summary(instrument_id)
        if not summary["first_hour"] or not summary["last_hour"]:
            return None
        first = datetime.fromisoformat(summary["first_hour"]).replace(tzinfo=timezone.utc)
        last = datetime.fromisoformat(summary["last_hour"]).replace(tzinfo=timezone.utc)
        return first, last

    # -- MT5 custom symbols (cached; refreshed from MT5 on demand) ------------

    @staticmethod
    def _ms_to_utc_str(ms: int) -> str | None:
        if ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    def _mt5_symbol_row(self, row: tuple) -> dict:
        symbol, source_symbol, ticks, first_ms, last_ms, range_label, imported_at, synced_at = row
        return {
            "symbol": symbol,
            "source_symbol": source_symbol or "",
            "ticks": int(ticks),
            "first_ms": int(first_ms),
            "last_ms": int(last_ms),
            "first_utc": self._ms_to_utc_str(int(first_ms)),
            "last_utc": self._ms_to_utc_str(int(last_ms)),
            "range_label": range_label or "",
            "imported_at": imported_at,
            "synced_at": synced_at,
        }

    def list_mt5_custom_symbols(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, source_symbol, ticks, first_ms, last_ms,
                       range_label, imported_at, synced_at
                FROM mt5_custom_symbol
                ORDER BY symbol
                """,
            ).fetchall()
        return [self._mt5_symbol_row(r) for r in rows]

    @staticmethod
    def _norm_mt5_symbol(entry: dict) -> str:
        return str(entry.get("symbol", "")).strip()

    def _write_mt5_symbol_row(
        self,
        *,
        symbol: str,
        source_symbol: str,
        ticks: int,
        first_ms: int,
        last_ms: int,
        range_label: str | None,
        imported_at: str | None,
        synced_at: str,
    ) -> None:
        """Single-row upsert; caller must hold the lock. Preserves import history."""
        self._conn.execute(
            """
            INSERT INTO mt5_custom_symbol (
                symbol, source_symbol, ticks, first_ms, last_ms,
                range_label, imported_at, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                source_symbol=excluded.source_symbol,
                ticks=excluded.ticks,
                first_ms=excluded.first_ms,
                last_ms=excluded.last_ms,
                range_label=COALESCE(excluded.range_label, mt5_custom_symbol.range_label),
                imported_at=COALESCE(excluded.imported_at, mt5_custom_symbol.imported_at),
                synced_at=excluded.synced_at
            """,
            (
                symbol,
                source_symbol,
                ticks,
                first_ms,
                last_ms,
                range_label,
                imported_at,
                synced_at,
            ),
        )

    def _commit_mt5_write(self) -> None:
        # Custom-symbol writes are rare, explicit user actions: commit at
        # once so the result survives a restart. This differs from the
        # hour-ledger hot path, which batches via _uncommitted/_commit_every;
        # the commit below also persists any batched hour rows, which is
        # harmless (they are complete statements), and resets the counter.
        self._conn.commit()
        self._uncommitted = 0
        # Fold the WAL into the main db file so the write survives even if
        # the -wal/-shm sidecars are lost across a restart (cloud-synced
        # folders, hard kills, file copies). Degrades gracefully under
        # cross-process read locks: PRAGMA reports busy instead of raising.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def upsert_mt5_custom_symbol(
        self,
        *,
        symbol: str,
        source_symbol: str = "",
        ticks: int = 0,
        first_ms: int = 0,
        last_ms: int = 0,
        range_label: str | None = None,
        mark_imported: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def _do_upsert() -> None:
            with self._lock:
                existing = self._conn.execute(
                    "SELECT imported_at FROM mt5_custom_symbol WHERE symbol=?",
                    (symbol,),
                ).fetchone()
                imported_at = now if mark_imported else (existing[0] if existing else None)
                self._write_mt5_symbol_row(
                    symbol=symbol,
                    source_symbol=source_symbol,
                    ticks=ticks,
                    first_ms=first_ms,
                    last_ms=last_ms,
                    range_label=range_label,
                    imported_at=imported_at,
                    synced_at=now,
                )
                self._commit_mt5_write()

        self._with_locked_retry(_do_upsert)

    def delete_mt5_custom_symbol(self, symbol: str) -> None:
        def _do_delete() -> None:
            with self._lock:
                self._conn.execute("DELETE FROM mt5_custom_symbol WHERE symbol=?", (symbol,))
                self._commit_mt5_write()

        self._with_locked_retry(_do_delete)

    def sync_mt5_custom_symbols(
        self,
        entries: list[dict],
        *,
        prune_empty: bool = False,
    ) -> tuple[list[str], bool]:
        """Authoritative sync: upsert fresh entries, delete stale rows.

        Only call after a *successful* live read from MT5. Returns
        ``(removed, pruned)`` where ``pruned`` tells whether the delete
        pass ran. When MT5 reports zero symbols and ``prune_empty`` is
        False, the prune is skipped (cache left untouched) so a wrong
        suffix/filter cannot wipe import history; pass
        ``prune_empty=True`` only after explicit user confirmation.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        removed: list[str] = []
        pruned = False

        def _do_sync() -> None:
            nonlocal removed, pruned
            with self._lock:
                fresh = {
                    self._norm_mt5_symbol(e) for e in entries
                    if self._norm_mt5_symbol(e)
                }
                for entry in entries:
                    symbol = self._norm_mt5_symbol(entry)
                    if not symbol:
                        continue
                    existing = self._conn.execute(
                        "SELECT imported_at FROM mt5_custom_symbol WHERE symbol=?",
                        (symbol,),
                    ).fetchone()
                    self._write_mt5_symbol_row(
                        symbol=symbol,
                        source_symbol=str(entry.get("source_symbol", "")),
                        ticks=int(entry.get("ticks", 0) or 0),
                        first_ms=int(entry.get("first_ms", 0) or 0),
                        last_ms=int(entry.get("last_ms", 0) or 0),
                        range_label=entry.get("range_label"),
                        imported_at=existing[0] if existing else None,
                        synced_at=now,
                    )
                rows = self._conn.execute("SELECT symbol FROM mt5_custom_symbol").fetchall()
                if not fresh and rows and not prune_empty:
                    # Read-only path: nothing was written above, so there is
                    # nothing to commit or checkpoint.
                    removed = []
                    pruned = False
                    return
                stale = [r[0] for r in rows if r[0] not in fresh]
                for symbol in stale:
                    self._conn.execute("DELETE FROM mt5_custom_symbol WHERE symbol=?", (symbol,))
                self._commit_mt5_write()
                removed = sorted(stale)
                pruned = True

        self._with_locked_retry(_do_sync)
        return removed, pruned
