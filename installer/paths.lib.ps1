# Reusable path-configuration helpers for the Dukascopy downloader.
# Dot-source from other scripts:  . "$PSScriptRoot\paths.lib.ps1"

$script:RepoRoot = Split-Path -Parent $PSScriptRoot


function Get-TickDataRoot {
  <#
    .SYNOPSIS
      Returns the configured tick data root (fallback: <repo>/data).
  #>
  $cfg = Join-Path $script:RepoRoot '.dukascopy\data.json'
  if (Test-Path -LiteralPath $cfg) {
    try {
      $j = Get-Content -Raw -LiteralPath $cfg | ConvertFrom-Json
      if ($j.data_dir -and (Test-Path -LiteralPath $j.data_dir)) { return $j.data_dir }
    } catch {}
  }
  return (Join-Path $script:RepoRoot 'data')
}


function Get-ConfiguredPaths {
  <#
    .SYNOPSIS
      Returns current paths as a hashtable:
      TerminalExe, Mt5DataPath, Portable, TickDataRoot.
  #>
  $root = Get-TickDataRoot
  $ws = Join-Path $root 'web_settings.json'
  $mt5 = @{ terminal_exe = ''; data_path = ''; portable = $false }
  if (Test-Path -LiteralPath $ws) {
    try {
      $m = Get-Content -Raw -LiteralPath $ws | ConvertFrom-Json
      if ($m.mt5) {
        $mt5.terminal_exe = [string]$m.mt5.terminal_exe
        $mt5.data_path = [string]$m.mt5.data_path
        $mt5.portable = $m.mt5.portable -eq $true
      }
    } catch {}
  }
  return @{
    TerminalExe = $mt5.terminal_exe
    Mt5DataPath = $mt5.data_path
    Portable = $mt5.portable
    TickDataRoot = $root
  }
}


function Show-FolderPicker([string]$InitialDirectory = '') {
  <# Native Windows folder picker; returns the path or '' on cancel. #>
  Add-Type -AssemblyName System.Windows.Forms
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = 'Select a folder'
  if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory)) {
    $d.SelectedPath = $InitialDirectory
  }
  if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $d.SelectedPath }
  return ''
}


function Show-FilePicker(
  [string]$InitialDirectory = '',
  [string]$Filter = 'Executable (*.exe)|*.exe|All files (*.*)|*.*'
) {
  <# Native Windows file picker; returns the path or '' on cancel. #>
  Add-Type -AssemblyName System.Windows.Forms
  $d = New-Object System.Windows.Forms.OpenFileDialog
  $d.Filter = $Filter
  $d.Title = 'Select a file'
  if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory)) {
    $d.InitialDirectory = $InitialDirectory
  }
  if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $d.FileName }
  return ''
}


function Get-DetectedMt5Terminal([string]$PythonExe) {
  <# Best-effort auto-detect terminal64.exe via the app's resolver. #>
  $out = & $PythonExe -c "from export.mt5_importer import resolve_mt5_terminal_exe; p,_=resolve_mt5_terminal_exe(); print(p)" 2>$null
  if ($LASTEXITCODE -eq 0 -and $out) { return ([string]$out).Trim() }
  return ''
}


function Get-DetectedMt5DataPath([string]$PythonExe, [string]$TerminalExe) {
  <# Best-effort auto-detect the MT5 data folder for a terminal. #>
  if (-not $TerminalExe) { return '' }
  $env:DUKE_TERMINAL_EXE = $TerminalExe
  try {
    $out = & $PythonExe -c "import os; from pathlib import Path; from export.mt5_importer import resolve_mt5_data_path; print(resolve_mt5_data_path(Path(os.environ['DUKE_TERMINAL_EXE'])))" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ([string]$out).Trim() }
  } finally {
    Remove-Item Env:DUKE_TERMINAL_EXE -ErrorAction SilentlyContinue
  }
  return ''
}


function Set-Mt5Terminal([string]$PythonExe, [string]$TerminalExe) {
  if (-not $TerminalExe) { throw 'empty terminal path' }
  & $PythonExe main.py mt5-config --terminal $TerminalExe
  if ($LASTEXITCODE -ne 0) { throw "failed to set MT5 terminal: $TerminalExe" }
}


function Set-Mt5DataFolder([string]$PythonExe, [string]$DataPath, [bool]$Portable) {
  if (-not $DataPath) { throw 'empty data folder path' }
  $args = @('main.py', 'mt5-config', '--data', $DataPath)
  if ($Portable) { $args += '--portable' } else { $args += '--roaming' }
  & $PythonExe @args
  if ($LASTEXITCODE -ne 0) { throw "failed to set MT5 data folder: $DataPath" }
}


function Set-TickDataRoot([string]$PythonExe, [string]$DataRoot) {
  if (-not $DataRoot) { throw 'empty data root path' }
  & $PythonExe main.py data-config --set $DataRoot
  if ($LASTEXITCODE -ne 0) { throw "failed to set tick data root: $DataRoot" }
}
