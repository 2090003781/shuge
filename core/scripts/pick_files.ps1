# pick_files.ps1 -- native Windows "Open File" dialog; returns selected absolute paths.
#
# Why: the web console cannot obtain local file paths (browser sandbox), so the local
#      core service launches this script to pop the real Windows dialog and hand the
#      absolute paths back to the front end.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File pick_files.ps1 -OutFile <path> [-Multi] [-InitialDir <dir>]
#
# Output: writes JSON (UTF-8, no BOM) to -OutFile:
#   {"ok":true,"files":["D:\\a\\b.md"]}
#   {"ok":false,"error":"canceled"}         <- user canceled
#   {"ok":false,"error":"..."}              <- no desktop session / component unavailable
#
# NOTE: keep this file PURE ASCII. Windows PowerShell 5.1 reads .ps1 using the system
#       ANSI code page unless the file has a BOM; non-ASCII literals break parsing.

param(
  [string]$OutFile = "",
  [switch]$Multi,
  [string]$InitialDir = "",
  [string]$Filter = "Text files (*.md;*.txt;*.json;*.yaml;*.yml)|*.md;*.txt;*.json;*.yaml;*.yml|All files (*.*)|*.*"
)

$ErrorActionPreference = "Stop"

function Write-Result($obj) {
  $json = $obj | ConvertTo-Json -Compress -Depth 4
  if ($OutFile) {
    [System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
  } else {
    [Console]::Out.Write($json)
  }
  exit 0
}

try {
  Add-Type -AssemblyName System.Windows.Forms | Out-Null
} catch {
  Write-Result @{ ok = $false; error = "cannot load System.Windows.Forms (no desktop session?): $($_.Exception.Message)" }
}

try {
  $dlg = New-Object System.Windows.Forms.OpenFileDialog
  $dlg.Title = "Select files to add to the context pack"
  $dlg.Multiselect = [bool]$Multi
  $dlg.Filter = $Filter
  $dlg.CheckFileExists = $true
  $dlg.RestoreDirectory = $true
  if ($InitialDir -and (Test-Path -LiteralPath $InitialDir)) { $dlg.InitialDirectory = $InitialDir }

  $result = $dlg.ShowDialog()
  if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Result @{ ok = $false; error = "canceled" }
  }
  $files = @($dlg.FileNames)
  if ($files.Count -eq 0) { Write-Result @{ ok = $false; error = "canceled" } }
  Write-Result @{ ok = $true; files = $files }
} catch {
  Write-Result @{ ok = $false; error = "dialog unavailable: $($_.Exception.Message)" }
}
