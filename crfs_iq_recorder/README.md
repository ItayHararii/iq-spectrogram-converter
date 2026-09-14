# CRFS IQ Recorder

Local Windows app for starting an IQ recording on a CRFS RFeye sensor, browsing the sensor over SFTP, and downloading WAVE files to this PC.

![Main window](docs/confluence/01-main-window.png)

## Windows (colleagues)

Build **`dist\CRFS_IQ_Recorder.exe`** with `build_exe.bat`, then copy that file to a USB stick or shared folder and double-click it. Python is not required on the PC that runs the EXE. The EXE is not stored in Git; attach it to a versioned GitHub Release when you distribute it.

IQ downloads go to **`%USERPROFILE%\CRFS IQ Recorder\Recordings`** (created automatically, local disk only — not OneDrive). Change that folder in **Settings**; the choice is remembered.

## Workflow

1. Open **Settings** and enter the sensor IP, HTTP login, and SFTP login.
2. Choose **Start / End** or **Center / Bandwidth**, and the frequency **units** (kHz, MHz, or GHz).
3. Pick a CRFS **recording format** (WAVE, XDAT, NCP, or HDF5) and enter **Recording time (seconds)** — for example `0.1`, `1`, or `10`.
4. Click **Start Recording**.
5. Click **Sensor Files** to browse the sensor. Select any split part and **Download this recording** to fetch all `_0001`, `_0002`, … files. **Open recordings folder** shows the local copies. Existing local files are not overwritten (`iq (2).wav`).

IQ files are written on the sensor first, then copied to this PC when you download them.

## Developer setup

**Windows:** double-click `Start CRFS IQ Recorder.bat`

**Linux:**

```bash
cd crfs_iq_recorder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start_crfs_iq_recorder.sh
```

## What is sent

`POST http://{sensor_ip}/emp/` with HTTP Basic Auth (defaults `admin` / `pass`).

Frequencies are converted to integer Hz. Recording time is copied to all three EMP timing fields:

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

Entering `2.5` seconds sends `"duration": 2.5, "rate": 2.5, "capture_length": 2.5`. Format defaults to WAVE; XDAT, NCP, and HDF5 are also sent as EMP `recording_format` values. Each Start Recording click uses a timestamped `task_id` such as `iq-20260910-213005` so jobs are distinguishable; the example above still shows the documented default.

A successful HTTP response means the **request was accepted**. It does not by itself prove the file has finished writing. After the requested time elapses, the app looks for matching WAVE files over SFTP (if that window is open) and only then reports that files were found. There is no EMP completion URL in public CRFS documentation.

Estimated size uses `61.051 × (bandwidth_Hz / 10_000_000) × recording_seconds` MB. It is labeled as an **estimate** for the whole capture (all split parts combined). The file list shows each part’s size separately.

## Connection and files

Host, ports, usernames, last band, recording time, frequency unit, format, and the download folder are saved between sessions. Passwords stay in memory for the session only — they are not written to the settings file or the activity log.

When a sensor IP is saved, the main window reads the Node webpage APIs and shows **model**, **firmware**, **serial**, and **connection status** at the top. Those values come from the sensor (`/api/node.json` and `/api/values/versions.json`), not from a static inventory.

SFTP opens `/mnt/1/remdata/YYYYMMDD/` using **today's local date**, and also checks the UTC date folder if the two differ. If that folder is missing (the sensor creates it only after a recording is started), the window says so and opens `/mnt/1/remdata/` instead. After a recording, Refresh or reopening Sensor Files looks for today's folder again. Files are listed with the newest first. HTTP and SFTP are different logins (CRFS SSH is not the HTTP admin account). **Test HTTP connection** does not test SFTP.

Each **Start Recording** click stores start/end, center, bandwidth, and total recording duration on this PC (no passwords). Those parameters follow the files even if the sensor IP changes. If the sensor splits a capture into `_0001.wav`, `_0002.wav`, … parts, every part shows those same parameters. Select any part and **Download this recording** to copy the whole group. The duration column is the total recording time, not the length of one file. Newly discovered files show a **NEW** badge. Select several files with Shift/Ctrl and use **Download** to fetch them in parallel (up to four at a time) into the saved folder. Downloads stream to a `.part` file first; existing local files are not overwritten. After a download finishes, the recordings folder opens. Click a WAVE file for a **short spectrogram preview** of the first portion of the file (same I-left / Q-right layout as the IQ-to-Spectrogram Converter). A quiet preview sample does not mean the whole recording is empty.

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

## Packaging

On a Windows PC with Python, double-click `build_exe.bat` (or run it from this folder). It writes a single standalone **`dist\CRFS_IQ_Recorder.exe`**. Give colleagues that file only.
