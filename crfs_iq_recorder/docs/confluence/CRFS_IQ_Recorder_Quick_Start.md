# CRFS IQ Recorder - Quick Start Guide

CRFS IQ Recorder is a Windows app for CRFS sensors. It starts an IQ recording, downloads the WAVE files to this computer, and shows a short waterfall preview so you can check that a signal was captured.

## Download and run

**Version:** 1.12.0
**File:** `CRFS_IQ_Recorder.exe` - one file, no Python, no install. `CRFS_IQ_Recorder_Setup.exe` - installer for company-managed PCs (installs to Program Files, Start-menu shortcut). With the installer, keep the default folder: do not install into Documents, Desktop or Pictures - Windows ransomware protection guards those folders and can block the uninstaller there.

> Windows EXE: [CRFS IQ Recorder 1.12.0](https://github.com/ItayHararii/iq-spectrogram-converter/releases/tag/v1.12.0)

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
5. Click **Start Recording**. Status goes **Sending request...**, then **Recording...**, then **Confirming files...** while the app waits for the sensor's files. **Sensor finished. Confirming file_063625_0001.wav...** means the sensor has reported the task complete and the app is checking that the file has stopped growing; **Recording confirmed** means every file the sensor named is there. If the sensor finishes but no file appears, the status says so instead of reporting a completed recording.

To log into a collection workbook: tick **Log recordings to Excel**, choose the `.xlsm` file, and check the collection event and target class. When a recording finishes, the app adds that capture to the Excel log automatically (class, frequencies, filename, and Playback Start as `N seconds`). Split parts stay on the same row.

Repeat count or Until stopped runs consecutive recordings, with a wait between them (default 5 seconds). **Stop after current recording** finishes the capture that is already running, or cancels the countdown, and does not start another.

HTTP and SFTP logins are remembered on this computer. In Sensor Files, select files and press Delete to remove them from the sensor (a confirmation is shown first).

![Main window: band and time, Log recordings to Excel, Start Recording, and Sensor Files.](01-main-window.png)

*Main window - band and time, Log recordings to Excel, Start Recording, and Sensor Files.*

## Find and download recordings

1. Click **Sensor Files**.
2. Today's folder on the sensor opens (`/mnt/1/remdata/YYYYMMDD/`). New files show **NEW**. A split capture appears as a collapsible parent such as `iq_20260915_142718`, with `_0001.wav` and `_0002.wav` underneath. Single files stay as normal rows.
3. Start, end, center, bandwidth, and duration are shown when the app has them. The parent shows part count and combined size.
4. Select the parent and click **Download this recording** for every listed part, or select individual children to download only those files. There is no save dialog. Delete is keyboard-only on the focused file list and asks for confirmation. Connecting and waterfall stay in the background, so you can still move, resize, or close the window, and **Open recordings folder** works while a sensor connection is pending.

Default local folder: `%USERPROFILE%\CRFS IQ Recorder\Recordings` on the local disk, not OneDrive. Change it in Settings.

The main window shows **IQ Storage** for the sensor disk that holds `/mnt/1/remdata/`. It refreshes on connect, while connected, and after a recording or a remote delete. Under 10% free is a yellow warning; under 5% is red. A recording that would not fit, including a safety margin, is blocked. Existing files are never deleted to free space.

Change it in **Settings -> Download folder**. The choice is remembered. Files stay on this PC (not OneDrive or other cloud folders).

![Sensor Files window with the file list, recording details, Download, and the local save path.](02-sensor-files.png)

*Sensor Files - file list, recording details, Download, and the local save path.*

## Merge split recordings (optional)

The sensor closes a recording file every 500 MB and continues in the next numbered part (`_0001.wav`, `_0002.wav`, ...). At 25 MHz bandwidth that is about 4.2 seconds per part. The parts are one continuous capture; this is a setting on the sensor, not in the app.

- Tick **Merge recording parts** in the main window to have every fully downloaded split recording joined into one file automatically. The option is off by default and is remembered. Untick it to stop new merges; **Cancel merge** stops the one that is running.
- Or select a recording in Sensor Files and click **Merge parts**. Parts that are not on this PC yet are downloaded first. The confirmation shows the parts, the verified duration, the output size, and the folder.
- The result is `<recording>_merged.wav` in the download folder, next to the original parts, which are kept. A `.merge.json` file next to it lists what was joined and the join checks. Open the merged file in IQ Spectrogram Converter like any other WAVE file; files over 4 GB are written as RF64, which the Converter reads.
- The app checks that the parts belong together (same frequency window, sample rate and format, numbered without gaps, each part starting where the previous one ends) before it writes anything. If something does not match, the merge is refused and the reason is in the log; the recording and the downloads are not affected.

## Cellular Collection (optional)

Records the frequency ranges listed in your protocol workbook, row by row, and repeats the whole list as often as you want so activity can be compared at different hours. The section sits at the bottom of the recording form, under **Merge recording parts**; click **Cellular Collection** (the chevron at the left) to open or close it. The badge beside the title shows the state: **Idle** (no plan yet), **Ready**, **Recording**, **Waiting**, **Scheduled**, **Paused**, **Finished** or **Interrupted**. While a collection runs, the manual **Start Recording** button is held, and the other way round.

The form reads top to bottom in the order you work: Excel file and worksheet, recording duration and wait, **Run now** or **Schedule**, and the action bar - **Preview**, **Start**, **Pause** / **Resume**, **Stop** with the one-line status - which stays visible under the form while it scrolls. Long explanations (the sensor's capture window, sequential windows, kept result rows) are behind **Details** next to the summary and in the activity log; problems are shown in the summary itself.

1. **Excel file and worksheet.** Click **Browse…** and pick the protocol workbook (`.xlsx` / `.xlsm`), then choose the worksheet. Each row is one recording: **Band**, **Direction**, **Duplex**, **Freq Start (MHz)**, **Freq Stop (MHz)**. The summary says how many rows were read, how many captures that means and the estimated storage per pass (`15 rows · 54 captures · Estimated 34.0 GB`); a row the app cannot use is counted there (`2 problems (see Preview)`), listed in **Preview**, and the plan stays disabled until it is fixed. **Reload** re-reads the workbook after you edit it.
2. **Recording duration** is set here, in seconds, and applies to every row. **Wait between recordings** is 10 s by default.
3. **Start: Run now** records the list once, or **Repeat** it a number of times / **Until stopped**, with the same wait between passes. **Schedule** shows the date, **Repeat daily** and the start times: **Add time** (beside the list) opens a time picker with hour and minute wheels and presets, each time has its own × to remove it, and the date field opens a calendar. Times are this computer's local time, shown in the Windows date and clock format; nothing has to be typed. The application must be running and the computer awake; a pass that was missed because the computer slept or the app was closed is reported when the app is back (**Run the missed pass now** or **Skip it**), and the log says when the computer was asleep. Passes never overlap: the next one starts only after the previous one has finished.
4. **Preview** shows the rows in worksheet order with the exact range recorded for each. The sensor's capability decides how wide one capture can be at the chosen duration (under **Details**, for example *CRFS 100-18: up to 50 MHz per capture at 2 s*). A row wider than that is recorded as **sequential frequency windows**, each for the full duration, one after another - the preview and the row's result say so, and the files are listed together on the protocol row. The windows are not recorded at the same time and are never merged into one IQ stream.
5. **Start** first checks which sensor is connected (model and serial; the plan is tied to it), shows the preview with any note about sequential windows, and starts. The status line in the action bar reads `700 MHz (B68 / B28) DL - row 1 of 15 - recording` or `Next recording in 10 seconds`, with the pass number and any pending Excel updates under it; everything else is in the activity log. **Pause** finishes the current recording and waits; **Resume** continues; **Stop** finishes the current recording and records nothing else (the button's tooltip says so). A row counts as recorded only when every one of its captures has been confirmed from the files on the sensor; a pass with a row that was not completed says so in the log.

**Window layout.** The activity log sits beside the form; drag the dotted handle between them to resize it, or use **Hide log** / **Show log** in the header (dragging the log shut does the same). The moon / sun button switches between the light and dark theme.

**What is written to the workbook.** Only the protocol's own nine columns are used: **Band, Direction, Duplex, Freq Start (MHz), Freq Stop (MHz), Sensor, IQ Recording, IQ Duration (s), Recording Date & Time**. The first pass fills the empty result cells of the protocol rows themselves; every further pass appends the same rows as a new block below the last used row of the same worksheet, with the frequencies copied (still numbers, shown with *MHz*) and its own results. **Sensor** is the connected sensor's verified identity in readable form (`CRFS 100-18`), **IQ Recording** lists the recording's files one per line (split parts of one recording stay together), **IQ Duration (s)** is the measured seconds as a number (`2.011`, not rounded), and **Recording Date & Time** is the actual local capture start as a real Excel date and time shown as `dd/mm/yyyy hh:mm:ss`. No Window, Status, Notes or Round columns are added and no new worksheets are created; earlier results and anything you typed in the workbook are kept, because the app only writes the cells of its own rows. A `.crfs-backup-<date>` copy is made before each collection's first change of the workbook; if the backup cannot be made, nothing is written and the panel says so. If the workbook is open in Excel, the results are written through Excel into the open workbook (your unsaved edits stay); if another program is holding the file in a way that blocks writing, the results wait (*N Excel update(s) pending*) and are written when the file is free - also after the app is restarted, and also when the collection itself has already finished. If the workbook cannot even be opened when you press **Reload** or **Start** (locked, drive unplugged), the rows imported earlier from it stay the plan and the results are queued the same way; the panel says so.

Technical details per capture (exact window frequencies, requested and measured seconds, UTC timestamps, sensor serial and firmware, file sizes and SHA-256) are kept outside the worksheet: in the `<recording>.cellular.json` manifest beside the downloaded files, in the session state and the Excel queue under the app's configuration folder (`cellular\session.json`, `cellular\workbook_queue.json`), and in the handover summary (`cellular_manifests\handover_*.json` / `.md`) written at the end of a session. Nothing is uploaded anywhere and no recording is deleted automatically.

## Preview the captured signal

Click a WAVE file. A short waterfall of the first portion appears under the list (frequency on X, time on Y, color in dBFS/bin). Use it as a quick visual check.

![Waterfall preview of the selected IQ file.](03-waterfall-preview.png)

*Waterfall preview of the selected IQ file - a short overview, not a full analysis.*

## Useful notes

- Recordings are saved on the sensor first. **Download** copies them to this computer.
- Status in the header and at the bottom left is the sensor API link. SFTP errors are shown in Sensor Files.
- Today's sensor folder is created after the first recording task is submitted.
- Recordings over 500 MB are split into numbered files by the sensor. The parent row represents the whole capture; download or delete that parent if you need every listed part, and use **Merge recording parts** or **Merge parts** to join them on this PC.
- With Excel logging on, each finished recording is added to the workbook automatically.
- The waterfall preview is a short overview of the start of the file.
