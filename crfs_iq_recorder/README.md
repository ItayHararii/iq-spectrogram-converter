# CRFS IQ Recorder

A small local tool for starting an IQ recording on a CRFS sensor and opening the folder where the file is stored.

## Workflow

1. Open **Connection Settings** once (sensor IP, HTTP login, optional SFTP login).
2. Choose **Start / End** or **Center / Bandwidth** (values in MHz).
3. Enter **Recording time (seconds)** — for example `0.1`, `1`, or `10`.
4. Click **Start Recording**.
5. Click **Open Sensor Files (SFTP)** to browse and download from the sensor.

IQ files are written on the sensor, not on this PC.

## Setup

**Windows:** double-click `Start CRFS IQ Recorder.bat`

**Linux:**

```bash
cd crfs_iq_recorder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start_crfs_iq_recorder.sh
```

**Demo (no sensor):** `python -m crfs_iq_recorder --demo`

## What is sent

`POST http://{sensor_ip}/emp/` with HTTP Basic Auth (defaults `admin` / `pass`).

Frequencies are converted from MHz to Hz. Recording time is copied to all three EMP timing fields:

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

Entering `2.5` seconds sends `"duration": 2.5, "rate": 2.5, "capture_length": 2.5`. Format is always WAVE.

A successful HTTP response means the **request was accepted**. It does not by itself prove the file has finished writing.

Estimated size is the existing empirical formula from bandwidth and recording time. It is approximate.

## Connection and files

Host, ports, and usernames are saved between sessions. Passwords stay in memory for the session only — they are not written to the settings file or the activity log.

When a sensor IP is saved, the main window reads the Node webpage APIs and shows **model**, **firmware**, **serial**, and **connection status** at the top. Those values come from the sensor (`/api/node.json` and `/api/values/versions.json`), not from a static inventory.

SFTP opens `/mnt/1/remdata/YYYYMMDD/` using **today's local date** on this computer. If that folder is missing (the sensor creates it only after a recording is started), the window says so and opens `/mnt/1/remdata/` instead. After a recording, Refresh or reopening Sensor Files looks for today's folder again. HTTP and SFTP are different logins (CRFS SSH is not the HTTP admin account).

Each **Start Recording** click stores start/end, center, bandwidth, and total recording duration on this PC (no passwords). If the sensor splits a capture into `_0001.wav`, `_0002.wav`, … parts, every part shows those same parameters. The duration column is the total recording time, not the length of one file. Select several files with Shift/Ctrl and use **Download Selected Files** to fetch them in parallel.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

## Packaging

`build_exe.bat` builds `dist\CRFS_IQ_Recorder.exe`.
