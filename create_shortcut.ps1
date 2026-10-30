# ---------------------------------------------------------------------------
# create_shortcut.ps1 - makes a "Pokemon TCG Tracker" shortcut (Windows) with
# the app logo, on the Desktop and in the Start Menu. Right-click either one
# and choose "Pin to taskbar" to get a one-click launcher.
#
#   Double-click "Create Shortcut.bat"   (easiest), or run:
#   powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Stop'

$appDir = $PSScriptRoot
$runBat = Join-Path $appDir 'run.bat'
$icon   = Join-Path $appDir 'assets\icon.ico'
$name   = 'Pokemon TCG Tracker'

$targets = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "$name.lnk"))
    # Point at cmd.exe (not run.bat directly) so Windows offers "Pin to taskbar".
    $lnk.TargetPath       = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $lnk.Arguments        = "/c `"$runBat`""
    $lnk.WorkingDirectory = $appDir
    $lnk.IconLocation     = "$icon,0"
    $lnk.WindowStyle      = 7   # start minimized; the browser opens by itself
    $lnk.Description      = 'Pokemon TCG Inventory & Portfolio Tracker'
    $lnk.Save()
    Write-Host "Created $($lnk.FullName)"
}

Write-Host ''
Write-Host 'Done! To pin it: right-click the new desktop icon and choose'
Write-Host '"Pin to taskbar" (Windows 11: "Show more options" first).'
