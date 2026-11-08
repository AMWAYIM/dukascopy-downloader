"""Server-side native path pickers via Windows PowerShell dialogs.

The web UI runs in a browser, which cannot open native file/folder dialogs.
This module shells out to PowerShell on the machine running the server and
presents the standard Windows dialogs (OpenFileDialog / FolderBrowserDialog),
returning the chosen absolute path. Used by the Settings Browse… buttons.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired as _TimeoutExpired
from typing import Literal

_CANCELLED = "__CANCELLED__"
_PICKER_TIMEOUT = 60.0

_PS_SCRIPT = r"""
$mode = $env:DUKE_PICKER_MODE
$initial = $env:DUKE_PICKER_INITIAL
Add-Type -AssemblyName System.Windows.Forms
if ($mode -eq 'file') {
    $d = New-Object System.Windows.Forms.OpenFileDialog
    $d.Filter = 'Executable (*.exe)|*.exe|All files (*.*)|*.*'
    $d.Title = 'Select MetaTrader 5 terminal64.exe'
    if ($initial) { $d.InitialDirectory = $initial }
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output $d.FileName
    } else {
        Write-Output '__CANCELLED__'
    }
} else {
    $d = New-Object System.Windows.Forms.FolderBrowserDialog
    $d.Description = 'Select a folder'
    if ($initial) { $d.SelectedPath = $initial }
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output $d.SelectedPath
    } else {
        Write-Output '__CANCELLED__'
    }
}
"""


def _find_powershell() -> str:
    found = shutil.which("powershell")
    if found:
        return found
    found = shutil.which("pwsh")
    if found:
        return found
    raise RuntimeError("PowerShell not found on this machine")


def has_interactive_desktop() -> bool:
    """True when the current session can show a visible dialog.

    Uses OpenInputDesktop: in a non-interactive session (service, scheduled
    task, SSH) there is no input desktop and a dialog would hang invisibly.
    Fail-open: if the check itself cannot run, assume interactive.
    """
    try:
        user32 = ctypes.windll.user32
        # DESKTOP_SWITCHDESKTOP = 0x0100
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return False
        user32.CloseDesktop(desktop)
        return True
    except Exception:  # noqa: BLE001
        return True


def show_path_picker(
    mode: Literal["file", "folder"],
    initial_dir: Path | str | None = None,
    timeout: float = _PICKER_TIMEOUT,
) -> Path | None:
    """Present a native dialog; return the picked path or None on cancel.

    Raises RuntimeError when PowerShell is missing, the dialog process fails,
    the session is non-interactive, or the dialog times out.
    """
    if mode not in ("file", "folder"):
        raise ValueError(f"mode must be 'file' or 'folder', got: {mode}")
    if not has_interactive_desktop():
        raise RuntimeError(
            "no interactive desktop available - run the app from your desktop "
            "session (not a service/SSH/scheduled task)"
        )

    env = os.environ.copy()
    env["DUKE_PICKER_MODE"] = mode
    initial = Path(initial_dir).expanduser() if initial_dir else None
    env["DUKE_PICKER_INITIAL"] = str(initial) if initial and initial.is_dir() else ""

    cmd = [
        _find_powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-STA",
        "-Command",
        _PS_SCRIPT,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except _TimeoutExpired as exc:
        raise RuntimeError(
            f"native picker timed out after {int(timeout)}s - "
            "is the app running on an interactive desktop?"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to launch native picker: {exc}") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"native picker failed: {detail}")

    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    result = lines[-1] if lines else ""
    if result == _CANCELLED:
        return None
    if not result:
        raise RuntimeError("native picker produced no path")
    return Path(result)
