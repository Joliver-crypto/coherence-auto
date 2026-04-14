' ============================================================================
'  run_liveview.vbs  —  Launcher for Coherence Auto
' ============================================================================
'
'  Overview
'  --------
'  This VBScript is called by the "Coherence Auto" desktop shortcut.
'  It launches the Electron app (which in turn spawns the Python backend).
'  If Node/Electron is not installed it falls back to running the Python
'  backend alone (you can then open http://127.0.0.1:5000 in a browser).
'
' ============================================================================

Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

scriptDir  = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
electronDir = fso.BuildPath(projectDir, "electron")

' --- Try launching Electron first ----------------------------------------
electronCmd = "cmd /c cd /d """ & electronDir & """ && npx electron ."
WshShell.CurrentDirectory = electronDir
On Error Resume Next
WshShell.Run electronCmd, 1, False
If Err.Number <> 0 Then
    ' --- Fallback: just run the Python backend ---------------------------
    Err.Clear
    On Error GoTo 0
    WshShell.CurrentDirectory = scriptDir
    WshShell.Run "python """ & fso.BuildPath(scriptDir, "webrtc_camera.py") & """", 1, False
End If
On Error GoTo 0
