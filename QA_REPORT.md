# QA report - CRFS IQ Recorder 1.8.8 and IQ Spectrogram Converter 1.5.0

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
| Keyboard deletion of remote files | Covered: `test_sftp_delete_removes_selected_files_only`, `test_sftp_delete_parent_removes_all_listed_parts` (demo SFTP only) | Pass. Note: Delete has no confirmation dialog; existing behaviour, left as is. |
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

## Blocked or not performed

- Real sensor: POST submission, task completion detection, split files created by the sensor, sensor-side storage numbers, incorrect sensor clocks. All of these ran against the demo transport only; the HTTP client contract (`test_api_client.py`) is unit-tested.
- Clean Windows PC without Python: not available. The EXEs ran from %TEMP% with Python stripped from PATH and the environment on the build PC.
- Smart App Control / SmartScreen behaviour of the **downloaded** 1.8.8 / 1.5.0 files was not re-tested in this pass; the binaries are unsigned exactly like 1.8.6 / 1.8.7, whose behaviour (SAC block of the unsigned installer, SmartScreen "Run anyway" on the portable EXE) is documented in `DISTRIBUTION.md`.
- 100 % / 125 % scaling only through `QT_SCALE_FACTOR` on the offscreen platform (Qt has no fonts there, so text rendering was not judged); the native display is 150 %.
- Excel COM path tested only on temp copies with Excel started invisibly, never on the user's open workbook.
- Code signing: not possible on this PC.
- `unique_task_id()` has one-second resolution; two Start presses within the same second are already prevented by the GUI state machine, so it was left unchanged.
- Delete key removes remote files without a confirmation dialog (existing, intentional per earlier tests).

## How to rerun

```text
crfs_iq_recorder> .venv\Scripts\python.exe -m pytest tests -q
root> venv\Scripts\python.exe -m pytest test_iq_convert_numerics.py test_gui_smoke.py test_iq_local_paths.py -q
root> venv\Scripts\python.exe qa_run.py --reference <1.4.0 iq_data.py> --real-wav <large stereo WAV> --exe dist\IQ_Spectrogram_Converter.exe --report results.json
```

All tests use temp folders and synthetic files or temp copies; nothing touches the sensor, the original recordings or the real workbook.
