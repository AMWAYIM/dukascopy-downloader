"""Prepare tick jobs and launch MetaTrader 5 to import custom symbols."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config.settings import BASE_DIR, Settings
from core.exceptions import JobCancelled
from core.models.instrument import Instrument
from core.services.planner import Planner
from export.mt5_tick_publisher import HOURS_MANIFEST, MT5TickPublisher
from storage.metadata_db import MetadataDB
from storage.tick_format import count_ticks_in_files
from storage.tick_storage import TickStorage

MT5_BUNDLE_DIR = BASE_DIR / "mt5"
SCRIPT_BASE_NAME = "DukascopyTickImport"
SCRIPT_MQ5_NAME = f"{SCRIPT_BASE_NAME}.mq5"
SCRIPT_EX5_NAME = f"{SCRIPT_BASE_NAME}.ex5"
M30_EA_BASE_NAME = "M30CacheWarmer"
M30_EA_MQ5_NAME = f"{M30_EA_BASE_NAME}.mq5"
M30_EA_EX5_NAME = f"{M30_EA_BASE_NAME}.ex5"
M30_CACHE_SUBFOLDER = "M30Cache"
M30_WARMUP_TIMEOUT_SEC = 5 * 60
LEGACY_IMPORT_NAMES = (
    "DukascopyImport.mq5",
    "DukascopyImport.ex5",
)
LEGACY_MANAGER_NAMES = (
    "DukascopySymbolManager.mq5",
    "DukascopySymbolManager.ex5",
)
JOB_ROOT = "dukascopy_jobs"

_import_lock = threading.Lock()
_active_terminal: dict[str, Path] = {}
IMPORT_TIMEOUT_SEC = 6 * 3600
MT5_STARTUP_TIMEOUT_SEC = 120
# MT5 can intermittently fail to open a file after a very large Common\Files
# job folder.  Keep each script run below that practical limit and append the
# batches into the same custom symbol.
MAX_HOUR_FILES_PER_IMPORT = 10_000


@dataclass
class Mt5Settings:
    terminal_exe: Path
    data_path: Path
    custom_suffix: str = ".DUK"
    portable: bool = False

    @classmethod
    def validate_config(cls, raw: dict[str, Any]) -> None:
        """Cheap config check without environment probing (no I/O auto-detect).

        Only validates explicitly configured paths; blank terminal/data_path
        is allowed and resolved later by :meth:`from_dict` at job runtime.
        """
        terminal = str(raw.get("terminal_exe") or "").strip()
        if terminal and not Path(terminal).is_file():
            raise ValueError(f"MetaTrader 5 not found: {Path(terminal)}")
        data_path_raw = str(raw.get("data_path") or "").strip()
        if data_path_raw and not Path(data_path_raw).is_dir():
            raise ValueError(f"MT5 data folder not found: {Path(data_path_raw)}")
        suffix = str(raw.get("custom_suffix") or ".DUK").strip() or ".DUK"
        if not suffix.startswith("."):
            suffix = "." + suffix
        if len(suffix) > 30:
            raise ValueError(f"Custom suffix '{suffix}' is too long")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Mt5Settings:
        terminal = str(raw.get("terminal_exe") or "").strip()
        if terminal:
            terminal_exe = Path(terminal)
            if not terminal_exe.is_file():
                raise ValueError(f"MetaTrader 5 not found: {terminal_exe}")
        else:
            terminal_exe, _source = resolve_mt5_terminal_exe()

        portable = _parse_portable(raw.get("portable"))

        data_path_raw = str(raw.get("data_path") or "").strip()
        if data_path_raw:
            data_path = Path(data_path_raw)
        elif portable:
            data_path = terminal_exe.parent
        else:
            data_path = resolve_mt5_data_path(terminal_exe)
        if not data_path.is_dir():
            raise ValueError(f"MT5 data folder not found: {data_path}")

        suffix = str(raw.get("custom_suffix") or ".DUK").strip() or ".DUK"
        if not suffix.startswith("."):
            suffix = "." + suffix
        return cls(
            terminal_exe=terminal_exe,
            data_path=data_path,
            custom_suffix=suffix,
            portable=portable,
        )


def _parse_portable(value: Any) -> bool:
    """Coerce the portable flag (default False = roaming)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "on", "portable")


MT5_DEFAULT_INSTALL_PATHS = (
    Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    Path(r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"),
)


def _read_origin_id(origin: Path) -> str:
    """Read MT5's origin.txt (UTF-16LE as written by the terminal, or UTF-8)."""
    raw = origin.read_bytes()
    if raw[:2] == b"\xff\xfe":
        return raw.decode("utf-16", errors="ignore").strip().lstrip("\ufeff").strip()
    if b"\x00" in raw:
        text = raw.decode("utf-16-le", errors="ignore").strip()
        if text:
            return text
    return raw.decode("utf-8", errors="ignore").strip().lstrip("\ufeff").strip()


def _terminal_roots() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    return root if root.is_dir() else None


def resolve_mt5_terminal_exe() -> tuple[Path, str]:
    """Best-effort terminal64.exe when none is configured.

    Order: single running process → origin.txt install dirs (most-recent
    data folder wins) → default Program Files locations.
    Returns (exe, source-label). Raises ValueError when ambiguous/missing.
    """
    running = [
        Path(p["path"])
        for p in list_terminal_processes()
        if p.get("path") and Path(p["path"]).is_file()
    ]
    unique_running = sorted(set(running))
    if len(unique_running) == 1:
        return unique_running[0], "auto:running-process"
    if len(unique_running) > 1:
        options = "\n".join(f"  - {p}" for p in unique_running)
        raise ValueError(
            "Multiple MT5 terminals are running — set terminal64.exe explicitly:\n"
            + options,
        )

    root = _terminal_roots()
    if root is not None:
        installs: list[tuple[Path, Path]] = []
        for child in root.iterdir():
            if not child.is_dir() or child.name in ("Common", "Community", "Help"):
                continue
            origin = child / "origin.txt"
            if not origin.is_file():
                continue
            install_dir = Path(_read_origin_id(origin))
            exe = install_dir / "terminal64.exe"
            if exe.is_file():
                installs.append((exe, child))
        unique_installs = sorted(set(exe for exe, _ in installs))
        if len(unique_installs) == 1:
            return unique_installs[0], "auto:origin.txt"
        if unique_installs:
            by_mtime = sorted(
                installs, key=lambda item: item[1].stat().st_mtime, reverse=True,
            )
            picked = by_mtime[0][0]
            candidates = "\n".join(f"  - {exe} (data: {data})" for exe, data in by_mtime)
            import logging
            logging.getLogger(__name__).warning(
                "Multiple MT5 installs found; auto-selected most-recent %s.\n%s",
                picked, candidates,
            )
            return picked, "auto:origin.txt (most-recent)"

    for candidate in MT5_DEFAULT_INSTALL_PATHS:
        if candidate.is_file():
            return candidate, "auto:default-install-path"

    raise ValueError(
        "MetaTrader 5 path is not configured and no terminal64.exe was found — "
        "set it in Settings (or start MT5 once and retry)",
    )


def resolve_mt5_data_path(terminal_exe: Path) -> Path:
    """Best-effort MT5 data directory (Roaming\\MetaQuotes\\Terminal\\<id>)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("APPDATA is not set — configure MT5 data path manually in Settings")
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    if not root.is_dir():
        raise ValueError(f"MetaQuotes Terminal folder not found: {root}")

    origin = terminal_exe.parent / "origin.txt"
    if origin.is_file():
        instance_id = _read_origin_id(origin)
        candidate = root / instance_id
        if candidate.is_dir():
            return candidate

    candidates = [
        p for p in root.iterdir()
        if p.is_dir() and p.name not in ("Common", "Community", "Help")
    ]
    if not candidates:
        raise ValueError("No MT5 terminal data folders found — configure data path in Settings")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def common_files_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("APPDATA is not set")
    return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files"


def portable_files_dir(data_path: Path) -> Path:
    """Files dir for a portable terminal (data lives next to the exe)."""
    return Path(data_path) / "MQL5" / "Files"


def files_base_dir(mt5: Mt5Settings) -> Path:
    """Primary job/cache base for this terminal mode."""
    if mt5.portable:
        return portable_files_dir(mt5.data_path)
    return common_files_dir()


def job_dir_for(mt5: Mt5Settings, job_id: str) -> Path:
    return files_base_dir(mt5) / JOB_ROOT / job_id


def candidate_job_dirs(mt5: Mt5Settings | None, job_id: str) -> list[Path]:
    """Primary + fallback job dirs (MQL5 writes both, Python polls both)."""
    dirs: list[Path] = []
    if mt5 is not None:
        try:
            primary = job_dir_for(mt5, job_id)
        except ValueError:
            return dirs
        dirs.append(primary)
        try:
            fallback = (
                common_files_dir() / JOB_ROOT / job_id
                if mt5.portable
                else portable_files_dir(mt5.data_path) / JOB_ROOT / job_id
            )
        except ValueError:
            return dirs
        if fallback != dirs[0]:
            dirs.append(fallback)
        return dirs
    try:
        dirs.append(common_files_dir() / JOB_ROOT / job_id)
    except ValueError:
        pass
    return dirs


def _remove_job_dirs(mt5: Mt5Settings | None, job_id: str, *, prefix_sweep: bool = False) -> None:
    targets = candidate_job_dirs(mt5, job_id)
    if prefix_sweep:
        bases: list[Path] = []
        if mt5 is not None:
            try:
                bases = [files_base_dir(mt5)]
            except ValueError:
                bases = []
            if bases:
                try:
                    other = common_files_dir() if mt5.portable else portable_files_dir(mt5.data_path)
                except ValueError:
                    other = None
                if other is not None and other != bases[0]:
                    bases.append(other)
        else:
            try:
                bases = [common_files_dir()]
            except ValueError:
                bases = []
        for base in bases:
            root = base / JOB_ROOT
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and child.name.startswith(job_id):
                    shutil.rmtree(child, ignore_errors=True)
    for job_dir in targets:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


def parse_progress_file_any(paths: list[Path]) -> dict[str, Any]:
    """Parse the first non-empty progress.txt across candidate locations."""
    for path in paths:
        data = parse_progress_file(path)
        if data:
            return data
    return {}


def custom_symbol_name(source_symbol: str, suffix: str) -> str:
    name = f"{source_symbol}{suffix}"
    if len(name) > 31:
        raise ValueError(
            f"Custom symbol name '{name}' exceeds MT5 limit of 31 characters — shorten the suffix",
        )
    return name


def parse_progress_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in ("ticks_imported", "ticks_total", "percent", "error_code", "files_done", "files_total"):
            try:
                data[key] = int(value)
            except ValueError:
                data[key] = 0
        else:
            data[key] = value
    return data


def write_manifest(path: Path, fields: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in fields.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_script_set(path: Path, job_id: str) -> None:
    path.write_text(f"; generated by dukascopy-downloader\nJobId={job_id}\n", encoding="utf-8")


def write_startup_ini(
    path: Path,
    *,
    symbol: str,
) -> None:
    content = f"""; generated by dukascopy-downloader
[Experts]
AllowLiveTrading=0
Enabled=1

[StartUp]
Symbol={symbol}
Period=M1
Script=dukascopy\\DukascopyTickImport
ScriptParameters=dukascopy_import.set
ShutdownTerminal=0
"""
    path.write_text(content, encoding="utf-8")


def write_expert_startup_ini(
    path: Path,
    *,
    symbol: str,
    expert: str,
    period: str,
    preset: str,
) -> None:
    content = f"""; generated by dukascopy-downloader
[Experts]
AllowLiveTrading=0
Enabled=1

[StartUp]
Symbol={symbol}
Period={period}
Expert={expert}
ExpertParameters={preset}
ShutdownTerminal=0
"""
    path.write_text(content, encoding="utf-8")


def write_m30_expert_set(path: Path, job_id: str) -> None:
    path.write_text(
        "; generated by dukascopy-downloader\n"
        f"InpJobId={job_id}\n"
        "InpForceRebuild=true\n",
        encoding="utf-8",
    )


def m30_cache_file_path(
    symbol: str,
    subfolder: str = M30_CACHE_SUBFOLDER,
    mt5: Mt5Settings | None = None,
) -> Path:
    # Keep '.' (e.g. EURUSD.DUK); only strip path-hostile chars — matches M30CacheWarmer.mq5
    safe = symbol.replace(":", "_").replace("\\", "_").replace("/", "_")
    name = f"M30_{safe}.bin"
    if mt5 is not None:
        return files_base_dir(mt5) / subfolder / name
    return common_files_dir() / subfolder / name


def m30_cache_candidates(symbol: str, mt5: Mt5Settings | None = None) -> list[Path]:
    primary = m30_cache_file_path(symbol, mt5=mt5)
    candidates = [primary]
    if mt5 is not None:
        other_base = common_files_dir() if mt5.portable else portable_files_dir(mt5.data_path)
        other = other_base / M30_CACHE_SUBFOLDER / primary.name
        if other != primary:
            candidates.append(other)
    return candidates


def _compile_mq5(
    mt5: Mt5Settings,
    dst_mq5: Path,
    dst_ex5: Path,
    hash_stamp: Path,
    src_hash: str,
) -> Path:
    if dst_ex5.is_file():
        dst_ex5.unlink(missing_ok=True)

    metaeditor = mt5.terminal_exe.parent / "metaeditor64.exe"
    log_tail = ""
    if metaeditor.is_file():
        log_path = dst_mq5.parent / "compile.log"
        subprocess.run(
            [str(metaeditor), f"/compile:{dst_mq5}", f"/log:{log_path}"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if dst_ex5.is_file():
            hash_stamp.write_text(src_hash + "\n", encoding="utf-8")
            return dst_ex5
        if log_path.is_file():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    if metaeditor.is_file():
        raise RuntimeError(
            f"Failed to compile {dst_mq5.name}. Log: {log_tail or 'no details'}",
        )

    raise FileNotFoundError(
        f"{dst_ex5.name} is missing and MetaEditor was not found.",
    )


def ensure_m30_cache_ea_installed(
    mt5: Mt5Settings,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    if should_cancel and should_cancel():
        raise JobCancelled()

    experts_dir = mt5.data_path / "MQL5" / "Experts" / "dukascopy"
    experts_dir.mkdir(parents=True, exist_ok=True)

    src_mq5 = MT5_BUNDLE_DIR / M30_EA_MQ5_NAME
    if not src_mq5.is_file():
        raise FileNotFoundError(f"Missing M30 cache EA source: {src_mq5}")

    dst_mq5 = experts_dir / M30_EA_MQ5_NAME
    dst_ex5 = experts_dir / M30_EA_EX5_NAME
    hash_stamp = experts_dir / f".{M30_EA_BASE_NAME}.mq5.sha256"
    src_hash = _sha256_file(src_mq5)
    installed_hash = hash_stamp.read_text(encoding="utf-8").strip() if hash_stamp.is_file() else ""

    if installed_hash != src_hash or not dst_mq5.is_file():
        shutil.copy2(src_mq5, dst_mq5)

    needs_compile = (
        not dst_ex5.is_file()
        or installed_hash != src_hash
        or dst_mq5.stat().st_mtime > dst_ex5.stat().st_mtime
    )
    if not needs_compile:
        return dst_ex5

    return _compile_mq5(mt5, dst_mq5, dst_ex5, hash_stamp, src_hash)


def _run_m30_cache_warmup(
    mt5: Mt5Settings,
    *,
    custom_symbol: str,
    job_id: str,
    progress_path: Path,
    staging_dir: Path,
    ticks_imported: int,
    ticks_total: int,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    if should_cancel and should_cancel():
        raise JobCancelled()

    if on_progress:
        on_progress({
            "message": "Warming M30 cache…",
            "percent": 92,
            "phase": "warm_m30",
            "ticks_imported": ticks_imported,
            "ticks_total": ticks_total,
            "custom_symbol": custom_symbol,
        })

    kill_mt5_terminal(mt5.terminal_exe)
    wait_mt5_exit(mt5.terminal_exe)
    ensure_m30_cache_ea_installed(mt5, should_cancel=should_cancel)

    preset_path = mt5.data_path / "MQL5" / "presets" / "dukascopy_m30.set"
    preset_path.parent.mkdir(parents=True, exist_ok=True)
    write_m30_expert_set(preset_path, job_id)

    ini_path = staging_dir / "m30_warmup.ini"
    write_expert_startup_ini(
        ini_path,
        symbol=custom_symbol,
        expert=r"dukascopy\M30CacheWarmer",
        period="M30",
        preset="dukascopy_m30.set",
    )

    cache_candidates = m30_cache_candidates(custom_symbol, mt5)
    launch_mt5_background(mt5.terminal_exe, ini_path, portable=mt5.portable)

    started = time.monotonic()
    while time.monotonic() - started < M30_WARMUP_TIMEOUT_SEC:
        if should_cancel and should_cancel():
            kill_mt5_terminal(mt5.terminal_exe)
            raise JobCancelled()

        progress = parse_progress_file_any([
            progress_path,
            *[d / "progress.txt" for d in candidate_job_dirs(mt5, job_id)
              if (d / "progress.txt") != progress_path],
        ])
        state = str(progress.get("state", ""))
        phase = str(progress.get("phase", ""))
        if state == "done" and phase == "warm_m30":
            return
        if state == "error":
            msg = progress.get("message") or "M30 cache warmup failed"
            code = progress.get("error_code", "")
            raise RuntimeError(f"{msg} (error {code})".strip())

        if any(p.is_file() and p.stat().st_size > 16 for p in cache_candidates):
            return

        if on_progress:
            on_progress({
                "message": progress.get("message") or "Warming M30 cache…",
                "percent": max(92, int(progress.get("percent", 92))),
                "phase": "warm_m30",
                "ticks_imported": ticks_imported,
                "ticks_total": ticks_total,
                "custom_symbol": custom_symbol,
            })
        time.sleep(0.75)

    raise RuntimeError(
        f"M30 cache warmup timed out after {M30_WARMUP_TIMEOUT_SEC}s for {custom_symbol}",
    )


def purge_import_artifacts(mt5: Mt5Settings) -> None:
    """Remove legacy script builds (not active importer or M30 cache EA)."""
    legacy = LEGACY_IMPORT_NAMES + LEGACY_MANAGER_NAMES
    for subdir in ("Experts", "Scripts"):
        folder = mt5.data_path / "MQL5" / subdir / "dukascopy"
        if not folder.is_dir():
            continue
        if subdir == "Experts":
            continue
        for name in legacy:
            path = folder / name
            if path.is_file():
                path.unlink(missing_ok=True)


SCRIPT_HASH_NAME = f".{SCRIPT_BASE_NAME}.mq5.sha256"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_script_installed(mt5: Mt5Settings, should_cancel: Callable[[], bool] | None = None) -> Path:
    """Copy MQ5 to Scripts and compile to EX5 (never reuse stale EA builds)."""
    if should_cancel and should_cancel():
        raise JobCancelled()

    purge_import_artifacts(mt5)

    scripts_dir = mt5.data_path / "MQL5" / "Scripts" / "dukascopy"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    src_mq5 = MT5_BUNDLE_DIR / SCRIPT_MQ5_NAME
    if not src_mq5.is_file():
        raise FileNotFoundError(f"Missing import script source: {src_mq5}")

    dst_mq5 = scripts_dir / SCRIPT_MQ5_NAME
    dst_ex5 = scripts_dir / SCRIPT_EX5_NAME
    hash_stamp = scripts_dir / SCRIPT_HASH_NAME
    src_hash = _sha256_file(src_mq5)
    installed_hash = hash_stamp.read_text(encoding="utf-8").strip() if hash_stamp.is_file() else ""

    if installed_hash != src_hash or not dst_mq5.is_file():
        shutil.copy2(src_mq5, dst_mq5)

    needs_compile = (
        not dst_ex5.is_file()
        or installed_hash != src_hash
        or dst_mq5.stat().st_mtime > dst_ex5.stat().st_mtime
    )

    if not needs_compile:
        return dst_ex5

    if dst_ex5.is_file():
        dst_ex5.unlink(missing_ok=True)

    if should_cancel and should_cancel():
        raise JobCancelled()

    metaeditor = mt5.terminal_exe.parent / "metaeditor64.exe"
    log_tail = ""
    if metaeditor.is_file():
        log_path = scripts_dir / "compile.log"
        subprocess.run(
            [str(metaeditor), f"/compile:{dst_mq5}", f"/log:{log_path}"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if dst_ex5.is_file():
            hash_stamp.write_text(src_hash + "\n", encoding="utf-8")
            return dst_ex5
        if log_path.is_file():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    if metaeditor.is_file():
        raise RuntimeError(
            "Failed to compile DukascopyTickImport.mq5. "
            f"Open MetaEditor, compile mt5/{SCRIPT_MQ5_NAME}, save .ex5 to mt5/. "
            f"Log: {log_tail or 'no details'}",
        )

    raise FileNotFoundError(
        f"DukascopyTickImport.ex5 is missing and MetaEditor was not found. "
        f"Compile mt5/{SCRIPT_MQ5_NAME} in MetaEditor once.",
    )


def cleanup_staging(
    mt5: Mt5Settings,
    job_id: str,
    *,
    remove_job_files: bool = True,
    remove_script: bool = False,
) -> None:
    if remove_job_files:
        _remove_job_dirs(mt5, job_id)

    if remove_script:
        purge_import_artifacts(mt5)

    script_set = mt5.data_path / "MQL5" / "presets" / "dukascopy_import.set"
    if script_set.is_file():
        script_set.unlink(missing_ok=True)

    legacy_tester_set = mt5.data_path / "MQL5" / "Profiles" / "Tester" / "dukascopy_import.set"
    if legacy_tester_set.is_file():
        legacy_tester_set.unlink(missing_ok=True)


def kill_mt5_terminal(terminal_exe: Path) -> None:
    """Stop MT5 instances for the configured terminal executable."""
    if os.name != "nt":
        return
    resolved = str(terminal_exe.resolve()).replace("'", "''")
    process_name = terminal_exe.stem.replace("'", "''")
    script = (
        f"$target = '{resolved}'; "
        f"Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -and ($_.Path -ieq $target) } | "
        "Stop-Process -Force -ErrorAction SilentlyContinue"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=10,
        check=False,
    )


def is_mt5_running(terminal_exe: Path) -> bool:
    if os.name != "nt":
        return False
    resolved = str(terminal_exe.resolve()).replace("'", "''")
    process_name = terminal_exe.stem.replace("'", "''")
    script = (
        f"$target = '{resolved}'; "
        f"$p = Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -and ($_.Path -ieq $target) }; "
        "if ($p) { exit 0 } else { exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def wait_mt5_exit(terminal_exe: Path, timeout_sec: float = 30.0) -> None:
    """Wait until MT5 has fully exited (needed before /config startup scripts run)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not is_mt5_running(terminal_exe):
            return
        time.sleep(0.25)
    if is_mt5_running(terminal_exe):
        kill_mt5_terminal(terminal_exe)
        time.sleep(2.0)


def build_mt5_launch_args(terminal_exe: Path, ini_path: Path, *, portable: bool = False) -> list[str]:
    args = [str(terminal_exe), f"/config:{ini_path}"]
    if portable:
        args.append("/portable")
    return args


def launch_mt5_background(
    terminal_exe: Path, ini_path: Path, *, portable: bool = False,
) -> None:
    """Launch MT5 minimized and detached (Windows: cmd start /MIN)."""
    exe_dir = str(terminal_exe.parent)
    launch_args = build_mt5_launch_args(terminal_exe, ini_path, portable=portable)
    if os.name == "nt":
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", "/MIN", *launch_args],
            cwd=exe_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
            close_fds=True,
        )
        return
    subprocess.Popen(
        launch_args,
        cwd=exe_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def terminate_process(job_id: str) -> None:
    terminal = _active_terminal.pop(job_id, None)
    if terminal is not None:
        kill_mt5_terminal(terminal)


def abort_mt5_import(
    job_id: str,
    mt5_raw: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Stop MT5 and remove in-flight import artifacts for a job."""
    terminal = _active_terminal.pop(job_id, None)
    if mt5_raw:
        try:
            terminal = terminal or Mt5Settings.from_dict(mt5_raw).terminal_exe
        except ValueError:
            terminal = terminal
    if terminal is not None:
        kill_mt5_terminal(terminal)

    mt5_for_cleanup: Mt5Settings | None = None
    if mt5_raw:
        try:
            mt5_for_cleanup = Mt5Settings.from_dict(mt5_raw)
        except ValueError:
            mt5_for_cleanup = None
    _remove_job_dirs(mt5_for_cleanup, job_id, prefix_sweep=True)

    if mt5_raw:
        try:
            mt5 = Mt5Settings.from_dict(mt5_raw)
            cleanup_staging(mt5, job_id, remove_script=False)
            if settings is not None:
                staging_dir = settings.data_dir / "mt5_staging" / job_id
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
        except ValueError:
            pass


def _import_progress_payload(
    progress: dict[str, Any],
    custom_name: str,
) -> dict[str, Any]:
    imported = int(progress.get("ticks_imported", 0))
    total = int(progress.get("ticks_total", 0))
    files_done = int(progress.get("files_done", 0))
    files_total = int(progress.get("files_total", 0))
    payload: dict[str, Any] = {
        "message": progress.get("message") or "Importing in MT5…",
        "percent": int(progress.get("percent", 0)),
        "phase": progress.get("phase", "import"),
        "ticks_imported": imported,
        "ticks_total": total,
        "custom_symbol": progress.get("custom_symbol", custom_name),
    }
    if files_total > 0:
        payload["files_done"] = files_done
        payload["files_total"] = files_total
    return payload


def _wait_for_mt5_import(
    progress_path: Path,
    *,
    job_id: str,
    terminal_exe: Path,
    custom_name: str,
    mt5: Mt5Settings | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll progress.txt until MT5 reports done or error."""
    _active_terminal[job_id] = terminal_exe
    last_emit = 0.0
    started = time.monotonic()
    startup_deadline = started + MT5_STARTUP_TIMEOUT_SEC
    saw_progress = False
    extra_paths: list[Path] = []
    if mt5 is not None:
        extra_paths = [
            d / "progress.txt" for d in candidate_job_dirs(mt5, job_id)
            if (d / "progress.txt") != progress_path
        ]

    while time.monotonic() - started < IMPORT_TIMEOUT_SEC:
        if should_cancel and should_cancel():
            kill_mt5_terminal(terminal_exe)
            _active_terminal.pop(job_id, None)
            raise JobCancelled()

        final_progress = parse_progress_file(progress_path)
        if not final_progress and extra_paths:
            final_progress = parse_progress_file_any(extra_paths)
        if final_progress:
            saw_progress = True
        elif not saw_progress and time.monotonic() > startup_deadline:
            raise RuntimeError(
                "MT5 did not start the import script within 2 minutes. "
                "Close MetaTrader 5 completely and try again.",
            )

        state = str(final_progress.get("state", ""))
        phase = str(final_progress.get("phase", ""))
        if state in ("done", "ticks_done", "error"):
            return final_progress

        files_done = int(final_progress.get("files_done", 0))
        files_total = int(final_progress.get("files_total", 0))
        now = time.monotonic()
        # A large CustomTicksReplace call can legitimately take longer than
        # a minute without publishing another hour-level update. Never treat
        # a paused progress file as a completed import; wait for MT5's explicit
        # ticks_done/done state (or the overall import timeout).

        if on_progress and now - last_emit >= 0.5:
            last_emit = now
            if final_progress:
                on_progress(_import_progress_payload(final_progress, custom_name))
            else:
                on_progress({
                    "message": "Launching MetaTrader 5…",
                    "percent": 0,
                    "phase": "launch",
                    "custom_symbol": custom_name,
                })
        time.sleep(0.75)

    raise RuntimeError("MT5 import timed out waiting for progress")


def import_ticks(
    settings: Settings,
    mt5_raw: dict[str, Any],
    instrument: Instrument,
    *,
    job_id: str,
    manifest_fields: dict[str, str],
    import_all: bool,
    range_start: date | None,
    range_end: date | None,
    warm_cache: bool = True,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Launch MT5 script import for a prepared job folder."""
    with _import_lock:
        return _import_ticks_locked(
            settings,
            mt5_raw,
            instrument,
            job_id=job_id,
            manifest_fields=manifest_fields,
            import_all=import_all,
            range_start=range_start,
            range_end=range_end,
            warm_cache=warm_cache,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )


def _import_ticks_locked(
    settings: Settings,
    mt5_raw: dict[str, Any],
    instrument: Instrument,
    *,
    job_id: str,
    manifest_fields: dict[str, str],
    import_all: bool,
    range_start: date | None,
    range_end: date | None,
    warm_cache: bool,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    mt5 = Mt5Settings.from_dict(mt5_raw)
    custom_name = custom_symbol_name(instrument.symbol, mt5.custom_suffix)
    origin = str(mt5_raw.get("origin_symbol") or instrument.symbol).strip() or instrument.symbol

    job_dir = job_dir_for(mt5, job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    progress_path = job_dir / "progress.txt"

    staging_dir = settings.data_dir / "mt5_staging" / job_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    ini_path = staging_dir / "import.ini"

    import_ok = False
    try:
        write_manifest(job_dir / "manifest.txt", manifest_fields)

        script_set = mt5.data_path / "MQL5" / "presets" / "dukascopy_import.set"
        script_set.parent.mkdir(parents=True, exist_ok=True)
        write_script_set(script_set, job_id)

        write_startup_ini(
            ini_path,
            symbol=origin,
        )

        if on_progress:
            on_progress({
                "message": "Installing MT5 importer…",
                "percent": 0,
                "phase": "install",
                "custom_symbol": custom_name,
            })

        ensure_script_installed(mt5, should_cancel=should_cancel)

        if should_cancel and should_cancel():
            raise JobCancelled()

        if on_progress:
            on_progress({
                "message": "Launching MetaTrader 5…",
                "percent": 0,
                "phase": "launch",
                "custom_symbol": custom_name,
            })

        launch_mt5_background(mt5.terminal_exe, ini_path, portable=mt5.portable)

        final_progress = _wait_for_mt5_import(
            progress_path,
            job_id=job_id,
            terminal_exe=mt5.terminal_exe,
            custom_name=custom_name,
            mt5=mt5,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )

        state = str(final_progress.get("state", ""))
        if state == "ticks_done":
            imported = int(final_progress.get("ticks_imported", 0))
            total = int(final_progress.get("ticks_total", 0))
            if warm_cache:
                _run_m30_cache_warmup(
                    mt5,
                    custom_symbol=str(final_progress.get("custom_symbol", custom_name)),
                    job_id=job_id,
                    progress_path=progress_path,
                    staging_dir=staging_dir,
                    ticks_imported=imported,
                    ticks_total=total,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            import_ok = True
            return {
                "custom_symbol": final_progress.get("custom_symbol", custom_name),
                "ticks_imported": imported,
                "ticks_total": total,
                "source_symbol": instrument.symbol,
                "import_all": import_all,
            }

        if state == "done":
            imported = int(final_progress.get("ticks_imported", 0))
            total = int(final_progress.get("ticks_total", imported))
            if warm_cache:
                _run_m30_cache_warmup(
                    mt5,
                    custom_symbol=str(final_progress.get("custom_symbol", custom_name)),
                    job_id=job_id,
                    progress_path=progress_path,
                    staging_dir=staging_dir,
                    ticks_imported=imported,
                    ticks_total=total,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            import_ok = True
            return {
                "custom_symbol": final_progress.get("custom_symbol", custom_name),
                "ticks_imported": imported,
                "ticks_total": total,
                "source_symbol": instrument.symbol,
                "import_all": import_all,
            }

        if state == "error":
            msg = final_progress.get("message") or "MT5 import failed"
            code = final_progress.get("error_code", "")
            raise RuntimeError(f"{msg} (error {code})".strip())

        raise RuntimeError(
            final_progress.get("message") or "MT5 import finished without a success status",
        )
    finally:
        _active_terminal.pop(job_id, None)
        # MT5 may retain the last input/progress file after an error. Stop it
        # before cleanup so an incomplete cleanup cannot leave a mixed job.
        if not import_ok:
            kill_mt5_terminal(mt5.terminal_exe)
            wait_mt5_exit(mt5.terminal_exe)
        cleanup_staging(mt5, job_id, remove_job_files=True, remove_script=False)
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def prepare_and_import(
    settings: Settings,
    store: TickStorage,
    planner: Planner,
    instrument: Instrument,
    *,
    job_id: str,
    mt5_raw: dict[str, Any],
    import_all: bool,
    start: date | None,
    end: date | None,
    range_label: str,
    metadata: MetadataDB | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Link hour tick files into the MT5 job folder, then run import."""
    mt5 = Mt5Settings.from_dict(mt5_raw)
    _active_terminal[job_id] = mt5.terminal_exe
    kill_mt5_terminal(mt5.terminal_exe)
    wait_mt5_exit(mt5.terminal_exe)

    staging_dir = settings.data_dir / "mt5_staging" / job_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    publisher = MT5TickPublisher(settings, store, planner, metadata)
    custom_name = custom_symbol_name(instrument.symbol, mt5.custom_suffix)
    origin = str(mt5_raw.get("origin_symbol") or instrument.symbol).strip() or instrument.symbol

    def publish_progress(snapshot: dict[str, Any]) -> None:
        if not on_progress:
            return
        pct = float(snapshot.get("percent", 0))
        on_progress({
            **snapshot,
            "message": snapshot.get("message") or "Preparing import…",
            "percent": max(1.0, round(pct, 1)),
            "phase": "prepare",
            "ticks_total": snapshot.get("total_ticks") or snapshot.get("rows"),
            "files_done": snapshot.get("done"),
            "files_total": snapshot.get("files_total"),
        })

    try:
        if on_progress:
            on_progress({
                "message": "Preparing import…",
                "percent": 1,
                "phase": "prepare",
            })
        if start is None or end is None:
            raise ValueError("import span missing")

        span_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        span_end = datetime(end.year, end.month, end.day, 23, tzinfo=timezone.utc)
        sources, ticks_source = publisher.resolve_sources(instrument, span_start, span_end)
        if not sources:
            raise FileNotFoundError("No hour tick files found for import range")

        batch_count = (len(sources) + MAX_HOUR_FILES_PER_IMPORT - 1) // MAX_HOUR_FILES_PER_IMPORT
        imported_total = 0
        files_total = 0

        for batch_number, offset in enumerate(range(0, len(sources), MAX_HOUR_FILES_PER_IMPORT), start=1):
            if should_cancel and should_cancel():
                raise JobCancelled()

            batch_sources = sources[offset: offset + MAX_HOUR_FILES_PER_IMPORT]
            batch_ticks = count_ticks_in_files(batch_sources)
            batch_job_id = f"{job_id}-{batch_number:02d}"
            batch_dir = job_dir_for(mt5, batch_job_id)

            def batch_publish_progress(snapshot: dict[str, Any]) -> None:
                if not on_progress:
                    return
                publish_progress({
                    **snapshot,
                    "message": (
                        f"Preparing batch {batch_number} / {batch_count} · "
                        f"{snapshot.get('done', 0):,} / {snapshot.get('files_total', 0):,} hour file(s)"
                    ),
                })

            result = publisher.publish_sources(
                batch_sources,
                batch_ticks,
                batch_dir,
                on_progress=batch_publish_progress,
                should_cancel=should_cancel,
            )

            if batch_number > 1:
                kill_mt5_terminal(mt5.terminal_exe)
                wait_mt5_exit(mt5.terminal_exe)

            manifest = {
                "job_id": batch_job_id,
                "source_symbol": instrument.symbol,
                "custom_symbol": custom_name,
                "custom_path": "dukascopy",
                "origin_symbol": origin,
                "digits": str(instrument.price_decimals),
                # The first batch replaces the partial failed import; later
                # batches append their dates into the same custom symbol.
                "replace_existing": "1" if batch_number == 1 else "0",
                "tick_format": "bin_v1",
                "tick_mode": "hours",
                "hours_file": HOURS_MANIFEST,
                "ticks_total": str(result.rows),
                "hours_total": str(result.files_published),
            }
            if on_progress:
                on_progress({
                    "message": f"Importing batch {batch_number} / {batch_count}…",
                    "percent": round(100 * (batch_number - 1) / batch_count, 1),
                    "phase": "import_ticks",
                    "custom_symbol": custom_name,
                    "files_done": files_total,
                    "files_total": len(sources),
                    "ticks_imported": imported_total,
                    "ticks_total": ticks_source,
                })

            import_result = import_ticks(
                settings,
                mt5_raw,
                instrument,
                job_id=batch_job_id,
                manifest_fields=manifest,
                import_all=import_all,
                range_start=start,
                range_end=end,
                # Tick history is the import contract. The optional M30 cache
                # is generated lazily by MT5 when a chart/tester needs it, and
                # should not delay or invalidate a successful tick import.
                warm_cache=False,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            imported_total += int(import_result.get("ticks_imported", 0))
            files_total += result.hours_with_data

        return {
            "custom_symbol": custom_name,
            "ticks_imported": imported_total,
            "ticks_total": ticks_source,
            "ticks_source": ticks_source,
            "source_symbol": instrument.symbol,
            "import_all": import_all,
            "range": range_label,
            "hours_with_data": files_total,
        }
    except JobCancelled:
        abort_mt5_import(job_id, mt5_raw, settings=settings)
        raise
    finally:
        _active_terminal.pop(job_id, None)
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def origin_id_for(terminal_exe: Path) -> str | None:
    origin = terminal_exe.parent / "origin.txt"
    if not origin.is_file():
        return None
    return _read_origin_id(origin) or None


def list_terminal_processes() -> list[dict[str, Any]]:
    """Best-effort list of running terminal64 processes (Windows only)."""
    if os.name != "nt":
        return []
    script = (
        "Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue | "
        "ForEach-Object { $_.Id + '|' + $_.Path }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    processes: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        if "|" not in line:
            continue
        pid, _, path = line.partition("|")
        try:
            processes.append({"pid": int(pid.strip()), "path": path.strip()})
        except ValueError:
            continue
    return processes


def diagnose_mt5(mt5_raw: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve config into inspectable paths without launching MT5."""
    raw = dict(mt5_raw or {})
    out: dict[str, Any] = {
        "portable": _parse_portable(raw.get("portable")),
        "terminal_configured": str(raw.get("terminal_exe") or ""),
        "data_path_configured": str(raw.get("data_path") or ""),
    }
    if out["terminal_configured"]:
        out["terminal_source"] = "configured"
    else:
        try:
            _auto_exe, _auto_source = resolve_mt5_terminal_exe()
            out["terminal_source"] = _auto_source
        except ValueError:
            out["terminal_source"] = "auto-detect failed"
    try:
        mt5 = Mt5Settings.from_dict(raw)
    except ValueError as exc:
        out["ok"] = False
        out["error"] = str(exc)
        return out
    out["terminal_exe"] = str(mt5.terminal_exe)
    script_ex5 = mt5.data_path / "MQL5" / "Scripts" / "dukascopy" / SCRIPT_EX5_NAME
    out.update({
        "ok": True,
        "mode": "portable" if mt5.portable else "roaming",
        "terminal_exists": mt5.terminal_exe.is_file(),
        "data_path": str(mt5.data_path),
        "data_path_exists": mt5.data_path.is_dir(),
        "origin_id": origin_id_for(mt5.terminal_exe),
        "launch_args": build_mt5_launch_args(
            mt5.terminal_exe, Path("import.ini"), portable=mt5.portable,
        )[1:],
        "job_base": str(files_base_dir(mt5)),
        "script_ex5": str(script_ex5),
        "script_installed": script_ex5.is_file(),
        "warnings": [],
    })
    warnings: list[str] = []
    out["warnings"] = warnings
    if mt5.portable and mt5.data_path.resolve() != mt5.terminal_exe.parent.resolve():
        warnings.append(
            "Portable mode expects the data folder to be the terminal folder "
            f"({mt5.terminal_exe.parent}); imports still run but MQL5 must exist there.",
        )
    if not mt5.portable and mt5.data_path.resolve() == mt5.terminal_exe.parent.resolve():
        warnings.append(
            "Data folder equals the terminal folder but portable mode is OFF — "
            "the terminal will use its Roaming data folder instead. "
            "Enable portable mode or point the data folder at "
            "%APPDATA%\\MetaQuotes\\Terminal\\<instance>.",
        )
    return out


assemble_and_import = prepare_and_import
stage_and_import = prepare_and_import
