# ============================================================================
#  create_shortcut.ps1  —  Creates the "Coherence Auto" desktop shortcut
# ============================================================================
#
#  Overview
#  --------
#  Run this script once to create a .lnk shortcut in the coherenceauto folder.
#  The shortcut has a camera icon and launches the Electron app (via VBS).
#
#  Usage:   powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
#
# ============================================================================

$base    = "C:\Users\Smash Mouth\Desktop\coherenceauto"
$scripts = Join-Path $base "scripts"
$vbs     = Join-Path $scripts "run_liveview.vbs"
$lnk     = Join-Path $base "Coherence Auto.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$s = $WshShell.CreateShortcut($lnk)
$s.TargetPath       = "wscript.exe"
$s.Arguments         = "`"$vbs`""
$s.WorkingDirectory  = $scripts
$s.IconLocation      = "$env:SystemRoot\System32\imageres.dll,109"
$s.Description       = "Launch Coherence Auto - Live Camera Viewer"
$s.Save()

Write-Host "Shortcut created: $lnk"
