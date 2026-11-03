# CRFS IQ Recorder - Quick Start Guide

CRFS IQ Recorder is a Windows app for CRFS sensors. It starts an IQ recording, downloads the WAVE files to this computer, and shows a short waterfall preview so you can check that a signal was captured.

## Download and run

**Version:** 1.13.0
**File:** `CRFS_IQ_Recorder.exe` - one file, no Python, no install. `CRFS_IQ_Recorder_Setup.exe` - installer for company-managed PCs (installs to Program Files, Start-menu shortcut). With the installer, keep the default folder: do not install into Documents, Desktop or Pictures - Windows ransomware protection guards those folders and can block the uninstaller there.

> Windows EXE: [CRFS IQ Recorder 1.13.0](https://github.com/ItayHararii/iq-spectrogram-converter/releases/tag/v1.13.0)

1. Save the exe on this PC (Desktop or a local folder).
2. Double-click **CRFS_IQ_Recorder.exe**. The window opens in a few seconds; sensor details fill in afterwards.
3. This computer must be able to reach the sensor on the network.

**"Windows protected your PC"** can appear the first time you run a new version. That is Microsoft Defender SmartScreen saying the file is new to it and is not signed by a publisher it knows; it is not a virus detection. Check that you downloaded the file from the GitHub Release link above, then choose **More info -> Run anyway**. Do not turn off SmartScreen or Defender for this.

**The app does not start at all** on some Windows 11 PCs (Smart App Control on) and on company-managed PCs (App Control / AppLocker). Unsigned software is refused there by design; releases become signed once the Sensorz code-signing enrollment is complete. Until then: on a company PC, ask IT to deploy `CRFS_IQ_Recorder_Setup.exe`; do not turn off Smart App Control. To see which protection is involved, run `diagnose_block.ps1` from the project folder `packaging` next to the downloaded file and send the report it writes to the Desktop.

**Taskbar icon:** the app uses the Sensorz icon. If a pinned shortcut still shows an old or blank icon after an update, unpin it and pin the running app again (Windows caches shortcut icons).

Do not put real sensor IPs, logins, or passwords on this page or in screenshots.

## Record IQ

The window has two tabs under one sensor header: **Manual Recording** (one recording or a series) and **Excel Collection** (the rows of a protocol workbook). Only one of them uses the sensor at a time; while one runs or is paused, the other tab's Start is disabled and a line under it says why (for example *Excel Collection is running.*). Switching tabs never interrupts anything. The last tab and the form values come back at the next start; nothing starts by itself.

1. Open **Settings** and enter the sensor IP, HTTP login, and SFTP login.
2. Check **Model**, **Firmware**, **Serial**, and **Status** in the header. Status should read **Connected**. The same text is shown at the bottom left. The app checks the sensor every 10 seconds. After one missed check the badge turns amber **Reconnecting...**; after two it turns red **Disconnected**. It returns to green **Connected** on its own when the sensor answers. SFTP problems stay in Sensor Files and are not treated as an API disconnect.
3. Choose **Start / End** or **Center / Bandwidth** (values in MHz).
4. Enter **Recording time (s)** and check **Estimate**.
5. Click **Start Recording**. Status goes **Sending request…**, then **Recording**, then **Waiting for files** while the app waits for the sensor's files, then **Files ready**. If the sensor finishes but no file appears, the status says so instead of reporting a completed recording.
6. The **Last recording** card shows what happened: a tag (**Complete**, **Partial**, **Uncertain**, **Failed** or **Other range**), what was asked (`Requested: 100 MHz for 2 s`), what came back (`Received: 1 file, 2.01 s`, measured from the file itself) and the range actually recorded. **Partial** means the files hold more than 2 % less time than requested; **Other range** means the sensor recorded a different range than requested (the Node 100-18 records at most 100 MHz around the centre, so a 500 MHz request comes back as 100 MHz). **Download** copies the files to this PC; **Open folder** and **View waterfall** work once the files are downloaded.

To log into a collection workbook: tick **Log each recording to an Excel log workbook (.xlsm)**, choose the `.xlsm` file, and check the collection event and target class. When a recording finishes, the app adds that capture to the Excel log automatically (class, frequencies, filename, and Playback Start as `N seconds`). Split parts stay on the same row.

**Repeat recording** (a count or **Until stopped**) runs consecutive recordings, with a wait between them (default 5 seconds). **Pause** finishes the current recording and waits; **Resume** continues (time spent paused counts toward the wait). **Stop** finishes the capture that is already running, or cancels the countdown, and does not start another.

**When something goes wrong:** the activity log beside the form gives one short sentence per step. **See details** opens the diagnostic log (exact requests, sensor replies, errors). **Copy details** puts a report on the clipboard for support: app version, sensor model and serial, the last request and its reply, and the latest log lines. The sensor's address, other addresses, your user folder, the sensor's position and stored passwords are masked in it.

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

## Excel Collection (optional)

Records the frequency ranges listed in your protocol workbook, row by row, and repeats the whole list as often as you want so activity can be compared at different hours. Open the **Excel Collection** tab. The badge beside the title shows the state: **Idle** (no plan yet), **Ready**, **Recording**, **Waiting**, **Scheduled**, **Paused**, **Finished**, **Stopped** or **Interrupted**. While a collection runs or is paused, the Manual Recording tab's **Start Recording** is disabled with the reason under it, and the other way round.

The form reads top to bottom in the order you work: Excel file and worksheet, recording duration and wait, **Run now** or **Schedule**, and the action bar - **Preview**, **Start**, **Pause** / **Resume**, **Stop** with the one-line status - which stays visible under the form while it scrolls. The estimate and the kept result rows are behind **Details** next to the summary; problems are shown in the summary itself.

1. **Excel file and worksheet.** Click **Browse…** and pick the protocol workbook (`.xlsx` / `.xlsm`), then choose the worksheet. Each row is one recording: **Band**, **Direction**, **Duplex**, **Freq Start (MHz)**, **Freq Stop (MHz)**. The summary reads like `15 rows · 2 s per request · 10 s between recordings · 1 pass`; a row the app cannot use is listed in **Preview**, and Start stays disabled until it is fixed. **Reload** re-reads the workbook after you edit it.
2. **Recording duration** is set here, in seconds, and applies to every row. **Wait between recordings** is 10 s by default.
3. **Start: Run now** records the list once, or **Repeat** it a number of times / **Until stopped**, with the same wait between passes. **Schedule** shows the date, **Repeat daily** and the start times: **Add time** (beside the list) opens a time picker with hour and minute wheels and presets, each time has its own × to remove it, and the date field opens a calendar. Times are this computer's local time, shown in the Windows date and clock format; nothing has to be typed. The application must be running and the computer awake; a pass that was missed because the computer slept or the app was closed is reported when the app is back (**Run the missed pass now** or **Skip it**), and the log says when the computer was asleep. Passes never overlap: the next one starts only after the previous one has finished.
4. **Preview** shows the rows in worksheet order with the range requested for each. Every row is **one request** to the sensor, sent exactly as the row says (centre and bandwidth of its Start/Stop range, the chosen duration). What the sensor actually recorded is read from its files afterwards and shown in the result: a sensor that records less than requested (the Node 100-18 records at most 100 MHz around the centre) gives **Other range**, a recording shorter than requested gives **Partial**.
5. **Start** first checks which sensor is connected (model and serial; the plan is tied to it), shows the preview, and starts. The status line reads `Recording row 1 of 15: 703-713 MHz` or `Next recording in 10 s`; the **Last recording** card shows the row, its tag, `Requested` / `Received` / `Recorded`, and whether the files are downloaded and the workbook updated. While downloads lag, the line under the status says `Downloads: N waiting`; when too many wait, the collection pauses until they catch up and says so. **Pause** finishes the current recording and waits; **Resume** continues; **Stop** finishes the current recording and records nothing else. Before every request the app checks that the sensor is free.
6. **End of a pass.** A summary shows `Completed 14 · Partial 0 · Failed 1 · Uncertain 0`. **Retry failed rows** first checks any uncertain row with the sensor (a row whose file turns up counts as recorded and is not sent again), then sends only the failed rows again, unchanged (`attempt 2`). Completed and partial rows are kept. A failed attempt writes nothing to the workbook, so the retry fills the same row. Nothing is ever re-sent on its own.
7. **Downloads finish before the next collection.** A new collection can start only when the last one has finished downloading (Start says *The last collection is still downloading…*). If the app is closed before the downloads finish, it says so at the next start; **Continue** downloads the rest (the files are still on the sensor) and sends nothing.

If the app or the computer stopped during a collection, the next start shows what was interrupted. **Check with the sensor** looks for files of recordings that were not confirmed, **Continue** finishes the pass, and **Dismiss** hides the notice; nothing is sent again without you pressing a button.

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
