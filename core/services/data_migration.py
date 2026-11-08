"""Copy → verify → delete migration of a whole data root.

Moves an existing populated data root (ticks, metadata.db, staging, logs,
web settings) to a new location. The source is only removed after every file
has been copied and verified by size, so an interrupted migration leaves both
roots intact and can be re-run.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from config.data_config import validate_data_dir
from storage.fast_delete import fast_remove_tree

# Transient artifacts never copied to the destination.
_SKIP_NAMES = {".web.lock", ".write_probe"}


def migrate_data_dir(
    src: Path,
    dst: Path,
    *,
    delete_source: bool = False,
    force: bool = False,
    on_progress: "callable[[int, int], None] | None" = None,
) -> dict:
    """Copy src → dst, verify, and optionally delete src.

    Raises ValueError for invalid arguments and RuntimeError when the
    destination is unusable or verification fails.
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
    for path, target in source_files:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError as exc:
            raise RuntimeError(f"copy failed for {path}: {exc}") from exc
        copied += 1
        if on_progress is not None:
            on_progress(copied, len(source_files))

    # Verify: every source file exists at the destination with the same size.
    for path, target in source_files:
        if not target.is_file() or target.stat().st_size != path.stat().st_size:
            raise RuntimeError(f"verification failed for {target}")

    source_deleted = False
    if delete_source:
        fast_remove_tree(src)
        source_deleted = not src.exists()

    return {
        "files_copied": copied,
        "bytes_copied": total_bytes,
        "files_verified": len(source_files),
        "source_deleted": source_deleted,
    }
