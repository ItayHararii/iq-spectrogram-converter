# CRFS IQ Recorder - Quick Start Guide

CRFS IQ Recorder is a Windows app for CRFS sensors. It starts an IQ recording, downloads the WAVE files to this computer, and shows a short waterfall preview so you can check that a signal was captured.

## Download and run

**Version:** 1.8.7
**File:** `CRFS_IQ_Recorder.exe` - one file, no Python, no install. `CRFS_IQ_Recorder_Setup.exe` - installer for company-managed PCs (installs to Program Files, Start-menu shortcut). With the installer, keep the default folder: do not install into Documents, Desktop or Pictures - Windows ransomware protection guards those folders and can block the uninstaller there.

> Windows EXE: [CRFS IQ Recorder 1.8.7](https://github.com/ItayHararii/iq-spectrogram-converter/releases/tag/v1.8.7)

1. Save the exe on this PC (Desktop or a local folder).
2. Double-click **CRFS_IQ_Recorder.exe**. The window opens in a few seconds; sensor details fill in afterwards.
3. This computer must be able to reach the sensor on the network.

**"Windows protected your PC"** can appear the first time you run a new version. That is Microsoft Defender SmartScreen saying the file is new to it and is not signed by a publisher it knows; it is not a virus detection. Check that you downloaded the file from the GitHub Release link above, then choose **More info -> Run anyway**. Do not turn off SmartScreen or Defender for this.

**The app does not start at all** on some Windows 11 PCs (Smart App Control on) and on company-managed PCs (App Control / AppLocker). Unsigned software is refused there by design; releases become signed once the Sensorz code-signing enrollment is complete. Until then: on a company PC, ask IT to deploy `CRFS_IQ_Recorder_Setup.exe`; do not turn off Smart App Control. To see which protection is involved, run `diagnose_block.ps1` from the project folder `packaging` next to the downloaded file and send the report it writes to the Desktop.

**Taskbar icon:** the app uses the Sensorz icon. If a pinned shortcut still shows an old or blank icon after an update, unpin it and pin the running app again (Windows caches shortcut icons).

Do not put real sensor IPs, logins, or passwords on this page or in screenshots.

## Record IQ

1. Open **Settings** and enter the sensor IP, HTTP login, and SFTP login.
2. Check **Model**, **Firmware**, **Serial**, and **Status** in the header. Status should read **Connected**. The same text is shown at the bottom left. The app checks the sensor every 10 seconds. After one missed check the badge turns amber **Reconnecting...**; after two it turns red **Disconnected**. It returns to green **Connected** on its own when the sensor answers. SFTP problems stay in Sensor Files and are not treated as an API disconnect.
3. Choose **Start / End** or **Center / Bandwidth** (values in MHz).
4. Enter **Recording time (s)** and check **Estimate**.
5. Click **Start Recording**. Status goes **Sending request...**, then **Recording...**, then **WAVE file found** when files appear.

To log into a collection workbook: tick **Log recordings to Excel**, choose the `.xlsm` file, and check the collection event and target class. When a recording finishes, the app adds that capture to the Excel log automatically (class, frequencies, filename, and Playback Start as `N seconds`). Split parts stay on the same row.

Repeat count or Until stopped runs consecutive recordings, with a wait between them (default 5 seconds). **Stop after current recording** finishes the capture that is already running, or cancels the countdown, and does not start another.

HTTP and SFTP logins are remembered on this computer. In Sensor Files, select files and press Delete to remove them from the sensor.

![Main window: band and time, Log recordings to Excel, Start Recording, and Sensor Files.](01-main-window.png)

*Main window - band and time, Log recordings to Excel, Start Recording, and Sensor Files.*

## Find and download recordings

1. Click **Sensor Files**.
2. Today's folder on the sensor opens (`/mnt/1/remdata/YYYYMMDD/`). New files show **NEW**. A split capture appears as a collapsible parent such as `iq_20260915_142718`, with `_0001.wav` and `_0002.wav` underneath. Single files stay as normal rows.
3. Start, end, center, bandwidth, and duration are shown when the app has them. The parent shows part count and combined size.
4. Select the parent and click **Download this recording** for every listed part, or select individual children to download only those files. There is no save dialog. Delete is keyboard-only on the focused file list. Connecting and waterfall stay in the background, so you can still move, resize, or close the window, and **Open recordings folder** works while a sensor connection is pending.

Default local folder: `%USERPROFILE%\CRFS IQ Recorder\Recordings` on the local disk, not OneDrive. Change it in Settings.

The main window shows **IQ Storage** for the sensor disk that holds `/mnt/1/remdata/`. It refreshes on connect, while connected, and after a recording or a remote delete. Under 10% free is a yellow warning; under 5% is red. A recording that would not fit, including a safety margin, is blocked. Existing files are never deleted to free space.

Change it in **Settings -> Download folder**. The choice is remembered. Files stay on this PC (not OneDrive or other cloud folders).

![Sensor Files window with the file list, recording details, Download, and the local save path.](02-sensor-files.png)

*Sensor Files - file list, recording details, Download, and the local save path.*

## Preview the captured signal

Click a WAVE file. A short waterfall of the first portion appears under the list (frequency on X, time on Y, color in dBFS/bin). Use it as a quick visual check.

![Waterfall preview of the selected IQ file.](03-waterfall-preview.png)

*Waterfall preview of the selected IQ file - a short overview, not a full analysis.*

## Useful notes

- Recordings are saved on the sensor first. **Download** copies them to this computer.
- Status in the header and at the bottom left is the sensor API link. SFTP errors are shown in Sensor Files.
- Today's sensor folder is created after the first recording task is submitted.
- Large recordings may be split into numbered files. The parent row represents the whole capture; download or delete that parent if you need every listed part.
- With Excel logging on, each finished recording is added to the workbook automatically.
- The waterfall preview is a short overview of the start of the file.
