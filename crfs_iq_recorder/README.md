# CRFS IQ Recorder

Starts an IQ recording on a CRFS RFeye sensor, lists files over SFTP, and downloads WAVE files to this PC.

![Main window](docs/confluence/01-main-window.png)

## Share the EXE

On a PC with Python, double-click `build_exe.bat`. Copy `dist\CRFS_IQ_Recorder.exe` to a USB stick or shared folder. The other PC does not need Python.

The EXE is not stored in Git. Attach it to a GitHub Release when you share it.

Downloads default to `%USERPROFILE%\CRFS IQ Recorder\Recordings` on the local disk, not OneDrive. Change the folder in Settings. The choice is remembered.

## Record and download

1. Open Settings and enter the sensor IP, HTTP login, and SFTP login.
2. Choose Start / End or Center / Bandwidth, and the frequency units (kHz, MHz, or GHz).
3. Pick a recording format (WAVE, XDAT, NCP, or HDF5) and enter recording time in seconds, for example `0.1`, `1`, or `10`.
4. Click Start Recording.
5. Click Sensor Files to browse the sensor. Select any split part and Download this recording to fetch `_0001`, `_0002`, and the rest of that group. Open recordings folder shows the local copies. Existing local files are not overwritten (`iq (2).wav`).

The sensor writes the IQ files first. This app copies them here when you download.

Password fields start empty. Type the HTTP and SFTP logins in Settings. Passwords stay in memory for the session. They are not written to the settings file or the activity log. Do not put real IPs or passwords in Git, screenshots, or docs.

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

A successful HTTP response means the request was accepted. It does not prove the file has finished writing. After the requested time, the app looks for matching WAVE files over SFTP (if that window is open). There is no EMP completion URL in public CRFS docs.

Estimated size uses `61.051 * (bandwidth_Hz / 10_000_000) * recording_seconds` MB. That number is an estimate for the whole capture. The file list shows each part's size separately.

## Connection and files

Host, ports, usernames, last band, recording time, frequency unit, format, and the download folder are saved between sessions.

When a sensor IP is saved, the main window reads the Node webpage APIs and shows model, firmware, serial, and connection status. Those values come from `/api/node.json` and `/api/values/versions.json`.

SFTP opens `/mnt/1/remdata/YYYYMMDD/` using today's local date, and also checks the UTC date folder if the two differ. If that folder is missing, the window opens `/mnt/1/remdata/` instead. After a recording, Refresh or reopening Sensor Files looks for today's folder again. Newest files are listed first.

HTTP and SFTP are different logins. Test HTTP connection does not test SFTP.

Each Start Recording click stores start/end, center, bandwidth, and total duration on this PC (no passwords). If the sensor splits a capture into `_0001.wav`, `_0002.wav`, and so on, every part shows those same parameters. Select any part and Download this recording to copy the whole group. Duration is the total recording time, not the length of one file.

New files show a NEW badge. Shift/Ctrl-select several files and Download them in parallel (up to four at a time). Downloads go to a `.part` file first. After a download finishes, the recordings folder opens.

Click a WAVE file for a short spectrogram preview of the start of the file (I left, Q right). A quiet preview does not mean the whole recording is empty.

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

## Build the EXE

On Windows with Python, double-click `build_exe.bat`. It writes `dist\CRFS_IQ_Recorder.exe`.
