# Creates / refreshes "IQ Spectrogram Converter.lnk" with the Sensorz icon.
# Windows taskbar uses the .lnk icon when launching via pythonw.exe.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RecorderIco = Join-Path $Root "crfs_iq_recorder\assets\sensorz_icon.ico"
$LocalIco = Join-Path $Root "assets\sensorz_icon.ico"
$Ico = if (Test-Path $RecorderIco) { $RecorderIco } else { $LocalIco }
$Pythonw = Join-Path $Root "venv\Scripts\pythonw.exe"
$Gui = Join-Path $Root "iq_gui.py"
$Lnk = Join-Path $Root "IQ Spectrogram Converter.lnk"

if (-not (Test-Path $Ico)) {
    Write-Error "Missing icon: $Ico"
    exit 1
}
if (-not (Test-Path $Pythonw)) {
    Write-Error "Missing pythonw: $Pythonw"
    exit 1
}
if (-not (Test-Path $Gui)) {
    Write-Error "Missing GUI script: $Gui"
    exit 1
}

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($Lnk)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$Gui`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.IconLocation = "$Ico,0"
$Shortcut.Description = "IQ Spectrogram Converter"
$Shortcut.Save()

Write-Host "Shortcut ready: $Lnk"
Write-Host "  Target: $($Shortcut.TargetPath)"
Write-Host "  Args:   $($Shortcut.Arguments)"
Write-Host "  Icon:   $($Shortcut.IconLocation)"
