"""Resolution and persistence of the tick data root.

The data root is the wholesale replacement of the repo's ``data/`` directory
(ticks, ``metadata.db``, locks, staging, CSV exports, logs, web settings). It
is resolved in this order:

1. ``DUKE_DATA_DIR`` environment variable (session-level override)
2. ``<repo>/.dukascopy/data.json`` (persistent, ``{"data_dir": "<abs>"}``)
3. Default: ``<repo>/data``
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR_ENV_VAR = "DUKE_DATA_DIR"

# Repo root, computed here (not imported from config.settings) to avoid a
# circular import: config.settings consumes this module.
ROOT = Path(__file__).resolve().parent.parent

CONFIG_SUBPATH = Path(".dukascopy") / "data.json"


def config_file() -> Path:
    """Path of the persistent data-root config file."""
    return ROOT / CONFIG_SUBPATH


def load_configured_data_dir() -> Path | None:
    """Return the configured absolute data root from disk, or None.

    Malformed JSON, non-string, or relative paths are ignored (the caller
    falls back to the default root rather than guessing).
    """
    path = config_file()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = raw.get("data_dir") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    resolved = Path(value).expanduser()
    return resolved if resolved.is_absolute() else None


def save_configured_data_dir(data_dir: Path | str) -> Path:
    """Persist an absolute data root. Raises ValueError on relative input."""
    resolved = Path(data_dir).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"data dir must be an absolute path: {resolved}")
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"data_dir": str(resolved)}, indent=2), encoding="utf-8")
    tmp.replace(path)
    return resolved


def clear_configured_data_dir() -> None:
    """Remove the persistent config file if present."""
    config_file().unlink(missing_ok=True)


def effective_data_dir() -> tuple[Path, str]:
    """Return (data_dir, source); source in {'env', 'file', 'default'}."""
    env_value = os.environ.get(DATA_DIR_ENV_VAR)
    if env_value and env_value.strip():
        return Path(env_value).expanduser(), "env"
    file_value = load_configured_data_dir()
    if file_value is not None:
        return file_value, "file"
    return ROOT / "data", "default"


def validate_data_dir(data_dir: Path) -> None:
    """Ensure data_dir can be created/written; raise RuntimeError otherwise.

    The default root is auto-created (same as today). A configured root that
    cannot be used must fail fast rather than fall back silently — a silent
    fallback would manufacture a second dataset against a different
    metadata.db, silently splitting the tick store from its index.
    """
    if not data_dir.is_absolute():
        raise RuntimeError(f"data root must be an absolute path, got: {data_dir}")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"data root is not usable: {data_dir} "
            f"(fix .dukascopy/data.json or unset {DATA_DIR_ENV_VAR})"
        ) from exc
