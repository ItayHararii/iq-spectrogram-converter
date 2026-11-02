# Cellular Collection - how it works (developer report)

**Version:** CRFS IQ Recorder 1.9.0 (November 2026)
**Operator guide:** the "Cellular Collection" section of the [Quick Start](confluence/CRFS_IQ_Recorder_Quick_Start.md).
**QA:** `QA_REPORT.md` at the repository root, Part C.

Cellular Collection records every allocation listed in a protocol workbook in a fixed order, optionally at selected times of day, and leaves traceable IQ behind: a manifest beside each capture, rows in the collection workbook, and a handover summary per session. It lives inside the existing Recorder and reuses its recording path, SFTP download, Excel queue, merge engine and worker-thread registry. Nothing in the manual recording flow changed for operators who do not open the panel.

Do not put sensor addresses, logins or passwords in this file.

## What was reused and what was added

| Existing component | Reused for |
| --- | --- |
| `MainWindow` recording path (`_on_start`, EMP stream, file watch, `recording_status`) | Every cellular capture is a normal recording, started through `MainWindow.start_capture(CaptureRequest)` and confirmed by the same file watch that confirms a manual recording. |
| `FileDownloadWorker`, `_open_browser` (SFTP) | Parts are copied with the same `.part` download and the same handshake gate. |
| `ExcelLogStore`, `pending_from_row`, `flush_excel_queue`, `excel_writer` | Cellular rows go through the same persistent queue, backup-before-first-write and "Pending Excel updates" retry. |
| `iq_merge` + `merge_controller` | Optional merge of split parts, originals kept, sidecar written. |
| `sensor_storage.query_iq_storage`, `sensor_info.SensorInfo` | Pre-run checks and the storage gate. |
| `spectrogram_preview` STFT helpers | Sampled previews (beginning, middle, end). |
| `json_store`, `worker_threads.track_worker_thread` | Crash-safe state files; orderly thread stop at exit. |

| New module | Purpose |
| --- | --- |
| `capture_api.py` | `CaptureRequest` / `CaptureSummary` and the outcome vocabulary shared by the GUI and the runner. |
| `cellular_protocol.py` | Workbook import by headers, unit parsing, validation, duplicate/overlap flags, result-row detection, column mapping, versioned snapshot. |
| `cellular_capabilities.py` | Sensor capability model: IBW, conservative capture window, coverage checks, sub-band splitting. |
| `cellular_plan.py` | Ordered task list with windows, durations and storage estimates. |
| `cellular_scheduler.py` | Run-now / times-of-day schedule, explicit time zone, round IDs, due / delayed / missed classification. |
| `cellular_runner.py` | The controller (QObject): task state machine, waits, Pause/Resume/Stop, persistence, recovery, downloads, checksums, quality checks, Excel, manifests, handover. |
| `cellular_manifest.py` | Capture manifest and handover JSON / Markdown. |
| `cellular_excel.py` | Row construction and the `Cellular Results` sheet. |
| `cellular_quality.py` | Pre-run checks, WAV validation, sampled preview, coverage summary. |
| `cellular_panel.py` | The collapsible panel and its dialogs. |
| `cellular_reference.py` | Template workbook writer with reference-chart allocations marked "verify before use". |

## Capture path (completion and recovery first)

```mermaid
sequenceDiagram
  participant R as CellularRunner
  participant W as MainWindow
  participant N as Sensor
  participant F as File watch (SFTP)
  R->>W: set_external_owner("Cellular Collection")
  R->>W: start_capture(CaptureRequest)
  W->>N: POST /emp/ (same payload builder as the button)
  N-->>W: event stream: file names, "finished"
  W->>F: watch the named parts until their sizes are stable
  F-->>W: confirmed / not found
  W-->>R: capture_finished(CaptureSummary)
  R->>R: task CONFIRMED | UNKNOWN | NOT_SENT | SENSOR_ERROR | FAILED
  R->>R: manifest, downloads, wait, next task
```

- `MainWindow.start_capture` returns a reason string when it cannot start (manual recording running, series active, invalid plan, storage gate) and otherwise emits exactly one `capture_finished` for the request, including on every failure path (`_fail_recording`, stream errors, "sensor finished but no file").
- The **external owner** blocks the manual **Start Recording** button and the Repeat series while a session owns the sensor; the runner refuses to start while a manual recording is active. One sequence controls the sensor at a time.
- Outcomes are kept apart: *confirmed* (files found and stable), *unknown* (request reached the sensor, result not proven - never counted as collected, never resent automatically), *not_sent* (nothing reached the sensor - one automatic retry), *sensor_error* (the sensor refused), *failed*. A finished round with uncertain or failed tasks is *partial*, not *done*.
- The **wait** between tasks starts only after the capture is confirmed and finalized (file sizes stable), so the sensor is never asked to record while it is still closing a file.
- **Downloads start from a fresh listing.** On the R40-8 the sensor still touches a recording a few seconds after it has reported *finished* and the sizes have stopped changing: a WAVE part grows by its 648-byte trailer, and an XDAT recording gets its `.xhdr` sidecar late and a longer `.xmrk`. The runner therefore waits `FINALIZE_SETTLE_S` (5 s) after confirmation, lists the folder again on a worker, adds files of the same stem that appeared late (noted in the manifest), records both `size_listed_at_confirmation` and the final `size`, and only then queues the downloads. If the second listing fails, the files are downloaded as listed at confirmation and the manifest says so.
- **Recording format.** The panel's *Recording format* (Advanced) defaults to *Same as recorder*; the format actually sent is stored in the capture summary, the task state and the manifest (`recording.recording_format`, `recording.encoding`). Content checks, duration measurement and previews apply to WAVE; other formats are downloaded and checksummed and the quality level is `not_checked` (not an error). The pre-run checks and the plan preview show the effective format.
- **Persistence:** `cellular/session.json` under the config folder holds the plan, rounds, task states, file states and settings; it is rewritten atomically after every change. On start the runner loads it and, if the previous process died mid-session, shows the session as *interrupted* with the uncertain tasks listed. **Reconcile with sensor** lists the dated folders over SFTP and confirms a task only from files the recording history can attribute to it; nothing is resent until the operator chooses **Resume session**, **Skip uncertain** or a retry. A reconciliation whose SFTP listing fails reports the failure and changes nothing. **Resume session** also re-queues downloads that were pending or running when the process died.

## Import and sequence

- Headers are matched by role (band, nominal, duplex, direction, start, stop, centre, bandwidth, duration, repeat, notes, result columns). Units come from the cell (`758 MHz`), the header (`Start (MHz)`) or a default chosen in the mapping dialog, in that order. Two layouts are supported: one row per direction, and one row per band with paired DL/UL columns.
- Validation refuses reversed, missing or contradictory edges, unknown directions and out-of-range frequencies; every refused row is listed with its row number and the plan stays disabled. Duplicates are flagged and collapsed; overlaps are flagged. Rows carrying results (filename, snapshot id, playback time) are reported and never imported as instructions.
- The snapshot of the imported protocol (`cellular/protocols/<hash>.json`) is what a session runs; editing the workbook afterwards changes nothing until **Reload**.
- Ordering: bands ascending by nominal frequency; within an FDD band every DL window, then every UL window; TDD bands once as "TDD / shared UL-DL". The reference facts used for tests (nominal order, FDD/TDD per band) are in `cellular_reference.py`; the workbook, not the reference, decides the allocations.
- Wide allocations are split into consecutive windows of the capture width (20 MHz default, even widths, 1 Hz widening of odd edges reported). Each window is a separate task with its own manifest, Excel row and files; the manifest's coverage reads `window 2 of 4 (sequential coverage, partial capture)` next to the whole-allocation extent, so partial captures are identifiable. Nothing is cropped, retuned or resampled silently.
- Sensor capability is a model, not a measurement: `cellular_capabilities.py` states the IBW from the model string and keeps "sustained gapless" apart from "instantaneous"; the panel's capture-window note says when no gapless figure has been verified for the model.

## Scheduling

Schedules are stored as times of day plus dates (or daily) with an explicit zone (`local` or `UTC`); occurrences are materialised in UTC. A round that becomes due while the sensor is busy is *delayed* and keeps its scheduled time in its ID and manifests. A round whose window passed while the app was not running is *missed*; it is listed and only runs when the operator presses **Run missed round now**. `tight_schedule_warning` compares the estimated round length against the spacing of the schedule before the session starts.

## Background work

All waiting, downloading, hashing, checking and rendering runs off the UI thread: `QThread` workers tracked by `worker_threads`, signals delivered with queued connections, bounded by `MAX_DOWNLOADS` (2) parallel downloads, a backlog limit that pauses new captures (with the reason shown), a local-disk reserve, and one quality check at a time (previews are drawn with matplotlib's object-oriented Agg API under a lock; pyplot is not thread-safe). The runner's timer tick (`TICK_MS`, 0.5 s) drives waits and schedule checks; nothing blocks.

The panel itself is cheap while closed: only the header row (toggle and status chip) exists until the operator opens it, or until a saved, interrupted or finished session is found at start, when `_ensure_built` creates the body. The main window therefore keeps the same widget count as 1.8.10 for operators who never use the feature.

## Metadata, Excel, quality

- Manifest `<stem>.cellular.json` (schema 1) beside the parts: identifiers (campaign, site, visit, round, task, protocol version, record id), the allocation (workbook values and the actual window), timing (scheduled, started, finished, requested vs held duration), sensor identity and GNSS/time status (`unknown` when not provided), file list with sizes and SHA-256, quality flags, Excel status, notes. The EMP `rate` field is never reported as a sample rate; the model sample rate is labelled `sample_rate_hz_model`.
- Excel: the matching class worksheet when the catalog has one, otherwise `Cellular Results` (created once, after the usual backup). Split parts of one capture are one row; separate windows are separate rows; a locked workbook defers the row and the tick retries.
- Quality: pre-run checks (connection, identity, clocks, GNSS when known, sensor and local storage, plan, test capture) are shown with the plan before Start. After download, `validate_capture` checks RIFF headers, sample format, part numbering and time continuity (`meta` chunk), tuning and bandwidth against the plan, held duration, and samples at the beginning, middle and end (clipping, zero runs, DC, level jumps). Previews are three small waterfalls from those blocks and say "Sampled preview (not the whole file)". Original IQ is never modified.
- Handover: `cellular_manifests/handover_<campaign>_<time>.json` and `.md` list every capture with its files, sizes, checksums, quality level and Excel status, and count logical captures, files, derived images and merged outputs separately; captures in a non-WAVE format are listed under *Content not inspected*, not as quality warnings. The handover is written when the session ends and again when the last background download or check finishes. Nothing is uploaded anywhere; originals stay until the transfer is verified by whoever receives them.

## Tests

`tests/test_cellular_*.py` (62 tests) cover: both workbook layouts, units, duplicates and result rows; ordering, TDD, sub-band splitting; time zones, delayed and missed rounds; waits after confirmation, Pause/Resume/Stop in every state; interrupted sessions, reconciliation from sensor files, no blind resend, pending downloads re-queued after a restart; backlog and low local storage pauses; locked workbook deferral; connection loss as *unknown*; midnight folder change; the fresh listing before downloads (late sidecar files, grown sizes, listing failure); manifests and handover counts, including the recording format and *not checked* captures; WAV validation and previews, non-WAVE files reported as not inspected rather than corrupt; the panel end-to-end on the demo transport, including the manual Start lock and the format choice; the collapsed panel building nothing until opened and opening itself for a saved session. Hardware results are in the QA report.
