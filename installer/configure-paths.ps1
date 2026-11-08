# Interactive configuration of the three data paths:
#   MT5 terminal64.exe, MT5 data folder, tick data root.
# Uses native Windows pickers (Browse) and the app's auto-detect, and writes
# through the python CLIs so validation/precedence match the GUI.
param(
  [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'paths.lib.ps1')

function Write-Ok([string]$Message) { Write-Host $Message -ForegroundColor Green }
function Write-WarnLine([string]$Message) { Write-Host $Message -ForegroundColor Yellow }

if (-not $PythonExe) {
  $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
  $PythonExe = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
}

$p = Get-ConfiguredPaths
Write-Host ''
Write-Host 'Current configuration:' -ForegroundColor White
Write-Host "  MT5 terminal     : $($p.TerminalExe)"
Write-Host "  MT5 data folder  : $($p.Mt5DataPath)   (portable: $($p.Portable))"
Write-Host "  Tick data root   : $($p.TickDataRoot)"

Push-Location $RepoRoot
try {
  while ($true) {
    Write-Host ''
    Write-Host 'Choose what to configure:' -ForegroundColor Yellow
    Write-Host '  [1] MT5 terminal64.exe'
    Write-Host '  [2] MT5 data folder'
    Write-Host '  [3] Tick data root'
    Write-Host '  [4] All of the above'
    Write-Host '  [q] Quit'
    $choice = Read-Host 'Select'

    $targets = @()
    switch ($choice) {
      '1' { $targets = @('terminal') }
      '2' { $targets = @('data') }
      '3' { $targets = @('tick') }
      '4' { $targets = @('terminal', 'data', 'tick') }
      'q' { break }
      default { Write-WarnLine "Unknown choice: $choice"; continue }
    }
    if ($choice -eq 'q') { break }

    foreach ($t in $targets) {
      $p = Get-ConfiguredPaths

      if ($t -eq 'terminal') {
        $initial = if ($p.TerminalExe) { Split-Path $p.TerminalExe -Parent } else { $env:ProgramFiles }
        $method = Read-Host 'Terminal: [B]rowse or [A]uto-detect? (B/A)'
        $path = ''
        if ($method -match '^(a|auto)') {
          $path = Get-DetectedMt5Terminal -PythonExe $PythonExe
          if (-not $path) { Write-WarnLine 'Auto-detect found nothing; falling back to browse.' }
        }
        if (-not $path) { $path = Show-FilePicker -InitialDirectory $initial }
        if (-not $path) { continue }
        $confirm = Read-Host "Use terminal: $path ? [y/N]"
        if ($confirm -notmatch '^(y|yes)$') { continue }
        Set-Mt5Terminal -PythonExe $PythonExe -TerminalExe $path
        Write-Ok "terminal set: $path"
      }
      elseif ($t -eq 'data') {
        $initial = if ($p.Mt5DataPath) { $p.Mt5DataPath } else { $p.TickDataRoot }
        $method = Read-Host 'MT5 data folder: [B]rowse or [A]uto-detect? (B/A)'
        $path = ''
        if ($method -match '^(a|auto)') {
          $term = $p.TerminalExe
          if (-not $term) { $term = Get-DetectedMt5Terminal -PythonExe $PythonExe }
          $path = Get-DetectedMt5DataPath -PythonExe $PythonExe -TerminalExe $term
          if (-not $path) { Write-WarnLine 'Auto-detect found nothing; falling back to browse.' }
        }
        if (-not $path) { $path = Show-FolderPicker -InitialDirectory $initial }
        if (-not $path) { continue }
        $confirm = Read-Host "Use data folder: $path ? [y/N]"
        if ($confirm -notmatch '^(y|yes)$') { continue }

        $portable = $p.Portable
        $term = $p.TerminalExe
        if (-not $term) { $term = Get-DetectedMt5Terminal -PythonExe $PythonExe }
        if ($term) {
          $termDir = (Split-Path $term -Parent).TrimEnd('\', '/')
          $pickedDir = $path.TrimEnd('\', '/')
          if ([string]::Equals($termDir, $pickedDir, [System.StringComparison]::InvariantCultureIgnoreCase)) {
            $ans = Read-Host 'This equals the terminal folder - enable portable mode? [Y/n]'
            if ($ans -notmatch '^(n|no)$') { $portable = $true }
          }
        }
        Set-Mt5DataFolder -PythonExe $PythonExe -DataPath $path -Portable $portable
        Write-Ok "MT5 data folder set: $path (portable: $portable)"
      }
      else {
        $ans = Read-Host "Tick data root (current: $($p.TickDataRoot)) - use folder picker? [Y/n]"
        if ($ans -match '^(n|no)$') { continue }
        $path = Show-FolderPicker -InitialDirectory $p.TickDataRoot
        if (-not $path) { continue }
        $confirm = Read-Host "Set tick data root: $path ? [y/N]"
        if ($confirm -notmatch '^(y|yes)$') { continue }
        Set-TickDataRoot -PythonExe $PythonExe -DataRoot $path
        Write-Ok "tick data root set: $path"
      }
    }
  }
} finally {
  Pop-Location
}
Write-Host 'Done.'
