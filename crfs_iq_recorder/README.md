# CRFS IQ Recorder

Starts an IQ recording on a CRFS RFeye sensor, lists files over SFTP, and downloads WAVE files to this PC.

How the Recorder, Node, local files, Excel, and Converter fit together is in [Architecture](docs/ARCHITECTURE.md).

![Main window](docs/confluence/01-main-window.png)

## Share the EXE

On a PC with Python, double-click `build_exe.bat`. Copy `dist\CRFS_IQ_Recorder.exe` to a USB stick or shared folder. The other PC does not need Python. Set `CRFS_BUILD_INSTALLER=1` to also get `dist\CRFS_IQ_Recorder_Setup.exe`, an installer for company-managed PCs.

The EXE is not stored in Git. Each version is tagged and the EXE is attached to the matching GitHub Release (keep older releases for rollback).

### Windows warnings, signing, and the icon

Full analysis, signing enrollment steps and costs, IT guidance, and the diagnostics script are in [Distribution](docs/DISTRIBUTION.md).

- **"Windows protected your PC" / "Unknown publisher"** on first run is Microsoft Defender SmartScreen: the EXE is not signed with a trusted code-signing certificate, and every new build is a new file hash with no download history. Defender itself reports no threat for the build (checked with `MpCmdRun -Scan` before each release).
- **"Cannot run at all"** on a Windows 11 PC with **Smart App Control** on, or on a company PC with App Control (WDAC) / AppLocker: unsigned code is refused, including the DLL/PYD files the one-file EXE unpacks into `%TEMP%`. Turning those protections off is not the fix.
- **What fixes the publisher part:** sign every release, and every bundled binary, with a trusted identity. The build does this automatically when one of `CRFS_SIGN_ARTIFACT_METADATA` (Azure Artifact Signing, formerly Trusted Signing, the recommended option), `CRFS_SIGN_THUMBPRINT` (certificate in the Windows store or on a token) or `CRFS_SIGN_PFX` + `CRFS_SIGN_PFX_PASSWORD` is set; see `sign_exe.ps1`. Signing names the publisher, satisfies Smart App Control, and lets later releases inherit trust, but per Microsoft it does not give a brand-new file instant SmartScreen reputation, and EV no longer bypasses SmartScreen. Self-signed certificates behave like no signature and are refused by the script. **Releases are unsigned until the Sensorz signing enrollment is done.**
- **Managed PCs:** use the installer (`CRFS_IQ_Recorder_Setup.exe`, installs to Program Files) and ask IT for a publisher rule once releases are signed. `packaging\diagnose_block.ps1` tells which mechanism blocks a given PC.
- **Do not install into Documents, Desktop or Pictures.** Defender's Controlled folder access (ransomware protection) lets only trusted programs change files there, so an install in those folders may be impossible to uninstall ("Some elements could not be removed") and the app may be unable to save recordings there. The installer warns before doing so; keep the default folder.
- **If Defender ever flags the EXE** (it does not today), submit the file as a software developer at https://www.microsoft.com/wdsi/filesubmission instead of adding exclusions.
- The EXE carries a Windows version resource (product name, company, version), which is what Explorer, Task Manager, and the SmartScreen dialog display.
- **Icon:** the EXE embeds the Sensorz `.ico` (16-256 px), the window sets the same icon, and the process registers the AppUserModelID `Sensorz.CRFSIQRecorder` so the taskbar groups it on its own. If a pinned shortcut keeps an old icon after an update, unpin and pin again; Windows caches shortcut icons per file.
- **Startup:** the bundle contains only the Qt modules the app uses, and sensor checks, SFTP storage, the saved workbook, and the Excel queue start after the window is on screen. A sensor that is off or unreachable does not delay the window.

Downloads default to `%USERPROFILE%\CRFS IQ Recorder\Recordings` on the local disk, not OneDrive. Change the folder in Settings. The choice is remembered.

The main window shows IQ Storage for the sensor disk that holds `/mnt/1/remdata/` (today's dated folder does not need to exist yet). Under 10% free is a warning; under 5% is critical. Start Recording is blocked if the estimate plus a safety margin would not fit. Existing recordings are never deleted to free space.

## Record and download

1. Open Settings and enter the sensor IP, HTTP login, and SFTP login.
2. Choose Start / End or Center / Bandwidth, and the frequency units (kHz, MHz, or GHz).
3. Pick a recording format (WAVE, XDAT, NCP, or HDF5) and enter recording time in seconds, for example `0.1`, `1`, or `10`.
4. Click Start Recording.
5. Click Sensor Files to browse the sensor. Split recordings appear as a collapsible group, for example `iq_20260915_142718` with `_0001.wav` and `_0002.wav` underneath. Select the parent and Download this recording to fetch every listed part, or select individual children to copy only those files. Open recordings folder shows the local copies. A file already saved with the sensor's size is not downloaded again; a different local file with the same name is never overwritten. The **Req.** columns are what was requested; **Recorded (MHz)**, **Recorded (s)** and **Rate (MS/s)** are read from the file itself, or *unknown*. The **Local copy** column says where each file stands here (*Saved*, *Queued*, *Downloading*, *Waiting to retry*, *Verified*, *Failed*). Sizes are decimal (1 MB = 1,000,000 bytes). Right-click (1.19.0) acts on the row under the pointer, or on the whole selection when the row is part of it: **Download** / **Retry download**, **Open folder** (the downloaded copy, selected), **Copy filename**, **Delete from sensor…**; on a folder **Open**, **Open local folder**, **Copy folder name** and **Delete folder…**. Actions that cannot be used right now are greyed out with the reason (*Downloads in progress*, *Not downloaded yet*). The menu follows the light and dark theme.

The sensor writes the IQ files first. This app copies them here when you download.

**Where downloads go (1.16.0).** Local copies mirror the sensor's own folders: a file at `/mnt/1/remdata/20261104/iq_x.wav` is saved as `<Recordings>\20261104\iq_x.wav`, so files of one day stay together even when the download runs after midnight (the folder comes from the sensor's path, not from this computer's date). Meaningful subfolders on the sensor are kept; parts of one recording and its manifest, preview and merge output share one folder. If a *different* file of the same name is already there (another sensor, for example), the whole recording goes into a sensor subfolder (`<Recordings>\20261104\<serial>\`), and if that is taken too the copy gets a numbered name; nothing is ever overwritten. Paths that would leave the recordings folder (`..`, drive letters, names Windows refuses) fall back to the folder root. Files downloaded by earlier versions stay where they are and are recognised there; nothing is moved.

Below the manual form, the **Last recording** card shows the result in one line each (`1 file · 0.503 s · 251.4 MB`, then `Recording complete · Download saved`). **Download** fetches the files; **Open folder** is available once a verified local copy exists. The sensor-reported range is shown only when it differs from what was requested.

After the recording time has passed, Start Recording reads **Confirming files…** while the app listens to the sensor's task stream and watches the sensor folder. The two things are reported separately: **Sensor finished. Confirming file_063625_0001.wav…** means the sensor has reported its task complete but the file is still being checked; **Recording confirmed** means the named file(s) were found and stopped growing. If the sensor finishes but its file never appears, the status says so (**Sensor finished, but … was not found on the sensor**) instead of claiming completion.

## Merge recording parts

The sensor writes one WAVE file per 500 MB (see [Why the sensor splits recordings](#why-the-sensor-splits-recordings)). The optional merge joins those parts back into one file on this PC.

- **Merge recording parts** (main window, next to Sensor Files) is off by default and remembered between sessions. While it is on, every split recording that has been downloaded completely is merged in the background; downloads that fail or are cancelled are never merged. Turning it off stops new automatic merges; a merge that is already running keeps going until **Cancel merge** is pressed.
- **Merge parts** in Sensor Files merges the selected recording on demand (select the parent row or any of its parts). Parts that are not in the download folder yet are downloaded first through the normal download, then merged. The confirmation lists the parts, the verified sample duration, the output size, and the output folder. Manual merges do not need the toggle.
- The result is `<recording>_merged.wav` next to the parts, with a `<recording>_merged.wav.merge.json` sidecar (parts, sizes, start times, join checks). The original parts are kept. An existing merged file is never overwritten; a second manual merge writes `… (2).wav`.
- Before writing, every part is checked: same encoding, channel count, sample rate, centre frequency, bandwidth and receiver (from the CRFS `meta` chunk), numbered parts in order with none missing or duplicated, no truncated or still-growing file, and the next part starting where the previous one ends (CRFS `signaltime`, 1.5 ms tolerance). A gap, an overlap, a mixed frequency window or an unreadable part stops the merge with the reason in the log. Parts without time stamps are merged with **continuity unknown** noted in the log and sidecar. Samples are copied byte for byte; nothing is resampled, filled or dropped.
- Outputs up to 4 GiB are plain RIFF/WAVE. Larger outputs use RF64 (`ds64` chunk), which the Converter reads without changes. The sensor's `meta` chunk is carried over, and a `LIST/INFO` note records which parts were joined.
- A merge runs after the recording, independently of recording and download status: a failed merge never marks a recording failed. Progress and the result are shown under the toggle and in the log. A merge interrupted by a crash leaves only `… .wav.partial`, which the app removes on the next start.

## Why the sensor splits recordings

Measured on an RFeye Node 40-8 (firmware 2.25) and checked against the RFeye Site user guide: the Node's recording service writes IQ to `/mnt/1/remdata/{date}/file_{time}_{n:4}` with `filesize: 500MB` (`/etc/node/ncpd/ncpd.conf`, storage section; the factory default is `iq_{date}_{time}_{n:4}` with 100 MB). `filesize` is "the maximum size of a file": the Node closes a part after 1023 blocks of 512,000 bytes (523,776,000 bytes of IQ) and opens the next number, whatever the sample rate. At 31.25 MS/s (25 MHz bandwidth) that is 4.19 s per part; at 62.5 MS/s, 2.1 s. The `duration`/`rate`/`capture_length` request values do not cause the split; the app sends the same number of seconds for all three, and the parts' `signaltime` stamps are contiguous to within 1 ms, so a split recording is one continuous capture. The limit is set on the Node, not in this app; the app does not change it. See the QA report for the per-part measurements.

## Cellular Collection

Records every allocation of a cellular protocol workbook in a fixed order (bands by nominal frequency, downlink before uplink within each FDD band, TDD bands once), with a configurable wait between captures, once or at selected times of day. The panel sits under **Merge recording parts** in the main window and reuses the normal recording, SFTP download, Excel and merge paths; while a collection runs the manual **Start Recording** is held, and the other way round.

- The workbook is read by column headers (band, direction, start/stop frequencies, optional duration; Hz/kHz/MHz/GHz understood). Invalid rows are listed and block the plan; rows that already carry results are never recorded again; the imported protocol is snapshotted, so edits to the workbook do not change a running session.
- Allocations wider than the capture window (20 MHz by default) are recorded as consecutive sub-band windows that are listed before the run; this is sequential coverage, not a simultaneous full-band capture, and nothing is cropped, retuned or resampled silently.
- Every capture gets a `<recording>.cellular.json` manifest beside its parts; a session ends with a handover summary (`cellular_manifests\handover_*.json` / `.md`). With Excel logging on, captures are added to the collection workbook (class sheet or a **Cellular Results** sheet). Nothing is uploaded and no recording is deleted automatically.
- Interrupted sessions are shown on the next start with their uncertain captures; **Reconcile with sensor** checks the sensor's files before anything is resent. Missed scheduled rounds are listed to run or skip; they never start on their own at a different hour.
- **Fixed in 1.19.1:** a row could end *Uncertain* with no file when its request reached the sensor about 30 s after the previous recording ended, the moment the Node closes its receiver connection (the sensor then reports "finished" without recording). Such a request is now held a few seconds (at most 5) until that moment has passed, for manual recordings too, and a row the sensor still finishes without recording anything (no reply, no file, no message) is sent once more, unchanged.
- **Check readiness** (1.16.0) runs the pre-collection checks (sensor identity, clock, storage, destination, plan, workbook) and answers in one line (`Ready to start (10 checks passed).`, `Not ready: ...`, or `Ready to start. Excel is not available right now: results will wait in the queue until it is.`). It never starts a recording.
- **Open written Excel file** (1.16.0) opens the workbook that actually received results, even after another protocol workbook was chosen; when Excel already has it open, that window is brought to the front instead of a second copy. The button stays off, with the reason in its tooltip, until a result has been saved.
- **Preview** lists the rows in order with the requested range of each and a short size/time estimate; rows that cannot be recorded are listed with the reason. Technical detail goes to the diagnostic log (right-click the Activity panel for **Open diagnostic log** / **Copy diagnostic details**).

Operator steps are in the [Quick Start](docs/confluence/CRFS_IQ_Recorder_Quick_Start.md); the design is in [Cellular Collection](docs/CELLULAR_COLLECTION.md).

## Log recordings to Excel

Use this for field collection into an existing `.xlsm` workbook (macros stay in the file).

1. Tick Log recordings to Excel and choose the workbook. The app makes a backup next to it before the first write.
2. Collection event is filled from the workbook (for example Haifa Port). Change it if needed.
3. Set the frequency range as usual. Target class is suggested from that range and is editable. If more than one class could apply, pick the class and worksheet before starting.
4. Repeat can be Off, a count, or Until stopped. Wait between recordings defaults to 5 seconds; you can use seconds or minutes, including zero. The countdown shows **Next recording in 00:30** after the current capture has finished.
5. **Stop after current recording** lets an active capture finish and cancels a countdown. It does not start another task.
6. Each completed recording gets one Excel row. Split parts share that row, with every filename on its own line in IQ Recording. Playback Start is the recording time as plain text such as `2 seconds` or `5 seconds`. Existing template rows and playback records are left alone. Frequency columns store the actual capture window, not the full class band.

Recordings are consecutive tasks, not gapless IQ. If Excel has the workbook open, rows are written into that live file. If Excel is busy, the app keeps Pending Excel updates and retries.

Settings remembers the HTTP and SFTP usernames and passwords on this computer (protected). Password fields stay masked. They are not written as plaintext and they are not copied into the activity log. Do not put real IPs or passwords in Git, screenshots, or docs.

In Sensor Files, select one or more remote files and press Delete (or right-click **Delete from sensor…**) to remove them from the sensor; the app asks for confirmation first, because the files cannot be recovered. Selecting a split-recording parent deletes every listed part. Local downloads and Excel rows are left in place.

**Delete folder…** (1.19.0, right-click a folder) deletes a whole folder inside `/mnt/1/remdata/`, never the recording root itself. The contents are counted in the background first; one confirmation shows the sensor (model, serial, address), the full path and the counts, with **Cancel** as the default. Links are removed as links and never followed; a folder that is itself a link is refused. It is refused while a recording, a collection or a download from that folder runs, or when something in it was written in the last 2 minutes by the sensor's clock. Only what was counted is deleted, each item checked again just before it goes; progress, **Cancel** and partial failures are reported, the listing refreshes afterwards, and local copies are kept (the status line says where).

## Spectrograms in Excel

Since 1.19.0 every verified IQ download (manual recording, Excel Collection or Sensor Files) gets a spectrogram, made in the background by the IQ Spectrogram Converter engine (`iq_data.py`, bundled in the EXE) with the Converter's saved settings when this PC has them (`IQ_GUI_SETTINGS`, or `.iq_gui_settings.json` in the app's folder or a few folders above it), otherwise its defaults. It runs in a separate below-normal process that ends with the app, one file at a time, from a persisted queue (`spectrograms.json` in the config folder) that survives restarts and never converts a file twice. Output: `<WAV folder>\Spectrograms\<name>.png` and `<name>.thumb.png`; the WAV is only read. The axis is absolute MHz from the file's CRFS metadata, with the requested range from the recording history as dashed markers; missing metadata is never invented. A failure is logged once (*Spectrogram failed: <file>*) and never stops a collection. A 2.3 GB recording took about 5 minutes; without scipy in the EXE the engine uses a numpy FFT that matches scipy within 0.033 dB.

Every Excel row that lists the file (the Excel log and the Excel Collection results) gets a thumbnail in an **IQ Spectrogram** column directly right of **IQ Recording**. An existing IQ Spectrogram column is reused; an empty column there is used as it is; otherwise a column is inserted once, by Excel itself, so formulas, formats and tables stay correct. Multi-part recordings show one thumbnail per part, side by side. Thumbnails (96 x 48 pt, aspect kept, inside the cell) move and size with their row, so sorting and filtering keep them with the right file, and the row is found again by its file name at write time. Click a thumbnail to open the full-size PNG. If the workbook is in a OneDrive or SharePoint folder that this PC syncs, and the PNG is in a synced folder too, the link is the SharePoint address; otherwise the link points to the file on this PC, and its tooltip says so. Cloud addresses are never guessed.

When the workbook is open in Excel, thumbnails are added there and the workbook is saved. If Excel is busy (a cell is being edited or a dialog is open), they are added a few seconds after Excel is free. A closed workbook is updated directly only when that is safe: a backup is made first, and macros, formulas and existing pictures are kept. Otherwise its thumbnails are added the next time it is open in Excel, and Activity says so once. Pending thumbnails survive a restart.

**Fixed in 1.19.0:** appending a row to the Excel log (or a repeat block to the protocol) while a filter hid rows in Excel could overwrite row 2, because Excel then reports row 1 as the last used row. The last row is now found correctly with filters on.

## Run from source

Windows: double-click `Start CRFS IQ Recorder.bat`

Linux:

```bash
cd crfs_iq_recorder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start_crfs_iq_recorder.sh
```

## What the sensor request looks like

The app sends `POST http://{sensor_ip}/emp/` with HTTP Basic Auth. Use the username and password from Settings. Frequencies are integer Hz. Recording time is copied to `duration`, `rate`, and `capture_length`:

```json
{
  "remote_recording_scans": [
    {
      "center_frequency": 800000000,
      "bandwidth": 12500000,
      "task_id": "iq-testing",
      "duration": 0.1,
      "rate": 0.1,
      "capture_length": 0.1,
      "recording_format": "WAVE"
    }
  ]
}
```

If you enter `2.5` seconds, all three timing fields are `2.5`. Format defaults to WAVE. Each Start Recording click uses a timestamped `task_id` such as `iq-20260910-213005`.

A successful HTTP response means the request was accepted. It does not prove the file has finished writing. The app keeps the EMP response stream open: the sensor names each file it opens (`time.RemoteTimeData`) and reports "Remote recording is now finished". After the requested time the app also watches the sensor folder over SFTP (whether or not Sensor Files is open) until every file the sensor named is present and has stopped growing. There is no EMP completion URL in public CRFS docs.

"Finished" ends the radio capture, not the file: the sensor then writes the samples to its disk, and if the client disconnects before that is done it ends the task and closes the file at what it has written (a valid WAVE that is simply shorter). Since 1.17.0 the app keeps the stream open until the sensor has finished every file (real sizes and the CRFS metadata in its header). A 2 s recording at 60 MHz on a Node 100-18 is about 1 GB and took over a minute to write while other files were being downloaded.

Since 1.18.0 an Excel Collection lets recordings and downloads take turns on the sensor's disk: no download starts during a recording, and the next request waits for running downloads and until the sensor has written the previous recording out of its memory (`docs/CELLULAR_COLLECTION.md`, 1.18.0).

Estimated size uses `61.051 * (bandwidth_Hz / 10_000_000) * recording_seconds` MB. That number is an estimate for the whole capture. The file list shows each part's size separately.

## Connection and files

Host, ports, usernames, protected passwords, last band, recording time, frequency unit, format, wait interval, and the download folder are saved between sessions.

When a sensor IP is saved, the main window reads the Node webpage APIs and shows model, firmware, serial, and connection status. Those values come from `/api/node.json` and `/api/values/versions.json`.

SFTP opens `/mnt/1/remdata/YYYYMMDD/` using today's local date, and also checks the UTC date folder if the two differ. While the sensor's clock is known to be wrong, the folder of the sensor's own date comes first. If that folder is missing, the window opens `/mnt/1/remdata/` instead. After a recording, Refresh or reopening Sensor Files looks for today's folder again. Newest files are listed first. The file watch after a recording also looks in the folder the sensor names in its reply (`file_dir`), so a recording written under a wrong date (for example `/mnt/1/remdata/20120102/` after the sensor lost its time) is still confirmed and downloaded.

**Sensor clock (1.19.0).** The app compares the sensor's clock (status page, with its own UTC offset) with this computer's clock, allowing for the round trip, when it connects, before each collection and when a recording reply looks misdated. The comparison counts only when this computer's clock is verified (SNTP, or the Windows time service). If the sensor is more than 2 minutes off and idle, the app refreshes the sensor's time service by saving its Network Time page twice: first with a temporary extra copy of the first server, then with the original list, so the settings end unchanged (on firmware 2.25 a Save with unchanged values does not restart the time service, and the restart is what steps the clock). It reads the page back, then checks the time every 3 s for up to 90 s, and reports **Sensor clock synchronized.** only after the restored list and two correct readings. It never switches NTP on, never acts while a recording is running or finishing, tries at most twice per episode, then waits 30 minutes (persisted in `sensor_clock_recovery.json`). A pending restore is finished at the next start if the app was closed during a refresh. Failed connection checks while the sensor is saving its NTP page are ignored, because its web server answers nothing else for 6-10 s.

HTTP and SFTP are different logins. Test HTTP connection does not test SFTP.

Each Start Recording click stores start/end, center, bandwidth, and total duration on this PC (no passwords). If the sensor splits a capture into `_0001.wav`, `_0002.wav`, and so on, Sensor Files groups those parts under one parent and Excel keeps them on one row. Select the parent to download or delete every listed part. Duration in the file list is the total recording time, not the length of one file.

New files show a NEW badge. Shift/Ctrl-select several files and Download them in parallel (up to four at a time). Downloads go to a `.part` file first. After a download finishes, the recordings folder opens.

Click a WAVE file for a short spectrogram preview of the start of the file (I left, Q right). A quiet preview does not mean the whole recording is empty.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - components, recording flow, threads, persistence, and Converter hand-off
- [Distribution](docs/DISTRIBUTION.md) - SmartScreen / Smart App Control / WDAC analysis, code signing, installer, IT guidance
- [Cellular Collection](docs/CELLULAR_COLLECTION.md) - protocol import, sequencing, scheduling, recovery, manifests (developer report)
- [Quick Start](docs/confluence/CRFS_IQ_Recorder_Quick_Start.md)

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

## Build the EXE

On Windows with Python, double-click `build_exe.bat`. It writes `dist\CRFS_IQ_Recorder.exe`. Since 1.19.0 the spec bundles the Converter engine (`iq_data.py`, `iq_metadata.py` from the repository root, plus `soundfile` and Pillow) and stops with a clear message if those files are missing; scipy stays out. With `CRFS_BUILD_INSTALLER=1` it also writes `dist\CRFS_IQ_Recorder_Setup.exe` (needs Inno Setup 6: `winget install --id JRSoftware.InnoSetup --exact --scope user`). Signing is automatic when signing credentials are configured; see [Distribution](docs/DISTRIBUTION.md).
