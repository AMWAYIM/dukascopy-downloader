"""Server-side native path pickers via Windows PowerShell dialogs.

The web UI runs in a browser, which cannot open native file/folder dialogs.
This module shells out to PowerShell on the machine running the server and
presents a modern native dialog (COM ``IFileOpenDialog``, Vista style),
falling back to the classic WinForms dialogs if COM interop is unavailable.

To avoid the per-request PowerShell cold start and C# recompilation
(``Add-Type``) that made Browse feel slow, a persistent worker process stays
alive after its first use and serves every later dialog. The one-shot path
remains as a fallback if the worker fails. Only one picker may be open at a
time (see ``is_picker_busy``).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from subprocess import TimeoutExpired as _TimeoutExpired
from typing import Literal

_CANCELLED = "__CANCELLED__"
_PICKER_TIMEOUT = 45.0

_picker_lock = threading.Lock()
_worker_proc: subprocess.Popen | None = None


def is_picker_busy() -> bool:
    """True when a picker dialog is currently open (non-blocking)."""
    return _picker_lock.locked()


_CORE_CSHARP = r"""
using System;
using System.IO;
using System.Runtime.InteropServices;

namespace DukePicker
{
    public static class FOS
    {
        public const uint PICKFOLDERS = 0x20;
        public const uint FORCEFILESYSTEM = 0x40;
        public const uint PATHMUSTEXIST = 0x800;
        public const uint FILEMUSTEXIST = 0x1000;
        public const uint NOCHANGEDIR = 0x8;
    }

    public static class SIGDN
    {
        public const uint FILESYSPATH = 0x80058000;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct FILTERSPEC
    {
        public string pszName;
        public string pszSpec;
    }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IShellItem
    {
        [PreserveSig] int BindToHandler(IntPtr pbc, [In] ref Guid bhid, [In] ref Guid riid, [Out, MarshalAs(UnmanagedType.Interface)] out object ppv);
        [PreserveSig] int GetParent([Out, MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
        [PreserveSig] int GetDisplayName([In] uint sigdnName, [Out, MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
        [PreserveSig] int GetAttributes([In] uint sfgaoMask, out uint psfgaoAttribs);
        [PreserveSig] int Compare([In] IShellItem psi, [In] uint hint, out int piOrder);
    }

    [ComImport, Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IFileDialog
    {
        [PreserveSig] int Show([In] IntPtr hwndOwner);
        [PreserveSig] int SetFileTypes([In] uint cFileTypes, [In, MarshalAs(UnmanagedType.LPArray)] FILTERSPEC[] rgFilterSpec);
        [PreserveSig] int SetFileTypeIndex([In] uint iFileType);
        [PreserveSig] int GetFileTypeIndex(out uint piFileType);
        [PreserveSig] int Advise([In, MarshalAs(UnmanagedType.Interface)] object pfde, out uint pdwCookie);
        [PreserveSig] int Unadvise([In] uint dwCookie);
        [PreserveSig] int SetOptions([In] uint fos);
        [PreserveSig] int GetOptions(out uint pfos);
        [PreserveSig] int SetDefaultFolder([In, MarshalAs(UnmanagedType.Interface)] IShellItem psi);
        [PreserveSig] int SetFolder([In, MarshalAs(UnmanagedType.Interface)] IShellItem psi);
        [PreserveSig] int GetFolder([Out, MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
        [PreserveSig] int GetCurrentSelection([Out, MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
        [PreserveSig] int SetFileName([In, MarshalAs(UnmanagedType.LPWStr)] string pszName);
        [PreserveSig] int GetFileName([Out, MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        [PreserveSig] int SetTitle([In, MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        [PreserveSig] int SetOkButtonLabel([In, MarshalAs(UnmanagedType.LPWStr)] string pszText);
        [PreserveSig] int SetFileNameLabel([In, MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        [PreserveSig] int GetResult([Out, MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
        [PreserveSig] int AddPlace([In, MarshalAs(UnmanagedType.Interface)] IShellItem psi, [In] uint fdap);
        [PreserveSig] int SetDefaultExtension([In, MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        [PreserveSig] int Close([In] uint hr);
        [PreserveSig] int SetClientGuid([In] ref Guid guid);
        [PreserveSig] int ClearClientData();
        [PreserveSig] int SetFilter([In, MarshalAs(UnmanagedType.Interface)] object pFilter);
        [PreserveSig] int GetFilter([Out, MarshalAs(UnmanagedType.Interface)] out object pFilter);
    }

    [ComImport, Guid("D57C7288-D4AD-4768-BE02-9D969532D960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IFileOpenDialog : IFileDialog
    {
        [PreserveSig] int GetResults([Out, MarshalAs(UnmanagedType.Interface)] out object ppenum);
        [PreserveSig] int GetSelectedItems([Out, MarshalAs(UnmanagedType.Interface)] out object ppsai);
    }

    public static class Native
    {
        public static readonly Guid CLSID_FileOpenDialog = new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7");
        public static readonly Guid IID_IFileOpenDialog = new Guid("D57C7288-D4AD-4768-BE02-9D969532D960");
        public static readonly Guid IID_IShellItem = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");

        public const uint CLSCTX_INPROC_SERVER = 0x1;

        [DllImport("ole32.dll")]
        public static extern int CoCreateInstance(
            [In] ref Guid rclsid,
            [In, MarshalAs(UnmanagedType.Interface)] object pUnkOuter,
            [In] uint dwClsContext,
            [In] ref Guid riid,
            [Out, MarshalAs(UnmanagedType.Interface)] out object ppv);

        [DllImport("shell32.dll")]
        public static extern int SHCreateItemFromParsingName(
            [In, MarshalAs(UnmanagedType.LPWStr)] string pszPath,
            IntPtr pbc,
            [In] ref Guid riid,
            [Out, MarshalAs(UnmanagedType.Interface)] out object ppv);

        public static string PickFolderOrFile(bool folder, string initialDir)
        {
            object raw;
            int hr = CoCreateInstance(ref CLSID_FileOpenDialog, null, CLSCTX_INPROC_SERVER, ref IID_IFileOpenDialog, out raw);
            if (hr != 0) throw new COMException("CoCreateInstance failed: 0x" + hr.ToString("X8"));

            IFileOpenDialog dlg = (IFileOpenDialog)raw;
            uint opts = FOS.FORCEFILESYSTEM | FOS.NOCHANGEDIR;
            if (folder) opts |= FOS.PICKFOLDERS;
            else opts |= FOS.FILEMUSTEXIST | FOS.PATHMUSTEXIST;
            dlg.SetOptions(opts);

            if (folder)
            {
                dlg.SetTitle("Select a folder");
            }
            else
            {
                FILTERSPEC[] filters = new FILTERSPEC[2];
                filters[0].pszName = "Executable (*.exe)";
                filters[0].pszSpec = "*.exe";
                filters[1].pszName = "All files (*.*)";
                filters[1].pszSpec = "*.*";
                dlg.SetFileTypes((uint)filters.Length, filters);
                dlg.SetFileTypeIndex(1);
                dlg.SetTitle("Select MetaTrader 5 terminal64.exe");
            }

            if (!string.IsNullOrEmpty(initialDir) && Directory.Exists(initialDir))
            {
                object item;
                hr = SHCreateItemFromParsingName(initialDir, IntPtr.Zero, ref IID_IShellItem, out item);
                if (hr == 0) dlg.SetFolder((IShellItem)item);
            }

            hr = dlg.Show(IntPtr.Zero);
            if (hr != 0) return null; // cancelled or failed

            IShellItem result;
            hr = dlg.GetResult(out result);
            if (hr != 0) return null;

            string path;
            hr = result.GetDisplayName(SIGDN.FILESYSPATH, out path);
            if (hr != 0 || string.IsNullOrEmpty(path)) return null;
            return path;
        }
    }
}
"""

_ONE_SHOT_SCRIPT = r"""
$mode = $env:DUKE_PICKER_MODE
$initial = $env:DUKE_PICKER_INITIAL
$src = @'
__CSHARP__
'@
try {
    Add-Type -TypeDefinition $src
    $out = [DukePicker.Native]::PickFolderOrFile($mode -eq 'folder', $initial)
    if ($null -eq $out) { Write-Output '__CANCELLED__' } else { Write-Output $out }
} catch {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    if ($mode -eq 'file') {
        $d = New-Object System.Windows.Forms.OpenFileDialog
        $d.Filter = 'Executable (*.exe)|*.exe|All files (*.*)|*.*'
        $d.Title = 'Select MetaTrader 5 terminal64.exe'
        if ($initial) { $d.InitialDirectory = $initial }
        if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName }
        else { Write-Output '__CANCELLED__' }
    } else {
        $d = New-Object System.Windows.Forms.FolderBrowserDialog
        $d.Description = 'Select a folder'
        if ($initial) { $d.SelectedPath = $initial }
        if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
        else { Write-Output '__CANCELLED__' }
    }
}
""".replace("__CSHARP__", _CORE_CSHARP)

_WORKER_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$src = @'
__CSHARP__
'@
Add-Type -TypeDefinition $src | Out-Null
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if (-not $line.Trim()) { continue }
    try {
        $req = $line | ConvertFrom-Json
        $mode = [string]$req.mode
        $initial = [string]$req.initial
        $out = [DukePicker.Native]::PickFolderOrFile($mode -eq 'folder', $initial)
        if ($null -eq $out) {
            Write-Output '{"path":null}'
        } else {
            $escaped = $out.Replace('\', '\\').Replace('"', '\"')
            Write-Output ('{"path":"' + $escaped + '"}')
        }
    } catch {
        $msg = $_.Exception.Message.Replace('\', '\\').Replace('"', '\"')
        Write-Output ('{"error":"' + $msg + '"}')
    }
}
""".replace("__CSHARP__", _CORE_CSHARP)


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


def _spawn_worker() -> subprocess.Popen:
    global _worker_proc
    cmd = [
        _find_powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-STA",
        "-Command",
        _WORKER_SCRIPT,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _worker_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    return _worker_proc


def _pick_via_worker(mode: str, initial_dir: str) -> Path | None:
    """Use the persistent warm PowerShell worker (fast path)."""
    proc = _worker_proc
    if proc is None or proc.poll() is not None:
        proc = _spawn_worker()
    payload = json.dumps({"mode": mode, "initial": initial_dir}) + "\n"
    proc.stdin.write(payload)  # type: ignore[union-attr]
    proc.stdin.flush()  # type: ignore[union-attr]
    line = proc.stdout.readline()  # type: ignore[union-attr]
    if not line.strip():
        raise RuntimeError("picker worker exited unexpectedly")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"picker worker returned invalid output: {line.strip()}") from exc
    if "error" in data:
        raise RuntimeError(data["error"])
    path = data.get("path")
    return Path(path) if path else None


def _pick_oneshot(mode: str, initial_dir: Path | None, timeout: float = _PICKER_TIMEOUT) -> Path | None:
    """One-shot subprocess dialog (fallback path, slower)."""
    env = os.environ.copy()
    env["DUKE_PICKER_MODE"] = mode
    env["DUKE_PICKER_INITIAL"] = str(initial_dir) if initial_dir and initial_dir.is_dir() else ""

    cmd = [
        _find_powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-STA",
        "-Command",
        _ONE_SHOT_SCRIPT,
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


def show_path_picker(
    mode: Literal["file", "folder"],
    initial_dir: Path | str | None = None,
    timeout: float = _PICKER_TIMEOUT,
) -> Path | None:
    """Present a native dialog; return the picked path or None on cancel.

    Uses the persistent worker for speed, falling back to a one-shot
    subprocess if the worker fails. Only one picker may run at a time; a
    concurrent call raises RuntimeError. Also raises RuntimeError when
    PowerShell is missing, the session is non-interactive, or the dialog
    times out.
    """
    if mode not in ("file", "folder"):
        raise ValueError(f"mode must be 'file' or 'folder', got: {mode}")
    if not has_interactive_desktop():
        raise RuntimeError(
            "no interactive desktop available - run the app from your desktop "
            "session (not a service/SSH/scheduled task)"
        )
    if not _picker_lock.acquire(blocking=False):
        raise RuntimeError("a browse dialog is already open")

    try:
        initial = Path(initial_dir).expanduser() if initial_dir else None
        initial_str = str(initial) if initial and initial.is_dir() else ""
        try:
            return _pick_via_worker(mode, initial_str)
        except Exception:
            # Fallback: one-shot dialog process. Uses the same lock, so the
            # single-active-picker guarantee still holds.
            return _pick_oneshot(mode, initial, timeout=timeout)
    finally:
        _picker_lock.release()
