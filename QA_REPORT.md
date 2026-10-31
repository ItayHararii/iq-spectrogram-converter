# QA report - CRFS IQ Recorder 1.8.9 and IQ Spectrogram Converter 1.5.1

Two passes. Part 1 (31 October 2026) ran against a real RFeye Node and produced Recorder 1.8.9 and Converter 1.5.1. Part 2 (30 October 2026) is the earlier simulation-only pass that produced 1.8.8 / 1.5.0; it is kept unchanged below except where a hardware result supersedes a statement.

Releases: Recorder 1.8.9 (tag `v1.8.9`), Converter 1.5.1 (tag `converter-v1.5.1`). Previous: 1.8.8 (`v1.8.8`), 1.5.0 (`converter-v1.5.0`), 1.8.7 (`v1.8.7`), 1.4.0 (`converter-v1.4.0`). All earlier releases stay on GitHub.

Legend used throughout: **Hardware** = executed against the real sensor in this pass; **Simulated** = executed against the demo transport, a local proxy or synthetic files; **Untested** = not executed.

---

# Part 1 - Hardware pass (31 October 2026)

## Sensor and software under test

| Item | Value |
| --- | --- |
| Sensor | CRFS RFeye Node, model **R40-8**, serial **rfeye300540**, firmware **2.25-325** (CRFS.MissionProcessor 1.11.1.169, EMP protocol 0.10), Alpine Linux 5.15.173, time zone UTC. Address is the one saved in the application settings (not reproduced here). |
| Sensor clock | 0.25 s ahead of the PC (SSH `date +%s.%N` against the PC clock, 500 ms round trip). No GPS fix during the whole session: every recording carried the warning "No GPS reference (0 satellites, jamming indicator 21 %), using internal clock". |
| Sensor storage | `/mnt/1/remdata/` on `/dev/sdb1`: 944.9 GB total, 880.6 GB free at the start (the app shows these as "820.1 GB free / 880 GB", see observation O1). 55 dated folders; today's `20261031/` existed with 5 operator files (2 recordings, 1.77 GB). |
| HTTP / API | TCP 80 reachable; `/` 200; `/api/node.json` 200 (model, firmware, serial); `/api/values/versions.json`, `/status.json`, `/gps.json`, `/network.json` 200; `/emp/` 200 (status page, SSE endpoint for recording). `/api/values/time.json`, `storage.json`, `disk.json`, `system.json` do not exist (404). |
| SFTP | Password login 2.2 s; listing of `/mnt/1/remdata/` 0.6 s; `statvfs` not offered by the server, free space read through the existing `df -kP` path. The Node accepts **2-4 simultaneous SSH key exchanges**; a 5th handshake times out after 8 s (see H10). |
| Recording API contract (observed) | `POST /emp/` with `remote_recording_scans[{center_frequency, bandwidth, task_id, duration, rate, capture_length, recording_format}]` answers **HTTP 200 `text/event-stream`**: `message.MessageData` (level + message), `time.RemoteTimeData` per WAVE part (`file_dir`, `first_filename`, `task_id`), INFO "Remote recording is now finished", then `: keepalive` every 10 s **for ever** (held 333 s in a raw probe; the Node never closes it). The Node kills the mission the moment the HTTP client disconnects. A second task while one is running pre-empts the first ("The node is currently locked due to a task"). A centre frequency with no antenna answers 200 + WARNING "No suitable antenna found" + finished, and writes no file. `duration: 0` is accepted and records until the client disconnects (618 MB in 40 s at 2 MHz). |
| WAVE layout (observed) | 44-byte RIFF header, 16-bit stereo PCM (I left, Q right), a 648-byte trailing chunk after `data`, parts split at 523,776,000 payload bytes. Complex sample rate = 125 MS/s / 2^k, the smallest value >= 1.25 x bandwidth, capped at 62.5 MS/s: 1 MHz -> 1.953125, 2 MHz -> 3.90625, 5-6 MHz -> 7.8125, 8-12.5 MHz -> 15.625, 20 MHz -> 31.25, 30-60 MHz -> 62.5 MS/s. |
| Recorder | Source at the state that became 1.8.9 (Python 3.13.14, PySide6 6.11.2, paramiko 5.0.0) driven by an in-process Qt harness (`app_drive.py`, outside the repository) that clicks the real widgets and samples UI-thread stalls every 50 ms; the released **1.8.8 EXE** and the new **1.8.9 EXE** driven on the desktop with their own isolated `%LOCALAPPDATA%`. Demo mode off for every hardware case. |
| Converter | Source at the state that became 1.5.1 and the released **1.5.0 EXE** / new **1.5.1 EXE**, headless (`--input --output --rbw --log`) and in-process on the files recorded in this pass. |
| Workbook | The operator's collection workbook (3 sheets `TETRA` hidden, `TETRA)` visible, `P25` visible; VBA; data validations) copied to `%TEMP%` before the pass. **The original was never opened for writing.** |
| PC | Windows 11 26H2, 3840 x 2160 at 150 %, Smart App Control enforced, Defender real-time protection and Controlled folder access on. Cursor, a browser and Office were running. |

## Hardware summary

| Area | Result |
| --- | --- |
| Connectivity | Hardware. HTTP/API and SFTP verified independently; identity, clock, storage read. |
| Recordings | Hardware. 15 raw-API captures (1-60 MHz bandwidth, 2-120 s, 1-15 parts, 15.6 MB-7.5 GB) and 24 captures through the application (Start/End and Center/Bandwidth; series of 10; stop paths; loss paths; Excel paths; released 1.8.8 EXE; new 1.8.9 EXE). Every file was checked against the request (sample rate, frames, duration, bytes, encoding). |
| Completion and split files | Hardware. Multi-part captures of 3, 5, 7, 10 and 15 parts; the sensor's own completion message is now used; the stable-size rule is only the fallback. |
| Consecutive recordings | Hardware. 10 x 2 s at zero wait: 10 POSTs, 10 distinct task ids, 10 files, 10 Excel rows, max UI stall 81 ms. |
| Connection loss | Simulated through a local TCP proxy in front of the real sensor (connection dropped after the POST; request accepted but never answered; port refused). |
| Storage and estimate | Hardware. Displayed free space matches `df` within rounding; the free-space delta per recording is 1.001 x file bytes. The 1.8.8 estimate was 17-46 % off whenever the bandwidth was not 5, 10, 20 or 40 MHz; the 1.8.9 model is within 0.6 % on every sustained capture. |
| Excel | Hardware recordings logged into the workbook **copy** with Excel open in edit mode, with the app exiting while rows were pending, and after restart. Split parts share one row; `Playback Start` is `N seconds`. One requirement mismatch found and fixed (H12, hidden sheet) and one spelling mismatch (H16). |
| Converter and preview | Hardware files. 6 new recordings (3.9, 15.6 and 62.5 MS/s; 2.0-8.4 s; 33-524 MB) through the engine, the 1.5.0 EXE and the 1.5.1 EXE; axes, durations, RBW, memory, cancel verified. No calibrated RF reference was available (see "Not verified"). |
| Defects | 16 Recorder defects (H1-H16: 5 high, 8 medium, 3 low) and 2 Converter defects (C5-C6) fixed with 36 new regression tests. Recorder suite 252 passed / 1 skipped; Converter suite 33 passed. |
| Releases | Recorder 1.8.9 (portable EXE + Setup) and Converter 1.5.1 built, retested on the sensor / on the recorded files, released. Still unsigned. |
| Cleanup | 99 QA files (30.46 GB) deleted from the sensor through the application's own Delete (with the new confirmation) plus the 1.8.9 test file; the operator's 5 files (`file_173514_*`, `file_173625_*`) and all older folders untouched; nothing deleted that this session did not create. |

## Defects found on hardware and fixed

Severity: High = wrong data, lost data, blocked workflow or a leaked connection that affects the sensor; Medium = wrong behaviour in a reachable path; Low = cosmetic or latent.

| # | Sev | Defect (what the hardware showed) | Fix | Regression tests |
| --- | --- | --- | --- | --- |
| H1 | High | The recording POST answers with an endless event stream (keepalive every 10 s, never closed by the Node). 1.8.8 read the body with `response.text` under a 30 s per-read timeout, so the call could not return: every recording left a blocked worker thread and an open HTTP connection, the Node kept the task alive for as long as that connection existed, and the reply content (file names, warnings, completion) was never used. The 1.8.8 EXE still produced files because the file watch runs independently of the HTTP worker. | `api_client`: streaming request path (`_request_streaming`, `parse_sse_blocks`, `StreamOptions`, `SseEvent`), `start_recording(cancel=, on_event=, stop_on_finished=)`, `abort()` that shuts the socket down so a blocked read returns at once (closing the response from another thread stalled the UI for over a second on Windows). `gui`: events are delivered to the UI thread (`record_event`), the stream is held until the files are confirmed and then closed, so the Node releases the task. | `tests/test_emp_stream.py` (10): split chunks and comments, finished stops the read, ERROR level reported as sensor error, time budget, cancel and abort unblock, server drop reported as lost, plain JSON reply still works, budget scales with duration, GUI mode holds the stream after finished. `test_gui_follows_the_sensor_stream_and_pins_the_reported_filename`. |
| H2 | High | The sensor names every part it opens (`time.RemoteTimeData.first_filename`); 1.8.8 ignored this and associated files with recordings by timestamp windows only, which is what allowed H8 and H14. | The reported stem is pinned to the recording as soon as it arrives (`recording_history.set_recording_stem`); the live watch uses the reported names when it has them (`live_capture_files`). | `test_live_watch_uses_the_names_the_sensor_reported`, `test_persist_stem_then_match_later_parts` (existing). |
| H3 | High | Completion was inferred from file sizes alone. On hardware the last part of a 62.5 MS/s capture reaches the disk up to 20 s after the sensor's completion message, and parts arrive 8-9 s apart at 15.6 MS/s, so a quiet listing is not proof of completion and a growing file could be declared final. | `file_watch.recording_complete` now takes `sensor_finished`, `stream_pending` and `expected_bytes`: nothing completes while the stream is open and the sensor has not said finished; growing files never complete (only a stall of `max_watch_s` ends the wait); after "finished", stable parts holding >= 98 % of the expected bytes complete at once, otherwise after the settle time so a late part is still picked up. | `test_growing_files_are_never_complete_even_after_the_watch_limit`, `test_open_stream_without_finished_holds_completion_and_sensor_finished_plus_size_releases_it`, `test_no_files_gives_up_after_the_watch_limit_only`. Hardware: 3-part (`file_190307_0001-0003`, 1,252.9 MB), 7-part, 10-part and 15-part captures all confirmed with the right part count; no premature completion in 24 application captures. |
| H4 | High | **The Node cannot sustain 62.5 MS/s to disk.** 40 MHz x 20 s produced 7 parts holding 14.15 s (-29 %); 40 MHz x 40 s produced 2 parts holding 3.26 s and 3.29 s (-92 %, twice); 40 MHz x 10 s was complete (10.006 s). 31.25 MS/s (20 MHz x 40 s, 10 parts, 5.0 GB) and 15.625 MS/s (10 MHz x 120 s, 15 parts, 7.5 GB) were complete to 0.06 %. The sensor reports "finished" normally in the short cases, so 1.8.8 would have logged the requested 20 s or 40 s in Excel. This is a sensor limitation; the application now measures and reports it. | After completion the application computes the seconds actually held from the combined part sizes (`size_estimate.recorded_seconds_from_bytes`), logs "The files hold about 14.1 s of IQ, not the 20 s requested", shows "Recording finished short: 14.1 s of 20 s" and writes the **measured** time to Excel. The message distinguishes a dropped connection from an early sensor stop. | `test_gui_logs_the_measured_duration_when_the_capture_is_short`, `test_recorded_seconds_from_bytes_recovers_capture_time`. Hardware: `app_s7` (40 MHz x 20 s -> "14.1 s of 20 s"), `app_l1` (connection dropped -> "0.8 s of 3 s"). |
| H5 | Medium | The size estimate (linear 61.051 MB per 10 MHz x 1 s) ignored the sensor's power-of-two sample-rate steps: -27 % at 2 MHz and 30 MHz, -22 % at 1 and 8 MHz, +21 % at 12.5 MHz, +46 % at 60 MHz, +17 % at 6 MHz; right only at 5, 10, 20, 40 MHz. The 1.8.8 EXE showed 24.42 MB for a 33.52 MB recording. | `size_estimate.wave_size_model`: rate from the measured step rule, 4 bytes per complex sample, +0.6 % samples, 692 bytes header and trailer per part, parts at 523,776,000 payload bytes. Used by the GUI estimate and by the storage check. The legacy formula stays as `empirical_size_mb` for comparison. Note: a size model was introduced in 1.8.4 (September) and withdrawn at the user's request; this one is different (measured on this sensor) and is flagged here deliberately. | `test_estimated_bytes_follow_the_sample_rate_model`, `test_sample_rate_model_matches_hardware_within_one_percent` (13 hardware points), `test_legacy_linear_formula_was_far_off_where_the_rate_steps`, `test_sample_rate_is_capped_and_steps_by_powers_of_two`. |
| H6 | Medium | `duration: 0` was accepted by the GUI and by the sensor, which then recorded until the client disconnected (618 MB in 40 s at 2 MHz). | `request_builder` refuses a recording time <= 0 before the POST. | `test_zero_or_negative_recording_time_is_refused_before_the_post`. Hardware: raw-API `api_edge_zero` documents the sensor behaviour. |
| H7 | Medium | A centre frequency with no antenna (for example far outside the receiver range) is accepted (HTTP 200), the sensor says "No suitable antenna found" and "finished", and writes nothing. 1.8.8 kept "Waiting for files" for the full 60 s watch limit and could adopt an unrelated file that appeared in the window. | Sensor WARNING/INFO messages are shown in the log; when the sensor reports finished without naming a file, the application gives up after `NO_FILE_GRACE_S` = 10 s with the sensor's own reason and never adopts a neighbouring file. | `test_gui_gives_up_quickly_when_the_sensor_finishes_without_a_file`. Hardware: raw-API `api_edge_range`. |
| H8 | Medium | In a zero-wait series the live watch could adopt a file that belonged to the previous recording or that predated the request (the sensor's file names are UTC, mtime is local, and the two recordings are seconds apart). | `live_capture_files`: with sensor-reported names only those count; otherwise a file already owned by another recording, or whose exact time candidates all predate the request (60 s skew allowed for the sensor clock), is never adopted. | `test_live_watch_does_not_adopt_a_file_owned_by_an_earlier_recording`, `test_live_watch_tolerates_a_sensor_clock_slightly_behind`. Hardware: `app_s4` 10 x 2 s at zero wait, 10 distinct files, each on its own record. |
| H9 | Medium | A refused or unreachable port was reported as `HTTPConnectionPool(...): Max retries exceeded with url: /emp/ (Caused by NewConnectionError(... [WinError 10061] ...))` although the client never retries. | `connection_failure_reason` maps the socket error to one sentence ("the sensor refused the connection (nothing is listening on that port)", "the connection attempt timed out", ...). | `test_connection_error_text_names_the_cause_not_the_retry_wrapper`. Hardware: `app_l3` (proxy port closed) produced the old text; the fix came after that run and is unit-tested. |
| H10 | Medium | Parallel downloads of 3 parts failed with "SFTP connection failed: timed out": the Node accepts only 2-4 simultaneous SSH key exchanges (2.4 s each); the 5th waits for a slot that never comes within 8 s. The file watch also opened a new SSH session on every poll. | `sftp_client`: process-wide `BoundedSemaphore(2)` around connect + auth (`SSH_HANDSHAKE_SLOTS`), cancellable while waiting. `sftp_window`: one retry after 3 s when a download's connect times out. `gui`: the watch keeps its SFTP session across polls. | `tests/test_sftp_handshake_gate.py` (2). Hardware: 3 parallel downloads of the 3-part recording completed (3.7 MB/s each, UI stall <= 111 ms). |
| H11 | Low | At start-up two storage reads ran back to back; the second cancelled the first and paramiko printed "Error reading SSH protocol banner [WinError 10038]" with a traceback on stderr. | Storage reads are coalesced (a read in flight for the same sensor marks a rerun instead of cancelling); the `paramiko` logger gets a `NullHandler`. | `test_storage_reads_are_coalesced_not_cancelled`. Hardware: no traceback at start-up afterwards. |
| H12 | High | **Excel rows went to the hidden `TETRA` sheet.** The worksheet selector defaulted to the first sheet by name; in the operator's workbook that sheet is hidden and the live sheet is `TETRA)`. Confirmed on the released 1.8.8 EXE (Worksheet showed `TETRA`, the row was written there). | `collection_catalog` skips sheets whose `sheet_state` is not `visible`. The 1.8.9 EXE defaults to `TETRA)`. | `test_hidden_sheets_are_not_log_targets`. Hardware: 1.8.9 EXE wrote `TETRA)` row 27. |
| H13 | Low | New rows were set to 16 pt per line + 4, shorter than the sheet's own 20-22 pt rows, so new entries looked squeezed. | Row height is never set below the sheet's template row height (openpyxl and COM paths). | `test_new_row_keeps_the_sheets_row_height`. Hardware: new rows 20.05-22 pt like their neighbours. |
| H14 | Medium | After deleting a recording's file from the sensor, its saved parameters were re-assigned to another recording's file through the "+4 h" time-zone guess, so Sensor Files showed the wrong band next to an operator file. | Pinned records (those with a stem) never re-adopt by time; hour-offset guesses must agree with the file's mtime (name time within -60 min / +5 min of mtime). | `test_pinned_recording_is_not_reassigned_when_its_file_is_deleted`, `test_hour_offset_guesses_must_agree_with_the_files_mtime`. Hardware: 1.8.9 EXE, after deleting `file_214214_0001.wav` the operator's files still showed no metadata. |
| H15 | Medium | The Delete key removed sensor files immediately. The 30 Oct report left this as "existing, intentional"; on real hardware it deletes irrecoverable recordings with one key press. | Confirmation dialog listing up to 6 names ("Delete N file(s) from the sensor? This cannot be undone."), No is the default. | `test_sftp_delete_removes_selected_files_only` and `test_sftp_delete_parent_removes_all_listed_parts` now cover both answers. Hardware: 1.8.9 EXE, No kept the file, Yes removed it; bulk cleanup of 99 files went through the same dialog. |
| H16 | Low | The Sensor column was written as `40-8` while the operator's rows use `CRFS-R408` for the same device. | `sensor_label.workbook_sensor_name` reuses the workbook's spelling when an existing name ends with the same digits/letters. | `test_sensor_label_reuses_the_workbooks_spelling_for_the_same_device`. Hardware: 1.8.9 EXE row shows `CRFS-R408`. |
| C5 | Low | Converter: the peak marker label was drawn 15 px above the spectrum axes, over the title. | Label placed inside the axes (`xytext` in axes fraction, `va="top"`). | `test_peak_label_stays_inside_the_spectrum_axes`. |
| C6 | Medium | Converter: every split part was plotted with a time axis starting at 0 s, so part 2 of a 20 s capture looked like its first 8.4 s. | `iq_data.part_time_offset` sums the durations of the earlier parts found next to the file; the time axis and title say "part 2 from 8.38 s"; when the earlier parts are missing the axis starts at 0 and the title says so. | `test_split_parts_continue_the_recordings_time_axis`, `test_split_part_without_its_earlier_parts_keeps_time_from_zero`. Hardware files: `file_190307_0002.wav` renders 8.38-16.76 s in the 1.5.1 source and EXE (identical PNG bytes). |

## Recording results (expected versus observed)

Raw API captures (`rec_sse.py`, which speaks the same EMP contract as the application and records every SSE event, every listing and the free space before and after). Centre 800 MHz unless stated. "Held" is the duration computed from the WAVE frame count; "1.8.8 est." is the released estimate, "1.8.9 est." the new model.

| Request | Rate (MS/s) | Parts | Bytes | Held (s) | Held vs requested | 1.8.9 est. vs actual | 1.8.8 est. vs actual | Time to first file / to "finished" |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 433 MHz, 1 MHz, 2 s | 1.953125 | 1 | 15,625,784 | 2.0001 | +0.005 % | +0.6 % | -21.9 % | 2.9 s / 5.1 s |
| 5 MHz, 2 s | 7.8125 | 1 | 62,853,684 | 2.0113 | +0.57 % | +0.04 % | -2.9 % | 3.0 s / 5.8 s |
| 156-162 MHz (start/end), 2 s | 7.8125 | 1 | 62,853,684 | 2.0113 | +0.57 % | +0.04 % | +16.6 % | 3.3 s / 5.6 s |
| 8 MHz, 2 s | 15.625 | 1 | 125,706,804 | 2.0113 | +0.57 % | +0.03 % | -22.3 % | 2.9 s / 5.3 s |
| 12.5 MHz, 2 s | 15.625 | 1 | 125,706,804 | 2.0113 | +0.57 % | +0.03 % | +21.4 % | 3.0 s / 5.6 s |
| 10 MHz, 120 s | 15.625 | 15 | 7,500,482,572 | 120.0077 | +0.006 % | +0.59 % | -2.3 % | 3.0 s / 123.3 s |
| 20 MHz, 10 s | 31.25 | 3 | 1,252,874,128 | 10.0230 | +0.23 % | +0.37 % | -2.5 % | 2.9 s / 14.0 s |
| 20 MHz, 40 s | 31.25 | 10 | 5,003,115,104 | 40.0249 | +0.06 % | +0.54 % | -2.4 % | 2.9 s / 43.4 s |
| 30 MHz, 2 s | 62.5 | 1 | 502,825,520 | 2.0113 | +0.57 % | +0.03 % | -27.2 % | 2.9 s / 5.2 s |
| 40 MHz, 2 s | 62.5 | 1 | 502,825,520 | 2.0113 | +0.57 % | +0.03 % | -2.9 % | 1.5 s / 6.0 s |
| 60 MHz, 2 s | 62.5 | 1 | 502,825,520 | 2.0113 | +0.57 % | +0.03 % | +45.7 % | 3.0 s / 5.1 s |
| 40 MHz, 10 s | 62.5 | 5 | 2,501,557,488 | 10.0062 | +0.06 % | +0.54 % | -2.4 % | 2.9 s / 13.2 s |
| **40 MHz, 20 s** | 62.5 | 7 | 3,536,540,240 | **14.146** | **-29.3 %** | n/a (short) | n/a | 2.9 s / 23.8 s |
| **40 MHz, 40 s** | 62.5 | 2 | 816,157,920 | **3.265** | **-91.8 %** | n/a (short) | n/a | 2.9 s / 44.0 s |
| **40 MHz, 40 s** (repeat) | 62.5 | 2 | 823,159,264 | **3.293** | **-91.8 %** | n/a (short) | n/a | 2.9 s / 43.2 s |

Every file: RIFF/WAVE, `fmt` PCM tag 1, 2 channels, 16 bit, sample rate as in the table, `data` chunk followed by a 648-byte trailing chunk (the header's `data` size therefore does not equal file size - 44; this is the sensor's format, not corruption). The free space reported by the sensor dropped by 1.001 x the file bytes after each capture (filesystem block overhead).

Duration overshoot: the sensor delivers about 0.57 % more samples than requested at 7.8-62.5 MS/s, 0.005 % at 1.95 MS/s, and **7.3 % at 3.906 MS/s** (2 MHz bandwidth: 2.1454 s for 2 s, 8,380,384 frames, 33,522,232 bytes, seen in all 20 captures at that rate). The model uses 0.6 %, so the 2 MHz estimate reads 31.44 MB for a 33.52 MB file (-6.2 %, was -27 % in 1.8.8). Not tuned further on two bandwidths' worth of evidence.

Application captures (harness on the source, isolated settings, demo off):

| Scenario | Parameters | Observed | UI stall (max) |
| --- | --- | --- | --- |
| S1 Center/Bandwidth | 820 MHz, 10 MHz, 2 s | `file_211418_0001.wav`, 125,706,804 B, 15.625 MS/s, 2.0113 s; accepted 0.5 s after Start, file named at 0.9 s, sensor finished at 3.6 s, confirmed and logged at 13.0 s; Excel row on `P25` with `CRFS-R408` | 39 ms |
| S2 Start/End | 865-870 MHz, 3 s | `file_190801_0001.wav`, 96,375,352 B, 7.8125 MS/s, 3.084 s; one row | 1,369 ms once (first openpyxl load of the workbook copy; subsequent writes 39-90 ms) |
| S3 Split | 820 MHz, 10 MHz, 20 s | 3 parts `file_190307_0001-0003`, 1,252.9 MB, announced at 0.9 s, 9.1 s and 17.7 s after Start; "finished" at 22.4 s; all parts stable at 30.6 s; one Excel row (`P25` r28, 52 pt) with the three names and `20 seconds` | 1,838 ms once (same cause) |
| S4 Series | 411 MHz, 2 MHz, 2 s, count 10, wait 0 | 10 POSTs with 10 distinct task ids (`iq-20261031-211034` ... one per second of start time), 10 files, 10 rows, "Recordings completed: 10 · Files found: 10", 13.0-13.1 s per cycle, 139.7 s total, no errors | 81 ms |
| S5 Stop after current | 3 s, count 4, wait 8 s | Stop pressed during recording 2; recording 2 completed (50.3 MB), "Series stopped after the current recording", no third POST | 55 ms |
| S6 Stop during countdown | 2 s, count 3, wait 20 s | Recording 1 completed, Stop pressed in the countdown, "Stopped. The next recording was cancelled.", 1 file on the sensor, no second POST | 68 ms |
| S7 High rate truncation | 800 MHz, 40 MHz, 20 s | 7 parts, 3,524.0 MB, "The files hold about 14.1 s of IQ, not the 20 s requested" (H4) | 90 ms |
| S8 Until stopped | 2 s, wait 3 s | 3 recordings then Stop in the countdown; 3 files, 3 rows | 46 ms |
| S9 Explicit worksheet | 421 MHz, 2 MHz, 2 s, sheet chosen by hand | Row on the chosen sheet | 1,187 ms once |
| E1 Excel open in edit mode | 2 x 2 s | Rows queued ("Pending Excel updates: 2"), drained 5.3 s after Excel left edit mode; 2 new rows, `2 seconds`, 20.1-20.2 pt; workbook saved by the app, VBA project and other sheets intact | 54 ms |
| E2 Exit with a pending row | 1 x 2 s, Excel in edit mode, app closed | Pending row written on the next start ("Excel: saved 1 row(s)"); two further restarts added nothing (dedupe) | 61 ms |
| L1 Connection dropped after the POST (proxy) | 3 s, count 2, wait 2 s | "Connection to the sensor dropped while waiting for its completion message"; the Node killed the mission on disconnect, the file holds 0.8 s; the app reported "0.8 s of 3 s", logged the measured time, did not retry, continued the series; 2 POSTs for 2 recordings | 46 ms |
| L2 Request accepted, never answered (proxy) | 2 s | "Request sent; no reply in time. Check Sensor Files before starting again."; phase UNKNOWN after 66 s; no second POST; no file (the sensor never received the request) | 72 ms |
| L3 Port refused (proxy down) | 2 s, count 2 | Immediate error, series ended, Start re-enabled, 0 POSTs reached the sensor | 42 ms |
| X1 Released 1.8.8 EXE | 410-412 MHz, 2 s | `file_205734_0001.wav` 33,522,232 B; estimate shown 24.42 MB (H5); Worksheet defaulted to hidden `TETRA` and the row went there (H12); Sensor `40-8` (H16); preview, download and keyboard delete without confirmation (H15) worked; after the delete the metadata jumped to `file_185751_0001.wav` (H14) | - |
| X2 New 1.8.9 EXE | 410-412 MHz, 2 s | v1.8.9 shown; connected (R40-8 / 2.25-325 / rfeye300540 / 820.1 GB free); estimate 31.44 MB; Worksheet defaulted to `TETRA)`; `file_214214_0001.wav` named 2 s after Start, "finished" at 4 s, confirmed and logged at 7 s (33,522,232 B, 3.90625 MS/s, 2.1454 s after download); Sensor Files listed it NEW with 410 / 412 / 411 / 2 / 2; preview "First 0.034 s · 411 MHz"; download 32 MB; Delete asked for confirmation, No kept the file, Yes removed it; metadata stayed pinned; Excel row `TETRA)` r27 with `CRFS-R408`, `2 seconds`, 22 pt; closed from the title bar | - |

Task-id resolution: `unique_task_id()` is `iq-YYYYMMDD-HHMMSS`. Two Start presses within one second are prevented by the state machine, and a series cannot start the next recording before the previous one is confirmed (13 s at the fastest), so the one-second resolution was not reached in 10 zero-wait recordings. Left unchanged; noted again as a latent limit.

## Excel requirement check

Latest agreed requirement (Quick Start 1.7.0+, Confluence): one row per recording; split parts on the same row, one filename per line in `IQ Recording`; `Playback Start` as plain text `N seconds`; Sensor named as the workbook names it. Observed on the workbook copy: S3 wrote one row with three names and `20 seconds`; S1/S2/S4 wrote one row each; E1/E2 preserved the queue; row heights follow the sheet. Mismatches found and fixed: hidden-sheet default (H12) and Sensor spelling (H16). Remaining difference, not changed: the operator's older rows hold clock times in `Playback Start` (for example `12:30:00`), the agreed format for new rows is `N seconds`; the two coexist in the same column.

Workbook integrity: the pristine copy's hidden `TETRA` sheet XML is 23 MB because it contains 1,048,477 empty `<row>` elements with no cells and no attributes (only 67 rows carry attributes); openpyxl drops those on the first write (sheet XML 25.5 KB). All cell values, styles, data validations, the other sheets and `vbaProject.bin` are byte-identical after the round trip. This is not data loss; it was the cause of the 1.8.7 freeze (R1 in Part 2).

## Converter and preview on the recorded files

Files: `file_190218_0001` (820 MHz, 10 MHz, 2 s), `file_190307_0001/0002/0003` (3-part 20 s), `file_200740_0001` (411 MHz, 2 MHz), `file_191700_0007` (last part of a 40 MHz capture, 62.5 MS/s). Engine in-process with the `stats` hook, 1.5.0 EXE headless, 1.5.1 source and EXE.

| Check | Result |
| --- | --- |
| Frequency axis | Baseband -fs/2 ... +fs/2 for every file (for example -7.8125 to +7.8087 MHz at 15.625 MS/s); the centre frequency is not in the WAVE file and is shown as 0 Hz, as documented |
| Duration | 2.0113 s / 8.3804 s / 2.1454 s / 1.5252 s from the frame counts, equal to the Recorder's measured values |
| RBW | Request is an upper bound: 15 kHz -> FFT 4096 at 15.625 MS/s, true ENBW RBW 7.646 kHz (2.004 bins x 3.815 kHz); 1 kHz -> 955.75 Hz. Printed in the log and the PNG |
| Split parts | 1.5.0 plotted every part from 0 s (C6). 1.5.1: part 2 plotted 8.38-16.76 s, title "part 2 from 8.38 s"; without the sibling parts the axis starts at 0 and the title says so |
| Images | Source and EXE produce byte-identical PNGs (1.5.0: 6 files; 1.5.1: `file_190307_0002.png` 862,543 B, SHA-256 `FDFE30E5...`). Peak label inside the axes (C5) |
| Memory | 1.5.0 EXE on the 524 MB part: worker peak working set 443 MB, bootloader 9 MB, 25.4 s wall; 1.5.1 EXE 24 s |
| Cancel | Cancel at 26.6 % of the STFT stopped within the same progress tick; no partial PNG left |
| Throughput | 2.9 s (33 MB) to 17.3 s (524 MB) in-process on the loaded machine |
| Recorder preview | Reads a fixed 786,432-byte prefix: 0.034 s at 3.9 MS/s, 0.008 s at 15.6 MS/s, 0.002 s at 62.5 MS/s; 6.7-7.6 s per preview (new SFTP session each), UI stall <= 80 ms; labelled "First 0.034 s · 411 MHz (sample)" |
| Signal content | All recordings contained noise floor only (-119 dBFS/bin) with the sensor's spectral shaping at the band edges; the band had no transmission during the session |

**File consistency versus RF accuracy:** the checks above establish that the files are self-consistent (header, frame count, sample rate, duration, encoding) and that the Converter's axes and RBW follow the file. They do not establish absolute RF accuracy (level, frequency offset, image rejection): no known reference signal (signal generator or a transmitter of known frequency and level) was available during the session. That remains **Untested**.

## Observations (not changed)

- O1 Units: Sensor Files and IQ Storage use binary units labelled GB/MB (33,522,232 B shows as "32 MB", 880.6 GB free as "820.1 GB"), while the estimate and the activity log use decimal MB ("31.438192 MB", "33.5 MB"). Consistent inside each view, inconsistent between them. Recommendation: one unit everywhere.
- O2 The estimate shows six decimals ("31.438192 MB"); two would do.
- O3 "Excel: saved 2 row(s)" counts queue items, not rows (three parts of one recording produce one row and messages for 2 + 1 items).
- O4 After a download the application opens File Explorer on the folder (existing behaviour since 1.7.x).
- O5 Closing the main window takes 8-13 s while the worker threads are given their 8 s grace.
- O6 A zero-wait series needs about 13 s per 2 s recording: 4 s poll interval plus the settle time after the sensor's completion message. Safe but slow for very short recordings.
- O7 Sensor file names are UTC (`file_214214` for a 23:42 local start); Sensor Files shows local mtime next to them.
- O8 The "No GPS reference" warning arrived with every recording and is filtered from the log as noise; the position in the metadata is 0/0.

## Not verified on hardware

- Absolute RF accuracy (no reference signal), see above.
- Behaviour across midnight (UTC and local) with real files: covered by unit tests only.
- An incorrect sensor clock: the sensor was 0.25 s ahead; the ±60 s tolerance and the hour-offset guesses are unit-tested, not provoked on the device (the clock was not changed, as requested).
- A full sensor disk: not provoked (the disk was not filled, as requested). The storage block is unit-tested with demo storage.
- The 1.8.9 Setup installer was built and carries version 1.8.9 but was not installed on this PC (it would have reinstalled over the operator's environment); the portable EXE was tested.
- Clean Windows PC without Python, Smart App Control / SmartScreen on the downloaded files, code signing: unchanged from Part 2. A connected sensor does not resolve these.
- Converter 1.5.0 GUI flow on the desktop: not re-driven because the desktop was in use at the time; the engine and headless EXE paths were verified, and the GUI smoke tests pass.

## Test and release status

- Recorder suite: 252 passed, 1 skipped (live-sensor test) - `crfs_iq_recorder> .venv\Scripts\python.exe -m pytest tests -q`.
- Converter suite: 33 passed - `root> venv\Scripts\python.exe -m pytest test_iq_convert_numerics.py test_gui_smoke.py test_iq_local_paths.py -q`.
- Builds: `crfs_iq_recorder\dist\CRFS_IQ_Recorder.exe` (56,852,161 B) and `CRFS_IQ_Recorder_Setup.exe` (43,011,168 B), version resources 1.8.9; `dist\IQ_Spectrogram_Converter.exe` (69,176,941 B, SHA-256 `24F786ECF769166E4EEACBF0A7757BB50FC87D2DEDFEDCA746B264947745977F`), version 1.5.1. Unsigned.
- Hardware harness and logs (not committed; they contain the sensor address): `%LOCALAPPDATA%\Temp\crfs_probe\hw\` - `probe_sensor.py`, `rec_sse.py`, `app_drive.py`, `fault_proxy.py`, `probe_api_edge.py`, `conv_drive.py`, `conv_inproc.py`, `cleanup_drive.py`; logs in `hw\logs\` (`rec_*.json`, `app_*.json`, `proxy_*.txt`, `api_edge_*.json`, `conv_*.json`, `cleanup_*.json`, build logs).

---

# Part 2 - Simulation pass (30 October 2026, Recorder 1.8.8 and Converter 1.5.0)

Date: 30 October 2026
Previous releases: Recorder 1.8.7 (tag `v1.8.7`), Converter 1.4.0 (tag `converter-v1.4.0`). The 1.4.0 converter report is in Git history at that tag.

## Environment

| Item | Value |
| --- | --- |
| PC | Windows 11 26H2 (build 26300), 3840 x 2160 primary display at 150 % scaling, Smart App Control enforced, Defender real-time protection and Controlled folder access on, Defender 4.18.26100.4 / signatures 1.463.34.0 |
| Recorder | Python 3.13.14, PySide6 6.11.2, openpyxl 3.1.5, paramiko 5.0.0, PyInstaller 6.22.3, Inno Setup 6; Excel 16 installed (used only on temp copies, started invisibly) |
| Converter | Python 3.13.14, numpy 2.5.1, scipy 1.18.0, matplotlib 3.11.1, soundfile 0.14.0, PyInstaller 6.22.0 |
| Sensor | None reachable. Every recording and SFTP scenario ran against the demo transport (`DemoTransport`, `DemoSftpBrowser`) or scripted HTTP outcomes. Checks that need real hardware are listed under "Blocked". |
| Real data | One 377 MB stereo PCM-16 recording (31.25 MS/s, 3.0 s) read in place for the large-file cases; the user's collection workbook (3 sheets, VBA, data validations) read read-only and copied to %TEMP% for every write test. No original WAV, workbook, or settings file was modified. |

Test machine load during timing: Zoom, Cursor and a browser were running (55-70 % CPU). Where it matters, old and new builds were measured interleaved in the same session.

## Summary

| Area | Result |
| --- | --- |
| Recorder unit + GUI suite (`crfs_iq_recorder/tests`) | 208 collected: 207 pass, 1 skip (live-sensor test). Before this pass: 188 pass, 1 skip. 19 tests added. |
| Converter numerical + GUI suites (`test_iq_convert_numerics.py`, `test_gui_smoke.py`, `test_iq_local_paths.py`) | 30 pass (16 numerical tests added) |
| Converter QA harness `qa_run.py` (synthetic files, GUI, real 377 MB WAV, packaged EXE) | 39 pass, 0 fail; the two optional cases (reference engine, EXE) also pass when run with `--reference` and `--exe` |
| DSP output versus the 1.4.0 engine | Identical: STFT arrays equal within 1e-5 dB, PNG bytes identical |
| Sustained runs | Recorder: 2 x 50 demo recordings with Excel logging, 100 saved recordings, 200 unique Excel names, no leaked QThreads, handle count flat (263 -> 263). Converter: 2 x 40 files through the GUI, working set flat (143.7 -> 142.3 MB), no worker threads left after close. |
| Defects fixed | 8 (2 high, 4 medium, 2 low), all with regression tests |
| Packaged EXEs | Both rebuilt, version resources 1.8.8 / 1.5.0, launched at native 150 % scaling, Defender custom scan clean, exit without Application Error events. Unsigned (no certificate on this PC). |

## Defects found and fixed

Severity: High = freeze, data loss or wrong data; Medium = wrong behaviour in a reachable path; Low = latent.

| # | App | Severity | Defect | Fix | Verification |
| --- | --- | --- | --- | --- | --- |
| R1 | Recorder | High | Excel helpers walked the worksheet's *used range*. The user's TETRA sheet has a used range of 1,048,544 rows (stray formatting), so `_last_used_row` touched 14.7 M cells (24.8 s on the UI thread), `find_row_with_iq_names` 1.7 s, the duplicate check 2.5 s, and the COM path did not finish in 60 s. The catalog load at startup materialised the same cells: +360 MB working set. | `excel_writer`: iterate only populated cells (`ws._cells`), stop read-only scans after 2000 empty rows. `collection_catalog.sheet_rows`: same empty-run limit. `excel_com`: `Cells.Find(xlPrevious)` for the last row, one bulk range read for headers and the IQ column. | `test_inflated_used_range_catalog_and_append_stay_fast`, `test_workbook_has_iq_filename_stops_scanning_after_empty_run`. Measured on a copy of the real workbook: catalog 3.62 s / 400 MB -> 0.02 s / 38 MB; `_last_used_row` 24.8 s -> 0.00 s; COM append > 60 s -> 2.3 s. Workbook round trip keeps `vbaProject.bin`, styles, theme, data validations and the other sheets byte-identical; only the empty formatted rows are dropped (sheet XML 23 MB -> 17 KB). |
| R2 | Recorder | High | Excel writes ran on the UI thread (`_flush_excel` called `flush_excel_queue` directly), so the window froze for the duration of R1. The queue was also mutated from the UI while a write ran. | Flush now runs through `_start_job` on a worker with coalescing (`_excel_flush_in_flight` / `_excel_flush_again`); `ExcelLogStore` has an `RLock`, `snapshot_queue()` and `drop_from_queue()` so items enqueued during a write survive. Demo mode keeps the synchronous path so tests stay deterministic. | `test_gui_excel_flush_runs_on_worker_and_keeps_ui_state` (writer thread != UI thread, coalescing, merged row for both parts), `test_flush_keeps_items_enqueued_while_writing`, `test_flush_with_empty_queue_does_not_rewrite_store`. |
| R3 | Recorder | Medium | COM append could leave an invisible EXCEL.EXE behind (attached to an instance that was quitting) and `GetActiveObject` hung 61 s on a stale ROT entry (0x800706be) when Excel had just closed. | Attach only when an `XLMAIN` window exists and the instance holds our workbook; otherwise start a fresh invisible instance, close the workbook, `Quit`, and wait for that PID to exit (15 s cap). | 8 consecutive appends on a temp copy: no orphan process, no hang. A first-chance `0x80010108 RPC_E_DISCONNECTED` message at `CoUninitialize` can still be printed by pywin32 in debug runs; it is benign and does not affect the result. |
| R4 | Recorder | High | All four JSON state files (settings, saved recording parameters, Excel dedupe set + pending queue, presets) were written with a truncating `write_text`. A kill or power loss during the write left an empty file, and the loaders silently returned empty state: recording-to-file associations lost, Excel dedupe set lost (duplicate rows after restart), pending Excel rows lost, connection settings lost. | New `json_store.py`: write to a sibling temp file and `os.replace` (atomic on NTFS), keep the previous good document as `.bak`, fall back to `.bak` when the live file is missing or corrupt, retry `os.replace` on Windows sharing violations and fall back to a plain write as the last resort. Used by `paths`, `recording_history`, `excel_log`, `presets` (presets without `.bak`). | `tests/test_json_store.py` (10 tests): truncated/empty live file recovers from backup; simulated crash while writing the temp file keeps the old document; blocked `os.replace` retries then falls back; recordings survive a truncated file on restart and the next save repairs it; Excel dedupe set survives a corrupt file; settings survive a corrupt file with secrets still stripped. |
| C1 | Converter | Medium | `process_file` allocated the Blackman-Harris window (2^24 points, 65 MB, 2.7 s) *before* validating the file, so a too-short file or a tiny RBW paid the cost and a very small RBW could try to allocate an FFT of hundreds of millions of points. | Validation (channels, samples, FFT size cap, frame count) moved before any allocation; new `MAX_N_FFT = 2^24` guard with a message that states the smallest usable RBW for that sample rate. | `test_too_short_file_fails_before_any_window_is_allocated`, `test_rbw_finer_than_the_fft_cap_is_rejected_with_guidance`. |
| C2 | Converter | Medium | NaN or Inf samples in a float WAV propagated through the FFT and max-pooling and blanked whole columns of the spectrogram (and the peak/floor used for auto scaling). | Float sources are sanitised per chunk with `nan_to_num` (NaN/Inf -> 0). PCM sources cannot contain non-finite values and skip the check. | `test_nan_and_inf_samples_in_float_wav_do_not_poison_the_spectrum`: one NaN, one +Inf and one -Inf sample in a 0.5 s tone; every spectrum value finite, peak at the right bin within 0.2 dB of 0 dBFS. |
| C3 | Converter | Low | The engine preload was scheduled with an untracked `after(30, ...)`; closing the window within 30 ms of opening fired it on a destroyed Tk. Reachable from tests and from the batch tooling, not from normal use. | `_preload_job` is tracked and cancelled in `_on_close`. | Covered by the GUI smoke suite (`close during conversion`, window open/close in `qa_run.py`). |
| C4 | Converter | Low | Settings JSON written with a truncating `write_text` (same class as R4, settings only). | Temp file + `os.replace`. | `qa_run.py` theme/settings round-trip cases. |

Also added: `stats` out-parameter on `process_file` (frequency axis, mean and peak spectra, waterfall, true RBW, peak) so numerical tests compare arrays instead of pixels; `isolated_excel_log` fixture so the recorder GUI tests no longer write into the developer's own Excel log file.

## Recorder scenarios

Executed = ran in this pass with the stated result. Covered = existing test exercised and passing in the full-suite run.

| Scenario | How | Result |
| --- | --- | --- |
| Frequency inputs: kHz/MHz/GHz, start/end <-> center/bandwidth conversion, invalid values | Covered: `test_gui_start_end_mhz_converts_to_hz`, `test_gui_ghz_unit_converts_to_hz`, `test_gui_mode_switch_converts_fields`, `test_gui_rejects_invalid_frequency`, `test_frequency.py` (parsing, rounding, display) | Pass |
| Sensor model/firmware/format validation | Covered: `test_sensor_info.py`, `test_gui_verified_format_is_sent`, `test_stale_sensor_info_does_not_overwrite` | Pass. Against a real sensor: blocked. |
| Recording request parameters (task id, duration, band) | Executed: `test_gui_series_sends_distinct_task_ids_and_logs_each_completion` (3 scripted accepted POSTs, duration 0.05 s each, distinct `iq-` task ids), covered: `test_request_builder.py`, `test_gui_start_uses_unique_task_id_and_blocks_duplicates` | Pass |
| Repeated recordings, delays, stop after current, stop during countdown | Covered: `test_gui_suggests_iridium_and_logs_repeat_parts`, `test_gui_stop_after_current_does_not_start_next`, `test_gui_wait_minutes_and_stop_during_countdown`; executed: sustained 2 x 50 series | Pass |
| Connection loss before submission | Executed: `test_gui_connection_failure_before_send_drops_record_and_ends_series` - one attempt, saved parameters discarded, series ended, Start re-enabled, no retry | Pass |
| Response lost after submission (sensor may be recording) | Executed: `test_gui_lost_response_after_send_keeps_record_and_never_retries` - exactly one POST, phase stays RECORDING, parameters kept, second Start ignored, timer elapse goes to FINALIZING with still one POST | Pass. `EmpClient.start_recording` has no retry loop (`test_read_timeout_after_send_is_unknown_and_not_retried`). |
| Hung HTTP does not block the next recording | Covered: `test_hung_http_does_not_block_the_next_recording` | Pass |
| Association of parameters with files, split parts, parts appearing later, UTC/local names, sensor IP change | Covered: `test_recording_history.py` (15 tests incl. `test_later_split_part_matches_without_first_file`, `test_persist_stem_then_match_later_parts`, `test_does_not_assign_metadata_to_unrelated_newest_file`) | Pass |
| Midnight crossing: both daily folders watched | Executed: `test_folders_for_recording_cover_a_midnight_crossing` (23:59:40 -> 00:00:30 yields 20260910 and 20260911, start-day first); covered: `test_sftp_paths.py` local/UTC candidates | Pass |
| SFTP listing, sort persistence, grouping of parts, newest first, up/parent | Covered: `test_sftp_listing.py`, `test_listing_sort.py`, `test_sftp_column_sort_persists_until_directory_change`, `test_sftp_lists_newest_files_first_without_new_on_first_listing`, `test_sftp_up_opens_parent_folder` | Pass |
| Multi-file and parent (all parts) download, fixed folder, no dialogs | Covered: `test_sftp_downloads_multiple_files`, `test_download_parent_downloads_split_parts`, `test_sftp_begin_downloads_uses_fixed_folder_without_dialogs` | Pass |
| Interrupted download, partial file, name collision | Covered: `test_download_util.py` (`.part` removed on short download, larger-than-listed accepted, " (n)" suffix, complete file moved) | Pass |
| Keyboard deletion of remote files | Covered: `test_sftp_delete_removes_selected_files_only`, `test_sftp_delete_parent_removes_all_listed_parts` (demo SFTP only) | Pass. Note: Delete had no confirmation dialog; left as is here, changed in 1.8.9 (Part 1, H15). |
| Slow/hanging SFTP connect, close during connect, stale preview, list failure | Covered: `test_sftp_ui_responsiveness.py` (8 tests, bounded timeouts) | Pass |
| Sensor and local storage, estimate vs actual size, blocked recording | Covered: `test_sensor_storage.py` (14 tests incl. `test_estimated_bytes_match_empirical_mb`, `test_gui_shows_demo_storage_and_blocks_when_full`), `test_size_estimate.py` | Pass. Split-part sizes from a real sensor: blocked. |
| Excel logging: duplicates, pending queue, locked workbook, restart recovery, macros/formulas/formatting | Covered: `test_collection_excel.py` (16 tests) and the Excel cases in `test_gui_smoke.py`; executed: inventory of the real workbook before/after an openpyxl round trip (temp copy) - all zip parts identical except the sheet XML that lost its empty formatted rows; `vbaProject.bin` kept | Pass |
| State files survive a crash mid-write | Executed: `tests/test_json_store.py` | Pass (fixed, R4) |
| Worker threads stopped at exit | Covered: `test_worker_threads.py`; executed: sustained run `leaked_at_stop = 0` | Pass |

## Converter scenarios

Synthetic IQ signals (float32 and PCM-16 WAV written with soundfile, fs = 48 kHz, RBW 100 Hz -> 1024-point FFT, bin width 46.875 Hz). Expectations computed independently in the tests (20 log10 of amplitude, numpy FFT of one windowed frame, ENBW of the periodic 4-term Blackman-Harris window = 2.0044 bins). Tolerance 0.05 dB for on-bin amplitudes, 0.02 dB against the independent FFT, 0.83 dB (the window's scalloping loss) for off-bin tones.

| Scenario | Test | Result |
| --- | --- | --- |
| Full-scale tone on a bin centre reads 0 dBFS at exactly that frequency; mirror image < -60 dB | `test_full_scale_on_bin_tone_reads_0_dbfs_at_the_right_frequency` | Pass (-0.000 dBFS) |
| Amplitude scaling follows 20 log10 (0.5, 0.1, 0.01) | `test_amplitude_scaling_follows_20log10` | Pass |
| Peak equals an independently computed windowed FFT over 9 bins | `test_peak_matches_independent_windowed_fft` | Pass |
| Off-bin tone loss within Blackman-Harris scalloping | `test_off_bin_tone_loss_is_within_blackman_harris_scalloping` | Pass |
| Negative frequency and I/Q channel order (I = left, Q = right, +f for cos + j sin) | `test_negative_frequency_tone_and_iq_channel_order` | Pass |
| Two tones keep independent levels, valley < -60 dB | `test_two_tones_keep_independent_levels_and_rbw_resolves_them` | Pass |
| Reported RBW = ENBW x bin width | `test_reported_rbw_is_enbw_times_bin_width` | Pass |
| Chunked processing equals single-block processing and covers every frame | `test_chunked_processing_equals_single_block_and_covers_all_frames` (blocks forced to 3 frames) | Pass |
| Time axis: a tone that starts at 50 % appears only in the second half of the waterfall | `test_time_axis_puts_a_late_tone_in_the_second_half` | Pass |
| DC offset shows at 0 Hz | `test_dc_offset_shows_at_zero_hz` | Pass |
| Clipped full-scale square wave converts, stays finite | `test_clipped_full_scale_square_wave_converts_and_stays_finite` | Pass |
| NaN / Inf samples in float WAV | `test_nan_and_inf_samples_in_float_wav_do_not_poison_the_spectrum` | Pass (fixed, C2) |
| Too-short file rejected before any window allocation | `test_too_short_file_fails_before_any_window_is_allocated` | Pass (fixed, C1) |
| RBW finer than the FFT cap rejected with the minimum usable RBW | `test_rbw_finer_than_the_fft_cap_is_rejected_with_guidance` | Pass (fixed, C1) |
| PCM-16 and float32 encodings give the same spectrum | `test_pcm16_and_float32_encodings_give_the_same_spectrum` | Pass |
| Corrupted header, truncated data, 0-byte, non-WAV, MPEG tag, mono, no metadata, silence, invalid RBW, dB range, colormap, Unicode and long paths, read-only folder, duplicate stems, atomic replace, cancel, original file unchanged (SHA-256), 377 MB real file in chunks | `qa_run.py` engine cases (30) | Pass |
| GUI: engine loads in background, repeated Convert ignored, Cancel, failed job then good job, batch with duplicate names, drop parsing, theme persistence, settings round trip, minimum size, close during conversion | `qa_run.py` GUI cases (10), `test_gui_smoke.py` | Pass |
| Packaged EXE converts headless with Python removed from PATH / env | `qa_run.py --exe` | Pass (10.2 s on the loaded machine). Not a clean VM. |

## Performance (before -> after)

| Measurement | Before | After | Note |
| --- | --- | --- | --- |
| Recorder EXE: peak working set at startup (user's workbook configured) | 537-541 MB | 176-178 MB | Interleaved 1.8.7 vs 1.8.8, same session. Cause: R1 catalog load. |
| Recorder EXE: window visible (warm) | 3.3-3.9 s (1.8.7, earlier session); 4.0 s (1.8.7, interleaved) | 3.3-4.1 s | No change within noise |
| Recorder EXE: handles at idle | 1177 | 1177 | |
| Recorder: catalog load of the real workbook (Python) | 3.62 s, +360 MB | 0.02 s, +2 MB | |
| Recorder: openpyxl append on the real workbook copy | `_last_used_row` 24.8 s (14.7 M cells) + write | 0.00 s + 2.4 s write (full load 2.5 s) | |
| Recorder: COM (Excel) append on the real workbook copy | > 60 s, aborted | 2.3 s, Excel exits afterwards | |
| Recorder: 50 demo recordings with Excel logging | - | 7.0 s / 8.7 s per 50, WS +13 MB then +5 MB, handles flat | New sustained run |
| Converter engine: 377 MB PCM-16 WAV, RBW 15 kHz | 6.3-7.5 s idle machine; 8.4-9.3 s loaded (1.4.0 engine, interleaved) | 8.8-9.6 s loaded (interleaved) | Within noise; the PCM path is unchanged apart from assembling `stats`. Peak WS 454 -> 455 MB. |
| Converter EXE: window visible (warm) | 2.5-3.1 s | 2.8-3.0 s | |
| Converter EXE: working set after start | 143.5 MB, 273 handles | 143.2-143.9 MB, 271-273 handles | |
| Converter: 40 files through the GUI, twice | - | 17.5 s / 18.1 s, WS 143.7 -> 142.3 MB | New sustained run |

## UI and distribution

| Check | Result |
| --- | --- |
| Both EXEs launched on the 3840 x 2160 display at 150 %: layout, dark theme, version label (v1.8.8 / v1.5.0), Sensorz taskbar icon, status bar | Pass (screenshots inspected, not committed - they show the configured sensor address) |
| Recorder at Qt scale 1.0 / 1.25 / 1.5 (offscreen): no overlap among the frequency, format, time, unit and Start widgets; no input under 24 px; minimum sizes 980 x 303 (main) and 995 x 485 (SFTP) logical px, both fit 1366 x 768 at 125 % and 1920 x 1080 at 150 % | Pass. A 1366 x 768 display at 150 % (911 logical px wide) would need the SFTP window scrolled horizontally. |
| Light and dark theme contrast in the recorder combos and log | Covered: `test_combo_popups_have_readable_contrast`, `test_theme_toggle_persists`; offscreen renders of both themes inspected | Pass |
| Converter minimum size, controls visible, theme toggle | `qa_run.py` GUI cases | Pass |
| Defender custom scan of the three new binaries | Clean, 0 detections, no exclusions added | Pass |
| Authenticode | NotSigned (all three). No certificate or Artifact Signing account on this PC; `build_exe.bat` signs automatically once `CRFS_SIGN_*` is set. | Documented |
| SmartScreen vs Smart App Control vs antivirus | Documented with the three messages, their meaning and the right action in `SECURITY_NOTES.md` (converter) and `crfs_iq_recorder/docs/DISTRIBUTION.md` (recorder, incl. Controlled folder access and `packaging/diagnose_block.ps1`) | Documented |
| Clean exit | Both EXEs closed from the title bar; no Application Error (event 1000) entries | Pass |

## Blocked or not performed (status after Part 1 in brackets)

- Real sensor: POST submission, task completion detection, split files created by the sensor, sensor-side storage numbers, incorrect sensor clocks. All of these ran against the demo transport only; the HTTP client contract (`test_api_client.py`) is unit-tested. [Done on hardware in Part 1 except the incorrect-clock case; the real POST contract turned out to differ from the demo transport, see H1-H4.]
- Clean Windows PC without Python: not available. The EXEs ran from %TEMP% with Python stripped from PATH and the environment on the build PC.
- Smart App Control / SmartScreen behaviour of the **downloaded** 1.8.8 / 1.5.0 files was not re-tested in this pass; the binaries are unsigned exactly like 1.8.6 / 1.8.7, whose behaviour (SAC block of the unsigned installer, SmartScreen "Run anyway" on the portable EXE) is documented in `DISTRIBUTION.md`.
- 100 % / 125 % scaling only through `QT_SCALE_FACTOR` on the offscreen platform (Qt has no fonts there, so text rendering was not judged); the native display is 150 %.
- Excel COM path tested only on temp copies with Excel started invisibly, never on the user's open workbook.
- Code signing: not possible on this PC.
- `unique_task_id()` has one-second resolution; two Start presses within the same second are already prevented by the GUI state machine, so it was left unchanged. [Reassessed on hardware in Part 1: still unreachable in a zero-wait series; unchanged.]
- Delete key removes remote files without a confirmation dialog (existing, intentional per earlier tests). [Changed in 1.8.9: confirmation added, H15.]

## How to rerun

```text
crfs_iq_recorder> .venv\Scripts\python.exe -m pytest tests -q
root> venv\Scripts\python.exe -m pytest test_iq_convert_numerics.py test_gui_smoke.py test_iq_local_paths.py -q
root> venv\Scripts\python.exe qa_run.py --reference <1.4.0 iq_data.py> --real-wav <large stereo WAV> --exe dist\IQ_Spectrogram_Converter.exe --report results.json
```

All tests use temp folders and synthetic files or temp copies; nothing touches the sensor, the original recordings or the real workbook.
