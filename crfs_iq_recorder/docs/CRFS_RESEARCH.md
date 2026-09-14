# CRFS EMP / remote IQ recording — research note

This note records what was verified before implementing CRFS IQ Recorder, and what remains unconfirmed. Public CRFS material does not publish the EMP JSON schema. The HTTP POST contract used by this application is the user-supplied integration request, corroborated by internal Sensorz documentation of the same sensor interface.

## Sources

### Official CRFS (public)

- [CRFS API Overview (CR-005348-MD)](https://www.crfs.com/hubfs/Brochures/APIs/API%20Overview.pdf) — EMP vs GMP; remote I/Q recording; storage on the sensor or external storage; VITA-49 streaming; REST + JSON. No endpoint paths, field names, or `recording_format` values.
- [RFeye software suite / APIs](https://www.crfs.com/software/rfeye-software-suite) — EMP = non-synchronous single-node tasks; GMP = synchronous multi-node tasks.
- [Node firmware release notes](https://www.crfs.com/software/release-notes/rfeye-node-firmware) — EMP API versions; Tasker can run EMP missions locally and save data on the Node; “EMP Status Information” (GPS/status) exists but **no URL**; IQ streaming over TCP/UDP in VITA-49; HTTPS support; Node webpage at the sensor IP; Core default web credentials `admin` / `rf3y3` (not used as this app’s default; the user contract specifies `admin` / `pass`). Full EMP user guide is on the CRFS Extranet (license holders).
- [RFeye Node 100-18 datasheet](https://www.crfs.com/hubfs/Data%20Sheets%202023/2023%20Datasheet%20PDFs/RFeye%20Node%20100-18%20CR-000127-DS-23a.pdf) — 9 kHz–18 GHz; 100 MHz IBW; **1 Hz tuning resolution**; sampling **16 bits per channel (I&Q)**; **125 MS/s I&Q** (full-rate ADC; not proven equal to recorded complex sample rate). External flash via USB. EMP is an optional Node app.
- [RFeye Node comparison](https://www.crfs.com/hardware/rf-sensors/rfeye-node-100-18) — model-dependent frequency range, IBW, and sustained I/Q storage/streaming bandwidths (see table below). Footnote: Node 100-x can record/stream 100 MHz I/Q for a few seconds (not sustained gapless); Node Plus can for hours.
- [RFeye Site 1.49 — WAV-E](https://www.crfs.com/blog/whats-new-in-rfeye-site-1-49) — Node I/Q snippet capture can be recorded as WAV-E in addition to XDAT and HDF5 (Site software). Standard WAV IQ has limited metadata (sample rate, sample count); WAV-E adds time, frequency, bandwidth.
- [DeepView export formats](https://www.crfs.com/software/rfeye-deepview) — VITA-49, RFIQ, XDAT, NCP, SIGMF, CSV, H5, HDF5, SIQ, WAV, EWAV. These are DeepView/Site export names, not proven EMP `recording_format` strings except where they match the Sensorz EMP enum.
- [SIGINT ecosystem export list](https://www.crfs.com/blog/sigint-software-ecosystems-making-applied-signal-intelligence-work) — same family of DeepView exports (RFIQ, XDAT, NCP, SigMF, VITA49, CSV, H5/HDF5, WavE, Enriched Wav).

### Internal (Sensorz) — EMP HTTP contract

- [RF streamer HLD](https://sensorz.atlassian.net/wiki/spaces/~812018014/pages/425852929/RF+streamer+HLD) — CRFS sensor API:
  - `POST http://{sensor_ip}/emp/`
  - HTTP Basic Auth (`username:password` Base64)
  - JSON body for IQ:

```json
{
  "remote_recording_scans": [
    {
      "center_frequency": "float",
      "bandwidth": "float",
      "task_id": "str",
      "duration": "float",
      "rate": "float",
      "capture_length": "float",
      "recording_format": "XDAT | NCP | HDF5 | WAVE"
    }
  ]
}
```

  - Same document’s sweep-scan `rate` is typed `int` and `duration` is typed `int`; IQ `rate` / `duration` / `capture_length` are typed `float`. **Meanings and units of `rate` and `capture_length` are not defined.**
  - Files are saved on storage connected to the sensor; RF Streamer returns a `filename` after the recording finishes (this is the **Streamer** response, not a published EMP schema).
  - CRFS nodes cannot run IQ recording and sweep tasks at the same time (operational observation).
- [PRD — IQ Data Recording](https://sensorz.atlassian.net/wiki/spaces/~812018014/pages/505479433/PRD-+IQ+Data+Recording+Feature) — SWORD UI formats NCP / xdat / HDF5 / WAVE; **SWORD** Phase-1 limits (1 s length, 20 MHz bandwidth) are product limits, not proven hardware EMP limits. Recordings stored on a disk-on-key at the sensor. No stop-once-started in that UI. Phase 1 did not fetch files from the sensor.
- [RF Streamer Extension — Download IQ Recordings](https://sensorz.atlassian.net/wiki/spaces/~812018014/pages/795836417/RF+Streamer+Extension+-+Download+IQ+Recordings) — retrieval is **SFTP over SSH**, not an EMP HTTP download API. Path comes from sensor metadata (`original_path`). No EMP list/status/stop URLs.
- [IQ Service HLD](https://sensorz.atlassian.net/wiki/spaces/~812018014/pages/993853449/IQ+Service+HLD) — SWORD-side recording/file APIs; format enum NCP, XDAT, HDF5, WAVE (plus VITA49 on the file-metadata model). Not the sensor EMP schema.

### Local workspace observation (not an API spec)

One CRFS-named capture in this repo (`IQ Collection/Cellular IQ/iq_rfeye300539_*.wav`):

- RIFF/WAVE, PCM (`wFormatTag` = 1), **2 channels**, **16-bit**, little-endian (WAVE default)
- Sample rate **62 500 000 Hz** (not equal to a 10 or 12.5 MHz RF bandwidth)
- Stereo layout matches this repo’s converter: I left, Q right
- File size ≈ 503 MB for ~2.01 s, consistent with 16-bit I + 16-bit Q (4 bytes per complex sample) plus a small WAVE header

This describes **that file**, not every EMP `WAVE` recording.

## Parameter findings

### `duration`, `rate`, `capture_length`

| Field | Public CRFS docs | Internal EMP mapping | This app |
| --- | --- | --- | --- |
| `duration` | Not defined | Present on IQ and sweep jobs; SWORD UI treats recording length as seconds | Sent as supplied. **Not assumed to be the only time base.** |
| `rate` | Not defined | IQ: float; sweep: int. **Not documented as IQ sample rate.** | Sent as supplied. **Not used as sample rate or capture count.** |
| `capture_length` | Not defined | IQ field only; no unit/relationship | Sent as supplied. |

Unresolved: units (seconds vs other); whether they form a burst schedule; whether `rate` is period, repetition rate, or something else; how many captures a job produces.

**File-size planning** therefore uses a separate GUI-only **recorded time** field. It is not placed in the POST body.

### `recording_format`

Verified EMP identifiers (internal HLD enum): **`WAVE`**, **`XDAT`**, **`NCP`**, **`HDF5`**.

| Identifier | EMP enum (internal) | Notes |
| --- | --- | --- |
| `WAVE` | Yes | User-contract default. Standard WAV IQ has limited metadata (Site 1.49). |
| `XDAT` | Yes | Also a Site snippet / DeepView export format. |
| `NCP` | Yes | CRFS Node Control Protocol / DeepView export name. |
| `HDF5` | Yes | Also Site snippet / DeepView. |
| `BIN` | **No** | Not found in public CRFS or internal EMP enum. Selector includes it as an unverified identifier. |
| `JSON` | **No** | JSON is the EMP **request** encoding and CRFS configuration export language. DeepView lists CSV (and related) among **analysis exports**. JSON is **not confirmed** as an IQ sample recording format. Preset/request JSON export is a separate feature. |
| WAV-E / EWAV / WavE | Not as EMP `recording_format` | Documented for Site snippet capture and DeepView export. Exact EMP string unknown — **not sent**. |
| VITA-49 | Streaming, not this POST | EMP IQ **streaming** over TCP/UDP; DeepView export `*.vrt` / `*.vita49`. |

Changing a file extension is not format conversion.

### Frequency, bandwidth, sample rate

Published **receiver** ranges (do **not** silently clamp GUI values; they vary by model/firmware):

| Model | Frequency range | IBW | Sustained local I/Q | Sustained I/Q stream |
| --- | --- | --- | --- | --- |
| Node 40-8 | 9 kHz–8 GHz | 40 MHz | 20 MHz | 10 MHz |
| Node 100-8 | 9 kHz–8 GHz | 100 MHz | 25 MHz* | 12.5 MHz* |
| Node 100-18 / LW | 9 kHz–18 GHz | 100 MHz | 25 MHz* | 12.5 MHz* |
| Node Plus 100-18 | 9 kHz–18 GHz | 100 MHz | 100 MHz | 100 MHz (550 MBps) |
| Node 100-40 | 9 kHz–40 GHz | 100 MHz | 25 MHz* | 12.5 MHz* |

\*See CRFS footnote on short 100 MHz captures for Node 100-x.

- Node 100-18 datasheet: **1 Hz** tuning resolution.
- Full-rate ADC: **125 MS/s I&Q**, **16-bit I and 16-bit Q**. Decimated recorded rate is **not** specified as equal to RF `bandwidth`.
- User payload uses integer Hz (`800000000`, `12500000`). This app requires integer Hz and refuses silent rounding.

### Sample representation (WAVE, when it is WAVE)

From WAVE spec + the local CRFS WAV + Site 1.49:

- Container: RIFF `WAVE`
- Typical IQ WAV: interleaved little-endian PCM, I then Q (stereo)
- Bit depth in the local file: 16-bit per component (4 bytes/complex sample)
- Headers: standard `fmt ` + `data`; WAV-E/BWF-style extra chunks possible but not confirmed for EMP `WAVE`
- Byte order: WAVE PCM is little-endian

Do **not** assume the same layout for XDAT, NCP, HDF5, BIN, or JSON.

### Storage and retrieval

- Official: I/Q may be stored **on the sensor** or on **external storage**; Tasker can save EMP results on the Node.
- Internal: USB/disk-on-key at the sensor; filename returned to SWORD after the job; download via **SFTP**, not EMP HTTP.
- **No verified EMP HTTP API** in public or internal docs for: listing files, downloading bytes, progress, stopping a recording, or polling completion.
- Firmware mentions EMP GPS/status **without a path**. This app does **not** invent `/emp/status` or similar.
- A successful POST means the HTTP request was accepted (or at least returned a status). It does **not** by itself prove the file is complete on disk.

### Connection test

No documented read-only EMP recording endpoint was found. The Node **web page** is documented at `http://{sensor_ip}/`. Connection test uses TCP reachability plus `GET /` and never sends `remote_recording_scans`.

## File-size estimate

User-supplied empirical reference (preserved as stated in **MB**):

`10 MHz bandwidth × 1 second = 61.051 MB`

`estimated_size_MB = 61.051 × (bandwidth_Hz / 10_000_000) × recorded_time_seconds`

Uncertain whether the original 61.051 figure was decimal MB (10^6 bytes) or MiB (2^20 bytes). The constant is used as given. It is **not** claimed accurate for every `recording_format`.

Optional technical estimate (only if the user enters a complex sample rate):

`payload_bytes = fs_complex × t × bytes_per_complex_sample × channels`

Default technical assumptions if used: 4 bytes/complex sample (16-bit I + 16-bit Q), 1 channel, headers excluded. **RF bandwidth is not used as `fs_complex`.**

## Unresolved questions

1. Exact units and coupling of `duration`, `rate`, and `capture_length`.
2. EMP JSON response schema (filename field names, error objects, sync vs async completion).
3. Whether `GET /emp/` or any status URL is legal; firmware “EMP Status Information” path.
4. Whether `BIN` or `JSON` are accepted `recording_format` values on any firmware.
5. EMP identifier for WAV-E / EWAV if it exists.
6. Recorded complex sample rate vs `bandwidth` for each format/model.
7. Endianness and headers for XDAT, NCP, HDF5.
8. Default HTTP vs HTTPS port and whether EMP Basic Auth shares Node webpage credentials (`pass` vs `rf3y3`).
9. Stop/cancel of an in-flight remote recording.

Until those are answered from CRFS EMP documentation or a live sensor, this application implements the supplied POST exactly and isolates the rest.
