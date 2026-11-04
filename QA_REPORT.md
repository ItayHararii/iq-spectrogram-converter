# QA report - CRFS IQ Recorder 1.13.1 / 1.13.0 / 1.12.0 / 1.11.0 / 1.10.0 / 1.9.1 / 1.9.0 / 1.8.10 / 1.8.9 and IQ Spectrogram Converter 1.5.1

Nine passes plus a follow-up. Part H (4 November 2026, morning) is an on-screen check of 1.13.0 on the operator's desktop, the investigation of the unexplained series start of 3 November with a guard on every way a recording can start, and a neutral Manual tab while an Excel run records; it fixed nine defects and produced Recorder 1.13.1. Part G (3 November 2026, evening) sends every protocol row as one request, reads every result from the files, splits the window into the Manual Recording and Excel Collection tabs, and ran one hardware campaign through the final application; it found and fixed six defects (the most important one: downloads of a finished collection were lost when the next collection started or the app was closed) and produced Recorder 1.13.0. Part F (3 November 2026, late morning) is the complete end-to-end pass on the Node 100-18 after the 1.11.0 polish: manual recordings in both frequency modes, the SFTP browser, the Cellular Collection with the workbook open in Excel, locked, closed and restarted, connection faults, the executables, and the four presentation observations made on 1.11.0; it found and fixed five defects (the most important one: a scheduled pass whose time went by while the app was closed was started late on its own by *Continue*) and produced Recorder 1.12.0. Part E (3 November 2026, morning) polished the GUI - the Cellular Collection panel above all - and removed the measured window-construction and theme-switch slowness; it produced Recorder 1.11.0 with no change to recording, Excel or scheduling behaviour. Part D (2 November 2026, night) simplified the Cellular Collection to the operator's workflow (the protocol rows are the plan and the result rows, nine columns, compact panel, local-time pickers), fixed the four reliability faults found in the review of the 18:44 run and three more found on the sensor during this pass, and ran the complete path from capture to Excel on the Node 100-18; it produced Recorder 1.10.0. Part C.1 (2 November 2026, evening) ran the Cellular Collection against the operator's own protocol workbook, fixed the import defects that surfaced and added the results sheets in the protocol workbook; it produced Recorder 1.9.1. Part C (2 November 2026) added the Cellular Collection feature and produced Recorder 1.9.0. Part 0 (1 November 2026) fixed the "Time elapsed - file not confirmed" status, investigated why the Node splits recordings, and added the optional merge of recording parts; it produced Recorder 1.8.10 (the Converter did not need a change). Part 1 (31 October 2026) ran against the same RFeye Node and produced Recorder 1.8.9 and Converter 1.5.1. Part 2 (30 October 2026) is the earlier simulation-only pass that produced 1.8.8 / 1.5.0; all are kept unchanged below except where a later result supersedes a statement.

Releases: Recorder 1.13.1 (tag `v1.13.1`), Converter 1.5.1 (tag `converter-v1.5.1`). Previous: 1.13.0 (`v1.13.0`), 1.12.0 (`v1.12.0`), 1.11.0 (`v1.11.0`), 1.10.0 (`v1.10.0`), 1.9.1 (`v1.9.1`), 1.9.0 (`v1.9.0`), 1.8.10 (`v1.8.10`), 1.8.9 (`v1.8.9`), 1.8.8 (`v1.8.8`), 1.5.0 (`converter-v1.5.0`), 1.8.7 (`v1.8.7`), 1.4.0 (`converter-v1.4.0`). All earlier releases stay on GitHub.

Legend used throughout: **Hardware** = executed against the real sensor in this pass; **Simulated** = executed against the demo transport, a local proxy or synthetic files; **Untested** = not executed.

---

# Part H - On-screen check, the start guard and a neutral Manual tab (4 November 2026, Recorder 1.13.1)

## Scope

- **On-screen check of 1.13.0** on the operator's desktop. The released EXE ran as a separate, titled test instance with an isolated copy of the operator's settings: both tabs, light and dark, `QT_SCALE_FACTOR` 1.25 and 1.5, popups, the Last recording card, Sensor Files and the minimum window size. It used real mouse input on the test instance only, with a screenshot before every action. One short real recording was made.
- **The unexplained series start of 3 November (21:44)** and the reload / Sensor Files events of 22:32-22:35: cause, and a guard on every way a recording can start.
- **The Manual tab during an Excel run** must not show the Excel row's recording states.
- **EXE check of 1.13.1.** The operator was working at the PC by then, so it was done through Windows UI Automation and `PrintWindow` on the second monitor, with no real mouse or keyboard input.

## Findings

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| H1 | **High** | **Code review and tests.** Start Recording was a default button, and a mouse click left the keyboard focus on it, so a later Enter or Space started another recording. In the Excel preview, Start was the dialog's default button, so Enter started the collection. Nothing else in the app starts a Manual series (see the investigation below). | The buttons that start recordings or collections are never default. A click leaves no focus on them, Enter is ignored, and Space works only after the operator moved there with Tab from an enabled control. Focus that lands on them because another control was disabled or hidden, or comes back on activation, does not arm them. Also: the preview dialog's Start is not default (Close is); Stop's question defaults to *No*; every request logs its trigger (`Start button (mouse)`, `(keyboard)`, `(program)`, `repeat series (n of N)`, the Excel row); a series logs *Repeat: recording n of N.*; a scheduled pass logs *Scheduled pass N started (set for HH:MM)*. Tests in `test_start_guard.py`. |
| H2 | Medium | **Visible (1.13.0).** *Schedule* without a start time read *Ready to start* with Start enabled; the error came only after the click. The state badge said *Ready*. | Start is disabled with *Add at least one start time.*, the status reads *Schedule not set · N rows*, and the badge is neutral. Test `test_schedule_uses_the_pickers_and_the_local_clock`. |
| H3 | Medium | **Visible (1.13.0).** The calendar opened below its field and reached the taskbar; in a lower window position it was cut off. The placement did not count the title bar or the dialog's minimum size. | Both are counted, and the popup flips above the field when the screen ends. Test `test_popups_count_the_title_bar_so_they_never_end_behind_the_taskbar`. |
| H4 | Medium | **UI Automation (1.13.1 EXE).** Sensor Files was always fitted to the primary monitor. Opened from a main window on a 1920×1200 monitor at 150 %, it ran about 250 px below the work area. | Fitted to the main window's monitor, with room for the title bar (this applies to every window fitted to the screen). Test `test_sensor_files_fit_the_main_windows_screen_with_room_for_the_title_bar`. |
| H5 | Medium | **Hardware (1.13.1 EXE).** While an Excel row recorded, the Manual tab's status line showed the Excel row's states (Part G, Info row). | The Manual tab shows *Idle* (muted) with *Excel Collection is running.* while another feature holds the sensor; its own states are unchanged. Test `test_manual_status_line_is_neutral_while_the_excel_collection_records`. |
| H6 | Low | **Visible (1.13.0).** The Units list showed a scrollbar, and the Format list cut off *HDF5*. The list's own 1 px border was not counted in its height. | The inner border was removed (the popup frame already has one). Test `test_dropdown_lists_show_every_choice_without_a_scrollbar`. |
| H7 | Low | **Visible (1.13.0).** The size estimate showed six decimals (*≈ 251.500692 MB*). | One decimal, or *< 0.1 MB*. Tests in `test_size_estimate.py`. |
| H8 | Low | **Visible (1.13.0).** Sensor Files cut the file name (*iq_202611…_0001.wav*) despite free space, and showed *Modified* day-first while the regional format is month-first. | The Name column fits its contents, and dates follow the regional short format. Test `test_sftp_names_are_not_cut_and_dates_follow_the_regional_format`. |
| H9 | Low | **Code review.** An Excel collection start, resume, continue, missed pass or retry did not say what triggered it. | `collection_started` carries the trigger (*Start button (…), then preview Start (…)*), and the other four log their button. Tests `test_an_excel_collection_logs_which_buttons_started_it`, `test_a_collection_start_names_its_trigger_in_the_log`. |

Seen and **listed, not changed** in 1.13.1:
- A stray *Storage unavailable* caption under the sensor card while disconnected, and *Sensor reconnecting…* before the first connection.
- A manual-tab scrollbar with almost no range at 100 %.
- The minimum window size (about 1065×723) is large for a small screen.
- **See details** opens the Windows *Open with* chooser when no app is set for `.log`.
- In dark, the disabled ghost buttons (Pause, Stop) look almost enabled and have no reason tooltip. Changing the style touches every outlined button.
- Sensor Files: **Download** looks enabled before a file is selected, and the window opens over the main window.
- Duplicate *Sensor connected.* / *Connected to CRFS 100-18.* lines.
- The preview table is cramped.
- *1 pass(es)* wording.
- The *Missed pass* activity text gives its date day-first.
- **Copy details** includes one about 4 KB event-stream line.
- The slow first tab switch seen once was not reproduced; it followed a workbook load.

## The unexplained series start (3 November) - investigation

- **21:44:44.** Windows logs show the session locked and in Modern Standby from 21:26:38 to 21:57:51 (Kernel-Power 506/507, Winlogon 811/812), so no person pressed anything.
- **What the app can do on its own.** The code paths and the tests show the app cannot start a Manual series by itself:
  - a series begins only from Start's `clicked` signal;
  - restore, the timers and the missed-pass logic never arm one.
- **Most likely cause:** the previous pass's in-process test driver (a programmatic click, or a queued action run after a context reset). **Not proven.**
- **22:32-22:35.** The session was unlocked (22:01:39-22:41:14) and in use. Most likely a person used the visible test window. **Not proven.**
- **The real hazard found** is H1: a click left the focus on Start, so a later Space or Enter could start again.
- **Tests** (`test_start_guard.py`, offscreen, with real key events through the window):
  - Enter and Return in every Manual field (Repeat set to Count) and every Excel field start nothing;
  - Enter on the Start buttons does nothing;
  - a click leaves no focus;
  - Space starts only after Tab from an enabled control, not after the focused control was disabled or hidden;
  - restore never arms a series or a schedule;
  - unlock- and standby-like sequences start nothing, on both tabs, with Start focused when the PC locks: another window activated, deactivate events, minimise, the link dropped, then restore, activate and screen-change events, the link back, and late Enter / Space;
  - each request names its trigger.
- **Mutation checks.** The guard, status-line, placement, date, schedule, Sensor Files and Excel-trigger tests were run against copies with their fix reverted (the old button, loose arming, the old phase line, the old placement, the old date text, no schedule check, the old badge, no screen argument, no title-bar room, the trigger dropped or not logged), and each failed there. The dropdown fix was checked by measurement (scroll range 1 → 0).

## On-screen and EXE checks

| Check | How | Result |
| --- | --- | --- |
| 1.13.0 EXE, both tabs, light and dark, `QT_SCALE_FACTOR` 1 / 1.25 / 1.5 | **Visible**, real mouse input on the test instance | Layouts fit. Findings H2, H3, H6-H8 and the listed items. A real Return key in the test window's *Recording time* field started nothing. |
| 1.13.0 recording, 10 MHz × 1 s | **Visible**, real sensor | Live states and card shown; the file was downloaded to the isolated folder and later deleted from the Node. |
| 1.13.1 EXE (09:19 build), Excel tab | **UI Automation**, no real input | *Schedule* without a time: *Schedule not set · 1 row*, *Add at least one start time.*, Start disabled. The badge still read *Ready*; fixed afterwards (H2) and rebuilt. *Run now*: *Ready to start · 1 row*, Start enabled. |
| 1.13.1 EXE, pickers and lists | **UI Automation** | The time picker opened above its button, inside the work area. Units (kHz / MHz / GHz) and Format (WAVE / XDAT / NCP / HDF5) showed every choice with no scrollbar. The calendar did not open from its button under UI Automation (no error logged). |
| 1.13.1 EXE, Excel run (1 row, 1 s) | **UI Automation, real sensor** | Identity checked by the app, then Start and the preview's Start. Meanwhile the Manual tab read *Idle* / *Excel Collection is running.*, with Start Recording disabled. *Completed 1 · Partial 0 · Failed 0 · Uncertain 0*; download verified by SHA-256. |
| 1.13.1 EXE, Sensor Files, close | **UI Automation** | Full file name; *Modified* in the regional format. H4 found. The EXE closed normally. |
| Rebuilt 1.13.1 (final artefacts) | Static | Both the portable EXE and the installer's payload were **blocked by Smart App Control** (WinError 4551; Code Integrity events 3033, 3077, 3089, 3118). No security setting was changed. Both EXEs' archives contain the final code (version, H2, H4 and H9 strings). Not run. |
| Calendar and Sensor Files from source | **Real Windows platform**, demo, isolated settings | Under UI Automation the calendar opened and stayed inside the work area, flipped above the field. Sensor Files opened on the main window's monitor (1280×752 work area) with its whole frame inside. |

Sensor files created by this pass (Node 100-18 `rfeye100400`, folder `20261104`): `iq_20261104_063851_0001.wav` (1.13.0 visible check) and `iq_20261104_074252_0001.wav` (1.13.1 Excel run). Both were deleted by exact name and size after an identity and idle check, and the folder was empty afterwards. Local copies stayed in the isolated test folder, which was deleted.

## Incident notes

- **Developer settings.** From a source checkout the app keeps its settings next to the code. The helper scripts of this pass loaded the connection settings from there, read only, until this was noticed. Through them, identity, mission-list and folder-listing calls reached the other sensor configured there (an R40-8); nothing was recorded or deleted on it. Source-run check windows also rewrote those settings (the last time at 09:38): most likely the last tab and the Excel schedule date. The previous values cannot be recovered, because the backup copy is rewritten with each save. Afterwards the helpers used only the isolated copy.
- **One stray click (1.13.0 check).** The calendar closed between a screenshot and the next click, and that click landed on an empty spot of the window behind it (another application); it had no effect. The calendar also closed by itself once in that check.
- **Suite output.** One full run printed a fragment of a fault-handler stack dump in its first part, while every test passed. The two later full runs did not show it. Likely a handled COM exception from the open-workbook check while Excel was open on the desktop. Not proven.

## Not done / still to verify

- The final 1.13.1 artefacts could not be run on this PC (Smart App Control, above). Their changes since the 09:19 build (H2 badge, H4, H9, the preview Start) are covered by the tests, the real-platform source check and the static check. The installer was not installed.
- The calendar opened from its button in the EXE is not verified: it did not open under UI Automation, and it once closed by itself in the visible check. From source it opens and stays open.
- A Manual recording through the 1.13.1 EXE was not made; its trigger logging is covered offscreen.

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 490 collected: **488 pass, 2 skip** (no local copy of the protocol workbook), 0 fail, 84 s, on the final code before the build, not overlapping it. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`, exit 0, 213 s: `dist\CRFS_IQ_Recorder.exe` 57,360,232 B (SHA-256 `623BDC52…25B870`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,513,941 B (SHA-256 `70D0886C…25A6CA`), version resources 1.13.1. Not Authenticode-signed (unchanged). |
| EXE test | The 09:19 build ran as the installer's payload (the portable EXE was blocked by Smart App Control), on the second monitor, driven by UI Automation with no real input, against the real Node; see above. The final rebuild: static check only. |
| Git | Branch `cursor/polish-gui-easy-launch`, annotated tag `v1.13.1`, pushed to `personal`; GitHub Release `v1.13.1` with both files. 1.13.0 and older releases kept. |

---

# Part G - One request per row, measured results, the two tabs, and a hardware campaign (3 November 2026, Recorder 1.13.0)

## Scope

The direct requests for 1.13.0 and the workflow addendum, then one hardware campaign through the final application:

- **One request per row.** Each protocol row is sent to CRFS as one request with its own start/stop and the app's duration. The app no longer derives a capture window from the duration, applies a 625 MB budget, or splits, narrows or shifts a range. Plans saved by 1.10.0-1.12.0 are *legacy*: their results are kept, and they are never continued.
- **Measured results.** Duration, sample rate, centre and bandwidth are read from the WAVE header and the CRFS `meta` trailer. A recording is *completed*, *partial* (more than 2 % short), *uncertain* or *failed*. Nothing is substituted from the request, and nothing is re-sent on its own.
- **Operator log and diagnostics.** The activity log has short sentences only. A rotating diagnostic log holds the detail, opened with **See details**; **Copy details** produces a masked support report.
- **Workflow addendum.**
  - **Manual Recording** / **Excel Collection** tabs with one controller of the sensor, and a reason line on the other tab's Start.
  - The *Last recording* card: Requested / Received / Recorded, the tag, and separate recording / transfer / Excel states. Its **Open folder**, **View waterfall** and **Download** buttons.
  - The end-of-pass summary with **Retry failed rows** (reconcile first).
  - Manual Pause/Resume.
  - Restore of the last tab and form.

Hardware rows ran on **Node 100-18, model `R100-18`, serial `rfeye100400`, firmware `2.25-325`**, from the source build of the final code with an isolated copy of the operator's settings (only the address changed). The identity, the mission list and a file-growth check came before every batch, and the Node's configuration was not changed.

The desktop session was locked from 21:26 to about 22:30. The application was therefore driven in-process through its real widgets, with screenshots taken by `QWidget.grab()`: **real sensor, script-driven, not a visible-desktop check**. The operator's own workbook was only read; every write went to temporary copies.

## Findings

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| G1 | **High** | **Hardware.** A collection started while the previous one's downloads were still queued took those downloads over in memory only. The files finished afterwards were never recorded (no manifest update, no preview). After a restart, the rest were forgotten without a message, and the files stayed on the sensor. A finished collection closed before its downloads completed was not requeued either. | A new collection (and Dismiss) waits for the last one's downloads, checksums and checks. Start shows *The last collection is still downloading (N waiting). Start the next one when the downloads finish.* At the next start, a finished collection with unfinished downloads shows *The application was closed before N downloads finished. The files are on the sensor; Continue downloads them.* Continue checks the identity, then only downloads: nothing is sent and the sensor is not held. Tests `test_a_new_collection_waits_for_the_last_downloads_and_a_restart_offers_them_again`, `test_start_says_it_waits_for_the_last_collections_downloads`. **Hardware:** re-verified (below). |
| G2 | Medium | **Hardware.** A 500 MHz request was recorded by CRFS as 100 MHz around the centre (full length), but the card's tag read **Complete**. | The class stays *completed* (time and data are complete), but the tag reads **Other range**, and the card reads `Recorded: 3500-3600 MHz (100 MHz), not the requested range`. Test `test_a_wider_request_recorded_narrower_says_so`. |
| G3 | Medium | **Hardware.** Every RemoteTimeData reply carries the Node's latitude, longitude and altitude. They reached the diagnostic log and therefore **Copy details**. | Latitude, longitude and altitude are written as `<location>`. Test `test_the_sensor_position_from_a_reply_body_is_never_written_out`. |
| G4 | Low | **Hardware.** While the Excel Collection was paused, the Manual tab still said *Excel Collection is running.* | *Excel Collection is paused. Stop it first.* Test `test_the_manual_tab_says_whether_the_collection_is_running_or_paused`. |
| G5 | Low | **Hardware** (VPN at about 2 MB/s). The backlog pause repeated its reason under the status line: *Paused: Downloads: 10 waiting (limit 8)…* followed by *Downloads: 10 waiting · Downloads: 10 waiting (limit 8)…*. | Said once. Test `test_a_download_backlog_pause_is_said_once`. |
| G6 | Low | Code review: an unusable download folder at start-up put the raw `OSError` text in the activity log. | *The download folder cannot be used. Check Settings.* The detail goes to the diagnostic log. |
| - | Info | **Hardware.** A long 100 MHz request is accepted and echoed with its full duration, but the file holds less. 9 s requested → 1 file, 942,797,376 B, **1.886 s**; in the EXE test, 9 s → 1,097,835,072 B, **2.20 s**. | Reported as **Partial** (*Partial recording: requested 9 s, received 1.89 s.*). The request is never changed, and the row is not retried on its own. |
| - | Info | The Node 100-18 always lists a standing `EMP Sweep [1]` mission. It is not a remote recording and never blocked a request. `Using internal clock because of ncpd.conf settings` repeats for the whole capture (224 times in one 9 s capture). | It is logged once per session in the activity log, and the count goes to the diagnostic log. |
| - | Info | **EXE.** While an Excel row waits for its files, the Manual tab's status line shows the shared controller's *Sensor finished. Waiting for files.* above the manual card. The action bar below says *Excel Collection is running.* | Left as is for 1.13.0 (one controller by design); to be reviewed with the operator. |
| - | Info | Masking of stored secrets is applied to every whole-word occurrence, also inside event names. A credential that is an ordinary word therefore leaves visible `***` marks, and the surrounding text can hint at it. | By design. Recommendation: use a credential that is not a dictionary word. |

## Hardware results (Node 100-18 `rfeye100400`, 3 November 2026, local times)

| Case | Result |
| --- | --- |
| Identity and addresses (19:00-22:35) | **Pass.** Before 21:22 the Node answered on both the direct cable address and the VPN address with the same model, serial, firmware, storage and listing. The direct link disappeared at about 21:22-21:25 (the laptop was undocked), so the campaign used the VPN address (about 146 ms RTT, about 2 MB/s SFTP) after the serial matched. The identity was re-read before every batch. |
| Raw request, 100 MHz × 1 s (19:13, `EmpClient`) | **Pass.** Payload `{"center_frequency": 3350000000, "bandwidth": 100000000, "duration": 1.0, "rate": 1.0, "capture_length": 1.0, "recording_format": "WAVE"}`, HTTP 200. Reply: centre 3.35e9, bandwidth 1e8, duration 1.0, 1 file. The file is 502,825,536 B at 125 MS/s, 1.00565 s. |
| Raw request, 500 MHz × 1 s (19:14) | **Observed.** Payload centre 3550000000, bandwidth **500000000**, duration 1.0, HTTP 200, finished. The reply said **bandwidth 1e8**; the file is 502,825,536 B, 1.00565 s, with meta bandwidth 1e8 at 3.55e9. CRFS recorded only 3500-3600 MHz. |
| M1: Manual Recording, 100 MHz × 1 s | **Pass.** Payload `{"center_frequency": 3550000000, "bandwidth": 100000000, …, "duration": 1.0, "rate": 1.0, "capture_length": 1.0, "recording_format": "WAVE"}`, HTTP 200. Card: *Complete · Requested: 100 MHz for 1 s · Received: 1 file, 1.01 s · Recorded: 3500-3600 MHz (100 MHz)*. Download 254 s (VPN); **View waterfall** and **Open folder** (Explorer on the folder) worked. |
| M2: Manual Recording, 500 MHz request | **G2 found and fixed.** Activity: *CRFS is recording 3500-3600 MHz; requested 3300-3800 MHz.* The file holds 1.0056 s at 100 MHz. |
| M3: series of 10 MHz × 1 s with Pause / Resume / Stop | **Pass.** *Pausing after this recording* → *Series paused.*, and no request while paused. Resume sent the next recording at once. Stop gave *Series stopped after the current recording.*, and no further request. See the incident note for an earlier start of this series that no command explains. |
| E1: Excel Collection, 15 rows × 0.5 s on a copy of the operator's protocol | **Pass** (G4, G5 found). Row 1 was rejected by **simulation** (the request was not sent); rows 2-15 were recorded. While it ran, the Manual tab's Start was disabled with the reason line. Operator Pause/Resume worked, and the backlog auto-pause at 8 waiting downloads triggered and resumed by itself. Pass summary *Completed 14 · Partial 0 · Failed 1 · Uncertain 0*. **Retry failed rows** sent row 1 as *attempt 2* (confirmed), giving *Completed 15 · …*. Rows 2-16 of the copy were filled once with *CRFS 100-18*, the file, 0.503 and the local time; the earlier results block was untouched, with no duplicates. The wide row (3300-3800 MHz) was again recorded as 3500-3600 MHz. |
| E3: 100 MHz × 9 s | **Partial (real).** The reply echoed duration 9.0 with 1 file of 942,797,376 B = 1.886 s. Card and log: *Partial recording: requested 9 s, received 1.89 s.* No split occurred. Not downloaded (VPN speed). G3 was found in this reply. |
| E2: two-row copy scheduled for 22:14 | **The pass started on time** (*Pass 1 started: 2 rows.* at 22:14:00). Its first request was not sent because of a test-harness defect (see the incident note), not the app. The relaunch restored the Excel tab, the form and *Interrupted during a pass: press Continue to finish it*; nothing started by itself, and the unsent row counted as *Not recorded*. After the identity and idle check, **Continue** recorded both rows (0.50 s each), and the copy's rows were filled after a backup. |
| G1 re-verification (22:26-22:34) | **Pass.** With E2's downloads running, Start was disabled with *The last collection is still downloading (2 waiting)…*. The app was closed mid-download and restarted: *The application was closed before 2 downloads finished…* with Continue / Dismiss, and the Manual tab free. Continue downloaded both files with manifests and previews, and Start re-enabled by itself. |
| Sensor clean-up (23:22) | **Pass.** After the identity and idle check, the 30 files this pass created (below) were deleted with the app's SFTP client, by exact name only. The folder went from 61 to 31 files. The other 31 files, recorded from 14:36 to 16:34 before this pass, were not touched. Free space rose from 195.7 GB to 203.1 GB, and the mission list afterwards held only the standing sweep. |

Sensor files created by this pass (Node folder `20261103`, `iq_20261103_<HHMMSS>_0001.wav`; the names carry the Node's UTC time, local time is 2 h later):
- Raw requests: `171321`, `171405`.
- M1: `193155`. M2: `194017`.
- The unexplained series start: `194446`, `194459`, `194515`. M3: `195410`, `195509`.
- E1 (15 files): `195813`, `195826`, `195840`, `195854`, `195931`, `195947`, `200000`, `200014`, `200033`, `200053`, `200109`, `200131`, `200411`, `200427`, `200543`.
- E3: `200818`. E2: `202601`, `202614`.
- EXE test: `210551`, `211441`, `211454`.

All 30 were deleted at 23:22.

## Simulated and offscreen results

| Case | Result |
| --- | --- |
| A rejected row (E1 row 1) | **Simulated** on the real application: the request was refused before sending, as a CRFS rejection would be. The retry path afterwards was real. |
| Tabs, cards, pass summary, recovery notice, theme, scaling | **Offscreen.** Renders at 1100×700 with `QT_SCALE_FACTOR` 1 / 1.25 / 1.5 in light and dark showed no clipped control. Fixed from the renders: the gap left by the hidden manual wait row; the empty status line keeping its height; stretched buttons on the result and pass cards; and a stale *5 s per request* on the Excel action bar. The theme toggle glyph showed correctly. |
| Everything above in the suite | `tests/test_result_view.py`, `test_workflow_tabs.py`, `test_cellular_panel.py`, `test_cellular_runner.py`, `test_operator_messages.py`, `test_capture_measure.py` (new or extended). |

## Incident notes

- **Writes not attributed (20:03-20:06).** Six files changed that this worker's visible record did not show it writing: three new and three edited. Work stopped and was reported. The coordinator found no other agent session; the writes matched this worker's queued plan, made before a context reset. The coordinator decided that this worker owns the tree and that the files are adopted. From then on, the working-tree state (status, sizes, mtimes, hashes) was snapshotted after each edit batch and compared before the tab rework, the campaign, the suite and build, and the commit.
- **Series start no command explains (21:44:44).** The Manual Recording series started three 10 MHz × 1 s recordings. The test driver had issued no command, the session was locked, and no input was recorded. A reproduction attempt did nothing. Those three files are listed among the created files and were deleted with the others. Cause unknown.
- **Test-harness defect (22:11-22:17).** A tracing wrapper installed by the test driver called a helper by a shared name, which a later driver command reused for a file-dialog stub. From 22:11:27, the application's diagnostic calls raised an error: doubled activity lines, no diagnostic lines, and E2's first request was not sent. The sensor was checked idle, and the instance was closed and relaunched. This led to G1.
- **Desktop in use (22:32-22:35).** After the session was unlocked, the test instance's protocol was reloaded once and its Sensor Files window opened twice. The driver had issued no command, and someone was using the desktop at that moment. Nothing was deleted or recorded. The test instance's title was then changed to say it is a test instance.

## Not done / still to verify

- **Visible desktop checks:** not done while the session was locked; after it was unlocked the desktop was in use by someone else. The installer was built and its version resource checked, but it was not installed (an install would replace the application installed on this PC; the installer script changed only in its example comment).
- Multi-part recordings: the Node's file size limit (4095 MB) means no split occurs at these durations. The merge stays covered by simulated tests.
- E3's 942 MB file and the EXE test's 1.1 GB partial file were not downloaded over the VPN (about 2 MB/s).
- Retry after a *real* CRFS rejection: no rejection occurred on the sensor in this pass, so the rejection was simulated (source build only; not in the EXE).
- In the EXE: Pause/Resume of the Excel Collection was not exercised (the two-row pass had finished when Pause was pressed), and the text placed by **Copy details** was not read (the clipboard cannot be read from the test shell). Both are covered by the source-build campaign and the tests.

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 473 collected: **471 pass, 2 skip** (no local copy of the protocol workbook; the Haifa workbook is not on this PC), 0 fail, 200 s, run once on the final code before the build, not overlapping it. After the build, one example address in `test_operator_messages.py` was changed to a documentation address, and that file was re-run (29 pass); the application code did not change. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`, exit 0, 776 s: `dist\CRFS_IQ_Recorder.exe` 57,354,584 B (SHA-256 `72607289…BC2622`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,508,551 B (SHA-256 `F89515B9…87436B`), version resources 1.13.0. Not Authenticode-signed (unchanged); the build repeats that Smart App Control / WDAC publisher rules may reject unsigned files. |
| EXE test against the Node (23:03-23:20) | **Pass, real sensor.** The portable EXE ran with an isolated `%LOCALAPPDATA%` and was driven through Windows UI Automation (no mouse or keyboard input; the session was locked). Screenshots were taken with `PrintWindow`. No Code Integrity event was logged. The EXE restored the tab and form saved by the source build and connected to the Node by itself. **Manual** 100 MHz × 9 s: *Partial*, *Requested: 100 MHz for 9 s · Received: 1 file, 2.20 s · Recorded: 3500-3600 MHz (100 MHz)*. **Open folder** / **View waterfall** were disabled with *Download required.*, and the Excel tab's Start read *Manual recording is running.* **Excel Collection**, two rows × 0.5 s, *Run now*: the confirmation listed both rows; *Completed 2 · Partial 0 · Failed 0 · Uncertain 0*; the copy was updated after a backup. Meanwhile the Manual tab's Start read *Excel Collection is running.* Start then waited with *The last collection is still downloading (2 waiting)…* until both files were saved. **View waterfall** opened the waterfall of the downloaded file, and **Open folder** opened Explorer on the configured download folder. **Close and relaunch:** the Excel tab, *Run now*, the changed 3 s wait, the Manual form with the changed 2 s, and the pass card were restored. **Copy details** reported *Diagnostic details copied.* (text not read, see above); the EXE's diagnostic log shows `<location>` for the position in real replies. |
| Git | Branch `cursor/polish-gui-easy-launch`, annotated tag `v1.13.0`, pushed to `personal`; GitHub Release `v1.13.0` with both files. 1.12.0 and older releases kept. |

---

# Part F - End-to-end pass on the Node 100-18: recordings, SFTP, Cellular Collection with Excel, faults, executables (3 November 2026, Recorder 1.12.0)

## Scope

Everything the operator uses, run in order against the real sensor with a test-data budget of 10 GB: the manual recording form in both frequency modes and several bandwidths and durations, repeats with delays, Stop during the countdown and during a recording, the SFTP browser (navigation, refresh, sorting, grouping, parallel downloads with checksums, a cancelled download, the waterfall preview), the Cellular Collection on temporary **copies** of the operator's protocol workbook with Excel closed, open (unsaved edit), locked by another program, and through close / restart, pause / resume / stop and a missed scheduled pass; connection faults (port refused, black-hole host); the responsiveness of the main window during all of it; the four presentation observations the coordinator made on 1.11.0; the full suite; the EXE and the installer. Sensor identity was read before every hardware batch and the mission list plus a 3 s file-growth check preceded every Start; the Node's configuration was not changed. **Hardware** rows below ran on **Node 100-18, model `R100-18`, serial `rfeye100400`, firmware `2.25-325`** (storage 242.9 GB, 213.7 GB free at the start). The desktop session was locked for the whole pass, so every GUI check drove the real `MainWindow` offscreen (`QT_QPA_PLATFORM=offscreen`) against the real sensor or the demo transport; nothing was clicked on the visible desktop. The operator's own workbook, its `Round 2026-11-02 18-44` sheet and its earlier backup were not opened for writing.

## Findings

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| F1 | Medium | **Hardware.** The size estimate was not recomputed when the sensor identity arrived. The saved plan is restored before the sensor answers, so a plan above 50 MHz showed the generic 62.5 MS/s figure: 100 MHz × 1 s displayed **251.5 MB**, the Node wrote **502.8 MB** (125 MS/s). | `_apply_sensor_info` refreshes the estimate (keeping the status message) when the model changes. Test `test_gui_estimate_follows_the_model_once_the_identity_arrives`. |
| F2 | Low | Coordinator observation 4: ~85 px of empty space under **Start times** - the chip host had a *Minimum* vertical policy and reserved a row per chip even when they fitted on one line. | Chip host *Expanding*. Test `test_times_field_does_not_reserve_a_row_per_chip_when_they_fit_on_one`. |
| F3 | Low | Coordinator observation 2: the action bar docked under the form showed a second, unlabeled **Start** that could be read as a second *Start Recording*. | The bar carries the caption **CELLULAR COLLECTION**; Start's accessible name and tooltip read *Start Cellular Collection*. Test extended: `test_collapsed_panel_builds_nothing_until_opened_and_opens_for_a_saved_session`. |
| F4 | Medium | **Hardware.** Cancelling a download in the SFTP browser was reported as a *failed* download (error popup) and left the `.part` file: on the closed socket paramiko raises its own `SFTPError`, which the cancel path did not recognise. | Cancel is recognised whatever the exception type; the partial file is removed; the summary counts it as cancelled. Tests `test_paramiko_browser_cancel_reports_cancelled_and_leaves_no_part_file`, `test_cancelled_download_is_counted_as_cancelled_not_failed`. **Hardware:** re-verified, no `.part` left. |
| F5 | **High** | **Hardware.** A collection closed while it was *waiting* for a scheduled pass came back after the restart as a bare *Interrupted: check with the sensor before continuing* although nothing had been in flight; the pass whose time had gone by stayed *pending* with no Run / Skip offer; and **Continue started that pass late on its own** (10:50 pass started at 10:53) because the 15-minute grace of the running scheduler was applied to a closed application. | `_mark_missed_while_closed` runs at load and in `resume_after_restart` (no grace: a pass whose time passed while the app was closed is *missed*); the recovery notice says what happened (*closed while waiting for its next pass* / *closed during a pass* / *check with the sensor* only when a capture is really uncertain); **Run the missed pass now** works straight from the interrupted session (identity check, reservation, requeued downloads) and is refused while an uncertain capture still needs reconciliation. Tests `test_restart_after_a_scheduled_time_passed_lists_the_pass_as_missed_and_continue_never_runs_it`, `test_missed_pass_can_be_run_or_skipped_straight_from_the_interrupted_session`, `test_a_missed_pass_is_not_run_while_an_interrupted_pass_still_needs_reconciliation`, `test_panel_offers_run_or_skip_for_a_pass_missed_while_the_app_was_closed`. **Hardware:** re-run below. |
| P1 | Polish | The badge read **Finished** after the operator stopped the collection and after *Finished with problems*. | **Stopped** (amber) after a stop; amber *Finished* when a pass had problems. Test `test_chip_says_stopped_when_the_operator_stopped_the_collection`. |
| P2 | Polish | **Hardware.** With the workbook held open by another program the log line read `[WinError 5] Access is denied: '<temp file>' -> '<file>'`. | *Could not save <file>: another program has the file open (it is written when the file is free).* Test `test_a_share_deny_write_handle_is_busy_and_the_log_text_names_no_temp_file` (real share-deny-write handle). |
| - | Info | `WARNING: Using internal clock because of ncpd.conf settings` (Part D1): **root cause confirmed on the Node** - its `ncpd.conf` sets `real_time_clock` and `reference_clock` to `internal`; the Node repeats the line 5-30× per second for the whole capture (31× in 2 s, 180× in 6 s). It is the sensor's own configuration, not something the app sends. | Nothing to fix in the app; the first log line now carries the explanation (*the Node is configured to use its internal clock; timestamps are not GPS-disciplined*). Test `test_internal_clock_warning_carries_its_explanation`. |
| - | Info | `ERROR: Unable to schedule the command` (Part D2) did **not** occur in the 28 captures of this pass. The app now releases the stream as soon as the files are confirmed (7-41 s after Start), before the ~45 s mark where 1.10.0 saw it. | Handling of a late error after *finished* unchanged (`test_error_after_finished_keeps_the_recording_confirmed`). Observed on hardware: the POST is sent once; a refused or timed-out connection is reported and **never re-sent**. |
| - | Info | Coordinator observation 1 (blank strip between the cards) was **not reproduced** with the committed 1.11.0 code in four offscreen states; the renders it was seen on were taken from pre-bump code (they read *v1.10.0*). Observation 3 was a grab artefact (full-window renders are fine). | No change. |

## Hardware results (Node 100-18 `rfeye100400`, 3 November 2026, local times; identity verified before each batch)

| Case | Result |
| --- | --- |
| Identity, mission list, storage (09:54, read-only) | **Pass.** `R100-18` / `rfeye100400` / `2.25-325`, *Connected* in 1.2 s; `GET /emp/missions` listed only the Node's standing `EMP Sweep [1]` (not a remote recording; it never blocked a request); 242.9 GB / 213.7 GB free; today's remote folder did not exist yet. |
| Manual recording, centre / bandwidth 1842.5 MHz × 12.5 MHz × 2 s (10:11) | **Pass.** HTTP 200, file named 1.3 s after Start, *Time elapsed* → *Sensor finished. Confirming …* → **Recording finished (1 file)** after 7.0 s, 125.7 MB, Start button back to *Start Recording*, *Recordings completed: 1 · Files found: 1*, no leaked worker. |
| Manual recording, start / end 1805-1830 MHz × 2 s | **Pass.** 251.4 MB (31.25 MS/s), confirmed in 7.0 s. |
| 25 MHz × 6 s (expected a split) | **Pass, one file.** 750,047,812 B, confirmed 41 s after Start (the Node flushes 33 s after *finished*; the watch kept waiting and did not end on *Time elapsed*). The Node's `filesize` is 4095 MB, so no multi-part file can be produced on this sensor without changing its configuration (not done). |
| Series of 3 × (12.5 MHz × 1 s), wait 3 s | **Pass.** Countdown *Next recording in 00:03 … 01*, three files, *Series complete.*, completed 3 / found 3 in 24 s. |
| Stop during the countdown; Stop during a recording | **Pass.** *Stopped. The next recording was cancelled.* (one file, Start re-enabled); *Stopping after this recording.* → file confirmed → *Series stopped after the current recording.*, no second request sent. |
| 50 MHz × 1 s; 100 MHz × 1 s | **Pass** (and F1 found). 251.4 MB at 62.5 MS/s; 502.8 MB at 125 MS/s confirmed in 28 s. |
| SFTP browser (10:2x) | **Pass.** Today's folder listed in 2.3 s, navigation 0.5 s, refresh 0.6 s; sorting by name / size / date in both directions correct; `_0001` parts grouped; three parallel downloads verified by SHA-256 against `sha256sum` on the Node (≈4 MB/s aggregate, sensor-limited); waterfall preview of a real file in ≈6 s; cancelled download → F4 found, fixed and re-verified. |
| Cellular session B (10:31) - two-row copy, *Run now*, 2 s, wait 3 s, 2 passes, Excel closed | **Pass.** 6 captures (the 75 MHz row became two sequential windows), block 0 filled in place (rows 2-3), block 2 appended (rows 4-5), `0.000` and `dd/mm/yyyy hh:mm:ss` formats, one backup, manual `J2` note kept, the `Round 2026-11-02 18-44` sheet untouched, manifests, quality, previews and handover written, all 6 downloads verified. |
| Session B2 (10:46-10:53) - workbook open in Excel with an unsaved `K3` edit; app closed while waiting for the 10:50 pass, restarted, *Continue* | **F5 found.** *Continue* started the 10:50 pass at 10:53 on its own. The rest passed: the two rows were written **through the open Excel window** (COM), rows 6-7, Excel's own unsaved `K3` edit saved with them, `J2` kept, downloads verified. |
| F5 re-run (11:04-11:10) - pass scheduled 11:06, app closed at 11:04 while *Next pass in 58 seconds*, restarted 11:06:28 | **Pass after the fix.** Log *Missed pass: the one scheduled for 11:06 did not start*; status *Interrupted: a scheduled pass was missed - run it now or skip it*; recovery notice without *check with the sensor*; Run / Skip offered; **nothing started for 10 s**; *Run the missed pass now* → 2 captures, written through COM, rows 8-9, downloads verified, no leaked worker. |
| Session C (11:11-11:16) - Excel closed, workbook held by a share-deny-write handle; app closed with results pending; restarted with the lock still held; lock released | **Pass** (and P2 found). First flush: backup written, save refused → *Excel update pending … busy*, retried every 15 s, session finished with 2 pending; after the restart still 2 pending; 40 s after the release **one** flush wrote rows 10-11, no duplicate, no second backup. |
| Session D (11:17-11:21) - 1 s, wait 15 s, 2 passes; Pause during the wait, Resume, Stop during capture 2 | **Pass** (and P1 found). *Paused* with the manual Start held and the workbook written while paused; Resume restarts the 15 s wait; Stop → question box → *Stopping after this recording* → capture confirmed and logged → *Pass 1 stopped: 2 of 2*, pass 2 never started, rows 12-13, downloads verified. |
| Operator's full 15-row protocol (copy) | **Import and plan only.** Imports in 0.8 s with 0 issues; plan at 1 s = 19 captures / 7.5 GB (n78 as 5 windows), at 2 s = 30 captures / 15.1 GB. Not recorded (budget). |
| Sensor clean-up and the free-space label (12:00) | **Pass.** Identity re-read, no remote recording listed, today's folder held exactly the 24 files below. Selected in the SFTP browser and deleted with the app's own *Delete*: 24 × *Deleted … from the sensor.* in 5.1 s, the folder re-listed empty, and the main window's storage label went from *191.2 GB free / 226.3 GB* to *199 GB free / 226.3 GB* (+8,355 MB) within 1.4 s without a manual refresh; no leaked worker. |

Budget: 24 captures, ≈9.1 GB written on the Node (manual path 2.3 GB, Cellular path 6.8 GB), ≈6.9 GB downloaded locally, all under 10 GB. Sensor files created by this pass (all under the Node's `20261103` folder, names `iq_20261103_<HHMMSS>_0001.wav`): 081147, 081220, 081248, 081523, 081532, 081541, 081607, 081632, 081702, 081721, 083140, 083156, 083233, 083312, 083351, 083431, 085319, 085330, 090647, 090659, 091144, 091155, 091719, 091746 - **24 files, all deleted at the end of the pass through the app's SFTP delete; nothing else on the Node was touched.**

## Simulated and offscreen results

| Case | Result |
| --- | --- |
| Connection refused (nothing listening) and black-hole host (connect timeout) during Start and *Sensor Files* | **Pass.** Status *Reconnecting…* then *Disconnected*; Start makes **one** attempt and reports *the sensor refused the connection … not retried* / *Connecting … timed out … likely not transmitted … not retried*, Start re-enabled after 2.1 s / 21 s; the SFTP window reports *SFTP error.* within 3.4 s / 8.1 s; no UI-thread stall over 100 ms after the first paint; no leaked worker. Note (unchanged behaviour): while the request is still connecting the form shows *Recording N s.* / *Time elapsed. Waiting for files…* until the error arrives. |
| Delayed files, interrupted downloads, restart with background work, failed backup, low disk space | **Pass** in the suite: `test_downloads_start_from_a_fresh_listing_that_picks_up_late_files_and_final_sizes`, `test_restart_recovers_an_interrupted_session_without_resending`, backup-failure and lock tests in `test_cellular_workbook.py`, `test_insufficient_disk_space_stops_before_writing`, `test_recording_blocked_when_free_space_is_too_small`; on hardware the 33 s late file and the pending-results restart (Session C) covered the real paths. |
| Multi-part files: grouping, several downloads, merge of compatible parts | **Simulated only** (demo transport and synthetic parts: `test_iq_merge.py`, `test_merge_gui.py`, `test_merge_controller.py`). The Node cannot split at 4095 MB within the 625 MB per-capture limit. |
| Main window, SFTP browser, Settings in both themes at 1100×760 and larger; panel collapsed / expanded; schedule mode; pickers | **Pass** offscreen (renders kept for the operator: the action bar in light and dark, the SFTP browser). F2's empty space gone; the caption on the action bar; no clipped control. |
| Close with workers in flight | **Pass.** Every harness run ended with `stop_worker_threads` reporting 0 leaked threads, including the refused / timed-out connection runs. |
| Visible desktop: taskbar icon, real DPI, clicks on the EXE | **Not done:** the session stayed locked at every probe (start, before each batch, before the build, after it). |

## Timings (same machine, offscreen, under IDE load)

| Measure | Value |
| --- | --- |
| Package import | ≈1.0 s |
| `MainWindow` construction (panel expanded, workbook loaded), first frame | 0.45-0.50 s, 0.07-0.10 s |
| Theme switch (expanded panel, 1100×760) | 0.44-0.58 s when closed windows are released as the app does; the 1.9 s first seen was dead windows kept alive by the probe |
| Portable EXE start → main window built | 7.2-7.9 s (onefile extraction + imports on a loaded machine; the installer's program avoids the extraction) |
| Full suite | 109.6 s for 409 tests (58.9 s at the baseline run before the new hardware-style tests and with less load) |

## Not done / still to verify

- Multi-part recordings on this sensor (see above) and therefore the merge on real parts.
- The full 15-row protocol end to end on the sensor (import and plan verified; the two-row copy recorded through every path).
- Visible-desktop checks and the installer itself (`PrivilegesRequired=admin`; cannot be installed from a locked session) - the installer's program was run instead.
- Daily repeat across midnight remains covered by the simulated checks only.

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 409 collected: **407 pass, 2 skip** (live-sensor tests), 0 fail, 109.6 s, run once before the build, not overlapping it. New or extended: `test_gui_smoke.py` (F1, warning note), `test_cellular_pickers.py` (F2), `test_cellular_panel.py` (F3, F5, P1), `test_sftp_ui_responsiveness.py` (F4), `test_cellular_runner.py` (F5 ×3), `test_cellular_workbook.py` (P2), `test_emp_stream.py` (warning note). |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`: `dist\CRFS_IQ_Recorder.exe` 57,199,813 B (SHA-256 `3995A59A…F0DE0`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,370,910 B (SHA-256 `00928AA1…CBB7B`), version resources 1.12.0. Not Authenticode-signed (unchanged). 570 s wall on the loaded machine. |
| EXE smoke test | Both the **portable EXE** (parent + onefile child) and the **installer's program** (`dist\onedir\…\CRFS_IQ_Recorder.exe`) started headlessly with an isolated `%LOCALAPPDATA%`, were alive after 30 s with the settings, cache and `cellular` folders created, printed nothing to stderr, and were stopped with no leftover process. Unlike Parts D and E, Smart App Control (still on, not changed) blocked neither artefact this time - no Code Integrity 3033 / 3077 / 3118 event was logged. |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.12.0`, pushed to `personal`; GitHub Release `v1.12.0` with both files. 1.11.0 and older releases kept. |

---

# Part E - GUI polish and the construction / theme-switch slowness (3 November 2026, Recorder 1.11.0)

## Scope

A presentation pass on the Recorder: the collapsible Cellular Collection panel, the main form, the SFTP browser and Settings were brought to one grid (36 px controls, 40 px primary button, 150 px label column, one spacing scale, one typography scale) in both themes, and the window-construction and theme-switch times measured in Part D were investigated and fixed. Recording, Excel and scheduling code was not changed (`cellular_runner`, `cellular_workbook`, `cellular_protocol`, `emp_client`, `emp_stream`, `sftp_*`, `recorder` untouched); only `theme.py`, `cellular_pickers.py`, `cellular_panel.py`, `gui.py`, two test files, the version, the installer version line and the docs changed. **Simulated** throughout: the sensor was not contacted and nothing was recorded. The desktop session was locked for the whole pass, so every GUI check ran offscreen (`QT_QPA_PLATFORM=offscreen`) with renders saved as PNG; the before pictures come from the unchanged 1.10.0 source, the after pictures from the same scenes with 1.11.0.

## What changed

| Area | Before (1.10.0) | After (1.11.0) |
| --- | --- | --- |
| Panel header | Badge read **Off** while the form was visible; enabled/disabled and expanded/collapsed were one state. | Badge is the collection state only (*Idle, Ready, Scheduled, Waiting, Recording, Pausing, Paused, Stopping, Interrupted, Finished*); the header toggle only shows or hides the form. |
| Workflow order and grid | A small recording-duration box beside 48 px controls; labels of different widths; wrapped rows. | Workbook + Browse, Worksheet → Recording duration + Wait → *Run now / Schedule* (date, start times and Repeat daily appear only for Schedule) → actions. All fields 36 px on a 150 px label column. |
| Start times | Times in one line; removal through a popup. | **Add time** beside the chip list; each chip has its own × (accessible name *Remove HH:MM*); flow layout wraps without clipping. |
| Pickers | Calendar cells rendered 19 px tall (stylesheet overrode the fixed size), empty space in the popup; faint arrows. | 36 px day cells, 30 px navigation buttons, visible arrows and glyphs; the Windows regional date and 12/24 h format kept. |
| Indicators and icons | Radio and check indicators invisible in the dark theme; faint combo arrows. | Generated PNG glyphs (check, radio dot, arrows, cross, plus, grip) coloured from the theme palette, cached, drawn in both themes. |
| Action bar | Preview / Start / Pause / Resume / Stop of mixed heights inside the scrolling form; Stop label long. | One aligned bar (`Preview`, primary **Start**, `Pause`/`Resume`, short **Stop** with tooltip *Stops after the current recording*) docked under the form so it stays reachable while the form scrolls. |
| Status text | A technical paragraph under the form. | `15 rows · 65 captures · Estimated 40.9 GB` (+ `N problems (see Preview)`); the full text behind **Details**; warnings stay visible and short. |
| Layout and log | Nested borders, empty frames, two controls that hid the log. | One card per area, no nested borders; the activity log is resizable (dotted splitter grip) and collapsible with **one** control; the splitter remembers the last size. |
| Theme button, Settings, SFTP browser | Mixed control heights. | Same 36 px grid, same checkbox and arrow glyphs, both themes. |

## Checks

| Case | How | Result |
| --- | --- | --- |
| Main window, light and dark, 1100×700 and larger; panel collapsed and expanded; schedule mode; running state | Offscreen renders of the real `MainWindow` (demo transport, isolated settings, a copy of the protocol workbook) | **Pass.** No clipped control, no needless scrollbar, no awkward wrap at 1100×700 with the log hidden; dark theme indicators visible. |
| Calendar, time picker, Preview dialog | Offscreen renders of the popups and the dialog | **Pass.** 320×367 calendar with 36 px cells; Preview without the focus frame and horizontal scrollbar it had. |
| 125 % and 150 % scaling | `QT_SCALE_FACTOR=1.25 / 1.5` with `QT_ENABLE_HIGHDPI_SCALING=1`, offscreen | **Pass** for layout (geometry stays on the logical grid; glyphs are 4× images that only scale down). Pixel rendering at real DPI not seen on the desktop (locked). |
| Settings dialog and SFTP browser | Offscreen renders, both themes | **Pass.** Consistent with the main form. |
| Recording / Excel / scheduling behaviour | Full suite | **Pass.** Unchanged modules, unchanged tests. |
| Desktop (visible) check of the EXE | - | **Not done:** the Windows session stayed locked; the renders above are the evidence. Worth a short look by the operator at 100 % and at the display's own scaling. |

## Timings (same machine, offscreen, same probe scripts against the 1.10.0 sources exported from Git and the 1.11.0 tree)

| Measure | 1.10.0 | 1.11.0 | Cause of the difference |
| --- | --- | --- | --- |
| `MainWindow` construction (panel expanded, workbook loaded) | 0.65-0.94 s | 0.65-0.72 s | Twelve per-widget stylesheets on combo popups (0.14 s) removed; palette only. |
| Theme switch (light → dark) | 0.23-0.45 s | 0.20-0.31 s | `setStyle("Fusion")` was re-run on every toggle; identical sheet now returns early; glyph icons cached. The remaining floor is Qt re-polishing every widget. |
| Start to first frame (`python -m crfs_iq_recorder`, saved workbook, panel expanded) | 2.69-2.75 s | 2.61-2.69 s | Dominated by interpreter and PySide6 import; unchanged. |
| GUI test modules (65 tests) | 1.10.0 code: `test_gui_smoke` 48 tests in 74 s, `test_theme_toggle_persists` 5.4 s; the new panel on the old harness: 142 s and 28 s | 42 s for the 65 GUI tests; the theme test is no longer among the ten slowest of the suite (every test outside that list took under 1.1 s) | **The slowness measured in Part D was in the test harness, not the app:** closed `MainWindow`s stayed alive (lambda reference cycles), so every theme change re-polished hundreds of dead widgets. `conftest.py` now releases closed windows after each test. |

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 398 collected: **396 pass, 2 skip** (live-sensor tests), 0 fail, in 81 s, run once, not overlapping the build. `test_cellular_panel.py` asserts the new summary text, the *Details* control, the *Ready* / *Idle* badge texts and the hidden action bar when collapsed. `compileall` clean; no unused import or undefined name in the edited modules. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`: `dist\CRFS_IQ_Recorder.exe` 57,196,487 B (SHA-256 `AD7F03FB…BCD4D`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,367,747 B (SHA-256 `B3AD00F8…D0EB4`), version resources 1.11.0. Not Authenticode-signed (unchanged). The script now starts PyInstaller through `python.exe -m PyInstaller`: the `pyinstaller.exe` launcher stub was refused by this PC's application-control policy (*blocked by your organization's Device Guard policy*); the build itself is the same. |
| EXE smoke test | The **portable EXE** started headlessly (`QT_QPA_PLATFORM=offscreen`, isolated `%LOCALAPPDATA%`, `--demo`): parent and child alive after 25 s, the app's settings, cache and `cellular` folders created (the panel was built), then stopped. The **Setup installer and the installer's program** (`dist\onedir\…\CRFS_IQ_Recorder.exe`) **could not be started on this PC**: Smart App Control refused both (*An Application Control policy has blocked this file*, Code Integrity events 3033/3077/3118) - the mirror image of Part D, where the onedir program ran and the portable EXE was refused. This is the reputation verdict for new unsigned binaries, not a fault in the program; the installer compiled normally and its version resource reads 1.11.0. Smart App Control was not changed. If a file is blocked on the operator's PC, try the other artifact or sign the release. |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.11.0`, pushed to `personal`; GitHub Release `v1.11.0` with both files. 1.10.0 and older releases kept. |

---

# Part D - Simplified Cellular Collection and the reliability fixes (2 November 2026, Recorder 1.10.0)

## Scope

The operator asked for the Cellular Collection to follow the workflow of the protocol workbook and nothing more: the rows of the selected worksheet are the recording plan (band, direction, duplex, start, stop), the duration is chosen in the app, every row is recorded with a wait in between, the same sequence is repeated at other hours, and each pass fills the workbook's own nine columns (first pass in the protocol rows themselves, later passes as appended blocks of the same rows). The per-round worksheets, the extra Window / Status / Notes / Round columns, the UTC/local selector and the campaign, site, visit and scenario controls of 1.9.x were removed (`cellular_protocol_results.py` deleted). Date and time are chosen with a calendar and a time picker in the Windows regional format; time-zone handling stays internal. The four faults of the 18:44 run reviewed before this pass were fixed with reproducing tests, and the hardware checks below found three more.

All workbook work ran on temporary **copies** of `Cellular_IQ_Collection_Protocol.xlsx` under `%TEMP%` (the original and its earlier `Round 2026-11-02 18-44` sheet and backup were not touched). Sensor work was read-only probes and short disposable recordings; no sensor file was deleted and the desktop was not driven. The sensor's identity was read before every hardware step: **Node 100-18, model `R100-18`, serial `rfeye100400`, firmware `2.25-325`**; the capability figures below are tied to that serial, and the app refuses to start or resume a collection when the connected sensor is not the one the plan was made for.

## Findings

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| D1 | High | In the 18:44 run the Node streamed `WARNING: Using internal clock because of ncpd.conf settings` several times a second (61 copies per 1 s capture, 151 per 5 s, 307 per 15 s measured here). The 50-message window filled with copies, the `ERROR: Unable to schedule the command` that really ended the stream was no longer visible, and the row was reported as failed because of the warning. | `EmpClient.start_recording` keeps distinct messages with a repeat count (`… (×61)`); a WARNING is never fatal; only ERROR / FATAL / CRITICAL ends a recording. Log line reads `Sensor warning: …`. Tests `test_repeated_warning_is_counted_once_and_never_hides_the_real_error`, `test_warning_only_stream_finishes_normally`. **Hardware:** every 1.10.0 capture logged one `Sensor repeated 61×: Using internal clock…` line and finished normally. |
| D2 | High | `ERROR: Unable to schedule the command` arrived ~45 s after `Remote recording is now finished` while the GUI still held the SSE stream; the capture was then treated as failed although its file was complete. | "Finished wins": a late error is logged and the files decide; the GUI releases the stream as soon as the files are confirmed. Test `test_error_after_finished_keeps_the_recording_confirmed`. |
| D3 | Medium | A closed SFTP watch session listed as an empty folder, so files that existed were "not found". | `list_watch_entries` raises when the session is dead or every folder fails (a missing date folder is still skipped); the watch reconnects and says so once. Tests `test_dead_watch_session_raises_instead_of_looking_like_an_empty_folder`, `test_paramiko_browser_reports_a_dead_transport_and_distinguishes_missing_folders`. |
| D4 | High | The POST sent 2 s after the previous stream closed was refused: the Node keeps the remote-recording mission listed until the client disconnects (**hardware probe:** the mission stayed in `GET /emp/missions` for 9 s after the client released the stream and disappeared only when the stream had ended). The refusal named no file and reported no *finished*, yet 1.9.1 retried on a timer. | Before **every** request the runner reads the mission list (`EmpClient.list_missions`, read-only GET) and waits until no remote recording is listed (bounded, then pause with the reason). A refusal that named no file and never finished is logged as *nothing was recorded*, counted as not recorded and re-sent **once** after the list shows the sensor idle - never on a timer alone. Tests `test_refused_request_names_no_file_and_is_not_finished`, `test_list_missions_is_a_read_only_get`, `test_every_request_waits_for_the_mission_list_and_a_refusal_is_sent_once_more`, `test_a_sensor_that_still_lists_a_mission_delays_the_request_then_pauses`. **Hardware:** 15 of 15 captures in the three sessions below were preceded by the idle check and none was refused. |
| D5 | Medium | **New, hardware session 2:** `ERROR: Collection was modified; enumeration operation may not execute.` arrived 0.2 s after `Sensor is writing iq_20261102_202602_0001.wav`; the Node left a 58,663,488-byte file (of the expected 251,413,056) and the row was given up at once because "a file was named". | `cellular_runner.failed_before_finished`: a sensor error **before** *finished* with no confirmed file is re-sent once after the idle check; the note names the partial file, which is neither counted nor deleted. An error after a confirmed file is never re-sent. Test extended in `test_every_request_waits_for_the_mission_list_and_a_refusal_is_sent_once_more`. |
| D6 | High | **New, hardware session 3:** when the workbook could not be opened at Reload / Start (held by another process), the worksheet list came back empty, the session silently ran with the workbook updates **off** and the three recordings were never written. | `cellular_protocol.WorkbookUnavailable` marks "the file cannot be opened at all"; the panel then keeps the rows imported earlier from that file, keeps the worksheet selected, says so, and the results are queued; any other import problem still drops the plan. The runner logs once at Start when the workbook is not updated this session. Test `test_a_workbook_that_cannot_be_read_keeps_the_stored_import_and_queues_results`. **Hardware:** second run of session 3 queued and later wrote all three rows. |
| D7 | Medium | **New, hardware session 3:** a lock violation while copying the backup (`WinError 33`) was reported as a backup *error*; the results stayed queued but nothing retried them until a restart. | A sharing or lock violation is a *busy* state (retried every 15 s); any other write error is retried every 60 s while results are pending (the queue is on disk and every write reconciles, so a retry never duplicates); each distinct error is logged once. Test `test_a_file_lock_held_by_another_program_is_busy_not_a_backup_failure` (real byte-range lock). |
| D8 | Low | Before the identity was read, the preview note read `—: up to 20 MHz per capture…`; a backup was made once per workbook instead of once per collection; the demo sensor's measured seconds were empty. | Note reads *Sensor not verified yet: …*; `WorkbookQueue.forget_backup` at session start (one backup per collection, as in 1.9.1); demo falls back to the requested seconds. |

## Hardware results (Node 100-18 `rfeye100400`, 2 November 2026, local times)

| Case | Result |
| --- | --- |
| Capability probe, 19:29-19:42 (12 disposable captures at 1842.5 MHz, 12.5-100 MHz, 2-15 s) | **Pass.** Sample rate = 125 MS/s ÷ 2ⁿ, the first ≥ 1.25 × bandwidth (12.5 MHz → 15.625 MS/s, 20 and 25 → 31.25, 50 → 62.5, 100 → 125). Files were complete up to ≈1.0 GB (100 MHz × 2 s held 2.003 s) but **cut short** at 100 MHz × 3 s (held 2.204 s of 3, 1.10 GB) and 25 MHz × 15 s (held 10.36 s of 15, 1.29 GB). The app therefore keeps each capture within 625 MB, the size that was complete in every run: 100 MHz up to 1.25 s, 50 MHz to 2.5 s, 25 MHz to 5 s, 12.5 MHz to 10 s, 6.25 MHz to 20 s, 3.125 MHz to 40 s; wider rows become sequential windows, announced in the preview. The sensor holds 0.56 % longer than requested at 31.25 / 62.5 / 15.625 MS/s (1.0056) and 0.1 % at 125 MS/s. |
| Mission list probe, 19:46 | **Pass.** `GET /emp/missions` parsed; `EMP Remote Recording [Center:1.842500 GHz,Span:25.000 MHz]` listed during the capture, kept for 9 s after the client released the stream, gone once the stream ended. |
| Session 1, 22:10 - run now, 2 passes, 1.0 s, wait 3 s, downloads with SHA-256, trimmed copy (rows 700 DL 753-803, 700 UL 698-748, 800 DL 791-821) | **Pass.** 6 of 6 captures confirmed (idle check before each POST, none refused); 62.5 MS/s, 251,413,056 B each, held 1.006 s; downloaded, checksums verified, *files checked, no problems found*. Workbook: pass 1 filled rows 2-4 in place, pass 2 appended rows 5-7 with the key cells and formats copied; `H` = `1.006` (format `0.000`), `I` = local datetime (`22:10:42` local for the sensor's `201042` UTC stem, format `dd/mm/yyyy hh:mm:ss`), Sensor `CRFS 100-18`; table `A1:I7`, three validations and the frozen pane kept; one backup. Progress line `700 MHz (B68 / B28) DL - row 1 of 3 - recording` / `Next recording in 3 seconds`. 13 min, mostly the six downloads. |
| Session 2, 22:25 - workbook open in a (hidden) Excel instance with an unsaved edit in `K2`; manual edits made between sessions (`J2` note, `H3` = 99.5, `A2` text changed) | **Pass with one sensor fault.** Excel detected (*written through the open Excel window*), block appended at rows 8-10, the unsaved `K2` edit and all manual edits kept, table `A1:I10`. The 700 UL capture failed with D5 (`Collection was modified…`): its row 9 stayed empty, the log said *Pass 1 partial: 2 of 3 protocol row(s) complete, 1 failed* and *Incomplete coverage this pass: 700 MHz (B68 / B28) UL (not recorded)*, state *Finished with problems*. The truncated file stays on the sensor. |
| Session 3, 22:50 - start time chosen in the pickers (22:52, local), workbook byte-locked by another process for the whole session, app closed with results pending and restarted with the lock still held | **Pass after D6/D7.** First attempt exposed D6 (results silently off) and D7 (no retry). Second attempt: *Next pass in 112 seconds*, first recording at **22:52:00** exactly; 3 of 3 confirmed; indicator *1 → 3 Excel update(s) pending*; *Finished … 3 Excel update(s) still pending; they are written when the workbook is free (also after a restart)*; after the restart the panel showed *3 Excel update(s) pending* and the queue on disk held 3. |
| Recovery, 22:58 - app started again with the workbook free | **Pass.** Start-up flush wrote the three rows as a new block at rows 11-13 (`iq_20261102_205202/205215/205228`, `1.006`, `22:52:02` …), new per-collection backup `…crfs-backup-20261102-225842.xlsx`, indicator cleared, table `A1:I13`. |

## Simulated results

| Case | Result |
| --- | --- |
| Import of the operator's workbook copy; plan from the stored rows only | **Pass.** 15 rows, worksheet order kept (UL and DL as listed), the app duration applies to every row; appended result blocks are recognised by key and never become instructions (`protocol_version()` hashes the plan rows only). |
| Worksheet writer | **Pass.** Block 0 in place, blocks appended, blocks re-located by key after edits and inserted rows, `0.000` and `dd/mm/yyyy hh:mm:ss` formats, table resize, real-protocol copy round trip with the *Reference* image, validations and the earlier round sheet intact; size-split parts grouped one per line in the row's cell; queue persistence across a restart without duplicates; backup failure keeps results queued; COM path (mocked); byte-range lock → busy → written after unlock. |
| Runner | **Pass.** Idle check before every request, refusal re-sent once, error after a named file re-sent once with the partial file noted, error after a confirmed file never re-sent, mission still listed → delayed then paused, identity mismatch refused, repeat count and until-stopped, timed passes capped, sleep gap and missed pass messages, protocol rows filled then appended, pending results survive a restart and a locked file, automatic start-up flush. |
| Stream, watch, pickers, panel | **Pass.** Repeated warnings counted once, finished wins, refusal recognised, dead watch session; pickers follow the system locale (12/24 h, date order), wheels and presets; panel end to end on the demo transport: import, preview, identity check before Start, schedule from the pickers on the local clock, protocol rows filled in place, pending indicator with no session, collapsed panel builds nothing until opened, workbook unreadable → kept import and queued results. |

## Not done / still to verify

- The full 15-row protocol was not recorded end to end on the sensor (the trimmed 3-row copy was); its import and plan were verified on the copy, and the writer on a real copy in the unit tests.
- Size-split files (several parts of one recording) were grouped only in the simulated checks; at 1 s the Node 100-18 wrote single files.
- Daily repeat, the missed-pass message after sleep, and the automatic retry after a lock is released were verified in the simulated checks; on hardware the pending results were written through the restart path.
- The UI was driven offscreen, not on the desktop; the 1.9.1 EXE kept running on the desktop throughout.

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 398 collected: a full run (52 min, not overlapping the build) gave **395 pass, 2 fail, 1 skip** (live-sensor test). Both failures were in `test_cellular_runner.py` and were fixed: two collections started within the same second produced the same backup file name, so the second copy overwrote the first (`ensure_backup` now adds `-2`, `-3`, … - a real defect caught by the per-collection backup assertion); and a test assumed the runner's 500 ms tick would not fire during a short event pump (it does under load; the test now stops the timer). The two modules were re-run green afterwards (43 pass). Added or rewritten in this part: `test_cellular_workbook.py` (16), `test_cellular_pickers.py` (9), the panel suite (8), runner (27), emp_stream (22); `test_cellular_protocol_results.py` removed with its module. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1` after the last code change: `dist\CRFS_IQ_Recorder.exe` 57,178,811 B (SHA-256 `1BAA088F…D339F`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,340,675 B (SHA-256 `A2B481CB…02408`), version resource 1.10.0. Not Authenticode-signed (unchanged). |
| EXE smoke test | The installer's program (`dist\onedir\CRFS_IQ_Recorder\CRFS_IQ_Recorder.exe`, the same build) started headlessly (`QT_QPA_PLATFORM=offscreen`, isolated `%LOCALAPPDATA%`), alive after 25 s, 73 MB, no stderr output, then stopped. The **portable onefile EXE could not be started on this PC**: Windows **Smart App Control** (enforce mode on this machine) refused it twice with *An Application Control policy has blocked this file* (Code Integrity events 3033/3077/3118); 1.9.0 and 1.9.1 had started under the same procedure earlier the same day, so this is the reputation verdict for the new, unsigned onefile stub rather than a fault in the program. If the portable EXE is blocked on the operator's PC too, use the Setup installer (its program ran here) or sign the EXE; Smart App Control was not changed. Not driven on the desktop. |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.10.0`, pushed to `personal`; GitHub Release `v1.10.0` with both files. 1.9.1 and older releases kept. |

---

# Part C.1 - The operator's protocol workbook and results written back (2 November 2026, Recorder 1.9.1)

## Scope

The operator supplied the data-collection protocol workbook `Cellular_IQ_Collection_Protocol.xlsx` (sheet *Cellular Protocol*: Band, Direction, Duplex, Freq Start (MHz), Freq Stop (MHz), Sensor, IQ Recording, IQ Duration (s), Recording Date & Time; 15 allocations covering the eight bands of the allocation chart as full ranges, DL then UL per FDD band, n78 as TDD 3300-3800 MHz; sheet *Reference* with the filling rules and the chart image) and asked for it to be filled by the collection and for every band to be scanned whole. All work ran on a temporary **copy** of the workbook outside the repository; the original was not opened for writing. **Simulated** throughout (no sensor case was needed: the capture path is unchanged since Part C); the sensor was not contacted.

## What the operator's workbook showed (and what changed)

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| C10 | High | The workbook imported **0 rows**: *0 allocation(s)*, plan empty, Start disabled. The sheet XML carries no `<dimension>` element (the workbook comes from a generator that omits it), so openpyxl's read-only `max_row` is `None`; 1.9.0 passed that as the row limit and read nothing. Every allocation of the field protocol would have been silently dropped. | `_row_values` streams to the sheet's end when no dimension is declared (the blank-row run limit bounds the scan). Regression test rewrites a workbook without `<dimension>` tags and asserts every row is imported. Re-import of the copy: 15 allocations, 0 errors, 2 warnings (the listed 700 DL / 800 DL ranges and 800 UL / 850 UL ranges overlap each other; both are recorded as listed). |
| C11 | Medium | Band cells of the form `700 MHz (B68 / B28)` had no nominal frequency (the cell is not a bare frequency), so bands were ordered by text and the plan preview could not show the nominal. | A leading frequency in the band text is its nominal (`_BAND_LEADING_FREQ`); the band text stays the label and is not doubled with "(700 MHz)" (`_mentions_frequency`). Labels read `700 MHz (B68 / B28) DL 753–803 MHz`. |
| C12 | Medium | Sub-band windows were spread evenly across the allocation, which put edges on fractional frequencies (B3 DL 1805-1880 MHz became 1805-1825, 1823.333-1843.333, …). Edges that are not on the protocol's grid are awkward to compare with the chart and with the Converter's axis. | Windows step from the start edge by the usable width and the last window is moved back onto the stop edge (1805-1825, 1825-1845, 1845-1865, 1860-1880 MHz; the 5 MHz overlap of the last window is recorded in the task and the manifest). Coverage of the allocation is unchanged (no gaps); tests assert whole-MHz edges for 50 and 75 MHz allocations. |
| C13 | Feature | The workbook's result columns (**IQ Recording**, **IQ Duration (s)**, **Recording Date & Time**) stayed empty: 1.9.0 wrote results only to manifests, the handover and the collection workbook's *Cellular Results* sheet, and `IQ Duration (s)` was not recognised (a unit in parentheses other than Hz was stripped from the alias key). | New module `cellular_protocol_results.py` and the mapping roles `result_time` (date & time) and the `iq duration s` aliases. Every round gets its own worksheet in the protocol workbook, `Round <scheduled local date> <HH-MM>` (` (2)` when an earlier session used the name), with the protocol header copied (texts, styles, widths, frozen pane) and one row per planned window in plan order: the protocol row's cells, the window actually recorded in the start/stop columns, the sensor that recorded in the Sensor column, the part file names one per line in *IQ Recording*, the held seconds in *IQ Duration (s)*, the local capture start as an Excel datetime (`dd/mm/yyyy hh:mm:ss`) in *Recording Date & Time*, and appended **Window / Status / Notes / Round** columns. The protocol sheet and earlier round sheets are never modified; a `.crfs-backup-<stamp>` copy is made before the first write; saving goes through a temporary file and `os.replace`; an open-in-Excel or locked workbook defers the write (retry every 15 s, logged once, *deferred* in the panel); writes are coalesced and run on a worker. Windows that were not recorded keep empty result cells and say *not recorded*, *skipped by the operator* or *uncertain — not counted as collected*; only measured seconds go in the duration column. Panel: *Advanced ▸ Fill the protocol workbook (one results sheet per round)*, on by default and enabled only when the protocol has a result column; a pre-run line names the workbook and warns when Excel has it open; the status area shows the round sheet and its write state. |

## Simulated results

| Case | Result |
| --- | --- |
| Import of the copy of the operator's workbook (after C10/C11) | **Pass.** 15 allocations, header row 1, mapping: result `IQ Recording`, result time `Recording Date & Time`, duration `IQ Duration (s)` (empty, so the campaign duration applies), `Sensor` kept as an extra column. The *Reference* sheet is not a protocol and is ignored. |
| Plan on the R40-8 model (20 MHz verified window, 2 s per capture, 10 s wait) | **Pass.** 65 captures per round: 700 MHz 3+3, 800 MHz 2+2, 850 MHz 2+2, 900 MHz 2+2, 1800 MHz 4+4, 2100 MHz 3+3, 2600 MHz 4+4 (DL then UL each), 3500 MHz TDD 25; about 16.3 GB and 22 min per round at 2 s (about 82 GB per round at 10 s). All edges on whole MHz; every allocation covered without gaps; overlaps of the last windows recorded. On the *Node 100-18* named in the workbook the IBW model gives 100 MHz but no verified gapless figure, so the panel's note says so and the 20 MHz default still applies until a wider window is verified on that unit. |
| Results sheet on the copy of the operator's workbook (simulated round: 5 confirmed incl. one short, 1 uncertain, 59 not recorded) | **Pass.** Sheet `Round 2026-11-02 19-00` with 65 rows and an Excel table `Round_2026_11_02_19_00` (A1:M66); file names one per line, duration `2.0` / `1.3`, datetime cells typed as dates; uncertain and unrecorded rows left empty with the reason in Status; the protocol sheet byte-for-byte unchanged (values compared with the original copy); the *Reference* chart image, the three data validations and the `CellularProtocol` table survive openpyxl's round trip; a second write of the same round reuses the sheet (idempotent). A separate hidden Excel instance (`DispatchEx`, `Visible=False`, read-only) opened the result **without a repair prompt**: 3 sheets, 1 shape on *Reference*, both tables, `I2` shown as `02/11/2026 19:00:07`, `H2` as `2.0`. |
| Runner: deferred first write while "Excel" holds the workbook, retry, round-by-round rewrites, short capture, session persistence of the sheet name | **Pass** (scripted host). One backup only; `background_jobs()` counts the write; the handover is rewritten after the last write. |
| Panel end to end on the demo transport with a protocol-shaped workbook | **Pass.** Checkbox enabled by the result columns, import label *results → one sheet per round in the workbook*, pre-run check *ok* naming the three columns, round sheet written through the real worker path with the demo sensor's name, SHA-256-verified parts and file-check status; protocol sheet untouched; one backup. |
| Locked workbook (`os.replace` refused), workbook open in Excel (`excel_has_workbook`), missing protocol sheet | **Pass.** `ExcelBusyError` in the first two cases with no temporary file left and the workbook unchanged; `ValueError` naming the sheet in the third. |
| Paired DL/UL layout | **Pass.** The DL row shows only the DL window and the UL row only the UL window. |

## Not done / still to verify

- The results sheet against the real sensor (the capture path is unchanged since Part C; the write path was exercised through the demo transport and the scripted host only).
- "Save each round separately" is implemented as one worksheet per round inside the protocol workbook (plus the backup copy and the per-session handover). If separate workbook files per round are wanted instead, that is a small addition.
- Whether "scan all the band from the spectrogram" also means a whole-band composite picture in the Converter: not built; the collection covers each band window by window, and each window is its own recording and spectrogram.

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (offscreen Qt) | 361 collected: **360 pass, 1 skip** (live-sensor test). Added in this part: 10 tests (`test_cellular_protocol_results.py`: 8; protocol import without `<dimension>` tags: 1; panel end to end with the protocol workbook: 1); the plan test now asserts whole-MHz edges. A first full run that overlapped the EXE build had one timing failure in `test_merge_gui` (a 15 s UI wait while the PC was busy; the merge itself had completed per the log); the module passes alone in 7 s and the final run below passed in full. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`, rebuilt after the last code change: `dist\CRFS_IQ_Recorder.exe` 57,128,531 B (SHA-256 `E087DBAF…414D72`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,291,697 B (SHA-256 `3249E3D7…B1DF22`), version resource 1.9.1. Not Authenticode-signed (unchanged). |
| EXE smoke test | Portable EXE started headlessly (`QT_QPA_PLATFORM=offscreen`, isolated `%LOCALAPPDATA%`), alive after 25 s with no stderr output, then stopped (the onefile child process included). Not driven on the desktop. |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.9.1`, pushed to `personal`; GitHub Release `v1.9.1` with both files. 1.9.0 and older releases kept. |

---

# Part C - Cellular Collection (2 November 2026, Recorder 1.9.0)

## Scope

First implementation of the Cellular Collection feature inside the Recorder: protocol import from Excel, ordered DL-before-UL sequence per band, scheduled rounds, programmatic captures through the existing recording path, downloads with checksums, manifests, Excel rows, quality checks and handover summary. Design notes: `crfs_iq_recorder/docs/CELLULAR_COLLECTION.md`; operator steps: Quick Start, section "Cellular Collection". No change to the manual recording flow, the Converter, or the sensor configuration.

Reference material: Jira SEN1-7340 and the two Confluence pages were not reachable from this environment, so the band facts used for tests (nominal order 700 < 800 < 850 < 900 < 1800 < 2100 < 2600 < 3500 MHz; FDD for the LTE bands, TDD for n78) come from the public 3GPP band tables and the operator's reference chart of 29 October 2026, and the template workbook marks every allocation *verify before use*. Nothing in the implementation hardcodes an allocation: the workbook decides.

## Hardware status

Sensor: the same RFeye Node **R40-8**, serial **rfeye300540**, firmware **2.25-325** as in Parts 0 and 1 (identity read from `/api/node.json` and shown by the connected main window before every case). Storage `/mnt/1/remdata/`: 944.9 GB total, 880.6 GB free at the start, 878.8 GB at the end. The sensor was unreachable for most of the afternoon (14:25-15:32 local: no ping, no TCP 80/22, ARP resolved, so the fault was beyond this PC) and answered from 15:51; every hardware case below ran between 15:51 and 16:40 local time (13:51-14:40 UTC, the sensor's file names). As in Part 0, the cases were driven **offscreen** (`QT_QPA_PLATFORM=offscreen`) through the real `MainWindow` and its Cellular panel with an isolated settings folder and the download folder `QA-20261102-190` (outside the repository); nothing was clicked on the desktop, the operator's workbook was not used (Excel logging off), and **nothing on the sensor was deleted**: the 11 disposable captures of this pass (2 s each, 125.7 or 251.4 MB) stay in the sensor's `20261102/` folder. The tunnel to the sensor moved 0.5-1.3 MB/s, so a 125.7 MB download took 95-250 s; three cases ran with automatic download off for that reason, after the download path had been verified.

| Case | Result |
| --- | --- |
| Identity and availability first (read-only: TCP, `GET /api/node.json`, SFTP `df`) | **Hardware, pass.** Model, serial and firmware as above; free space read over SFTP; no POST until the identity matched. |
| Pre-run checks on a 3-allocation plan (700 MHz DL 758-768, 700 MHz UL 703-713, 1800 MHz DL 1805-1815; 2 s each) | **Hardware, pass.** identity ok; computer clock ok; sensor clock and GNSS *info: unknown (not exposed by the sensor API used here)*; sensor storage ok (820.1 GB free, one round about 413.7 MB with margin); local storage ok (814.4 GB free, 377.3 MB per round plus the 2.1 GB reserve); plan ok (3 captures, 6 s of IQ, about 377.3 MB); test capture offered. |
| Plan of 3, recorder format **XDAT** (the operator's saved format), downloads on (15:55) | **Hardware, pass with one defect found (C5).** 3 of 3 confirmed in the planned order within 29 s (`file_135548`, `file_135558`, `file_135607`: `.xdat` 125,706,112 B + `.xmrk` 8,192 B each, held 2.011 s), downloaded and SHA-256 verified; but the quality check then reported every file as *corrupt - not a WAVE file*. Fixed; see C5 and the re-run below. |
| Plan of 3, format **WAVE** chosen in the panel, downloads on (16:10) | **Hardware, pass.** DL 700 -> UL 700 -> DL 1800 as planned; each capture: request accepted 0.6 s after Start, file named at +1 s, *finished* at +4 s, confirmed at +6-7 s; waits of 3 s started only after confirmation; `file_141021`, `file_141032`, `file_141042` (125.7 MB, 2.011 s each) downloaded two at a time and SHA-256 verified; quality ok / **warning (level change)** / ok with a sampled preview PNG per capture; manifests written; handover JSON/Markdown with 3 logical captures / 3 files; manual **Start Recording** held during the session and enabled again afterwards. The level-change warning is a measurement of the signal (the UL block differed between the sampled blocks), reported, not suppressed. Finding C6 (listed sizes) came from this run. |
| 50 MHz allocation (2600 MHz DL 2620-2670) split into sub-band windows, WAVE, downloads off (16:18) | **Hardware, pass.** Three windows 2620-2640, 2635-2655, 2650-2670 MHz (5 MHz overlap, each labelled *window n of 3 (sequential coverage, partial capture)*) recorded one after another: `file_141857`, `file_141907`, `file_141918`, 251,412,396 B each (20 MHz -> 31.25 MS/s), held 2.011 s; manifests carry the whole-allocation extent next to the actual window. No claim of simultaneous full-band capture is made anywhere. |
| Pause during a capture, Resume, Stop during the countdown (wait 25 s) (16:19) | **Hardware, pass.** Pause pressed 0.1 s after the first POST: *Pausing after the current capture…*, the capture finished and was confirmed (`file_141942`, 125,706,804 B), then *Paused* (round 1/1, task 2/3). Resume -> *Next capture in 25 s*. Stop at 21 s left: *Stopped. The pending capture was cancelled before it was sent.*, round *stopped: 1 of 3 confirmed, 2 not recorded*, no further POST (the sensor's folder shows no file after `file_141942`). Finding C7 (status text after Stop) came from this run. |
| Application closed 1 s after a POST (16:20), restart, **Reconcile with sensor**, **Resume session** | **Hardware, pass.** The session file held the first task as *submitting*; the restart showed *Interrupted* with the recovery text and the uncertain capture; Reconcile listed the sensor's folder (7.9 s) and answered *0 confirmed from files, 1 still uncertain* (the Node had not opened a file in that second - confirmed by a read-only listing: no `file_1420xx` on the sensor); Resume was refused with *Reconcile or skip the uncertain capture(s) first.* Nothing was resent. (The harness itself crashed at exit the first time because it skipped the application's own `stop_worker_threads`; not a product fault, fixed in the harness.) |
| Re-run after the fixes: one XDAT capture with downloads (16:33) | **Hardware, pass.** `file_143308`: at confirmation the sensor listed `.xdat` 125,706,112 B and `.xmrk` 8,192 B; the fresh listing 5 s later found `.xmrk` at 8,264 B and a new `.xhdr` of 795 B (noted in the manifest: *appeared on the sensor after the capture was confirmed*); all three downloaded and SHA-256 verified; result *files downloaded and checksummed; content not inspected*, quality level `not_checked`; the handover lists the capture under *Content not inspected*, not as a quality warning. |
| Re-run after the fixes: one WAVE capture with downloads (16:37) | **Hardware, pass.** `file_143709`: listed 125,706,156 B at confirmation, 125,706,804 B in the fresh listing (the 648-byte trailer), downloaded at the final size and verified; manifest records both sizes; quality ok, measured 2.0113 s at 15.625 MS/s, preview written. |
| Gapless / full-band claims | None made. The capture window defaults to 20 MHz because 20 MHz captures at 31.25 MS/s were verified gapless on the R40-8 in Part 1; wider allocations are recorded as consecutive windows and labelled *sequential coverage, partial capture*. |

Sensor observations (not changed): four of the POSTs were answered with the Node's message *The node is currently locked due to a task that it is currently running … will unlock <time> Node Time*, where the time was the end of an **earlier** capture (8-14 minutes in the past in two cases); the Node then named its file, finished and the capture was confirmed normally, so the message is shown in the log and nothing is retried or delayed because of it. The sensor clock and GNSS state are not exposed by the API calls this application uses, so both are reported *unknown* rather than guessed.

## Simulated results (demo transport, synthetic WAV parts, offscreen Qt)

| Case | Result |
| --- | --- |
| Excel layouts: one row per direction; paired DL/UL columns; title row above the headers; hidden sheets; units in cells, headers or default; duration column present / absent | Pass. 15 protocol tests. Allocations are imported exactly (no nominal-as-centre, no expansion to the standard band). A workbook without start/stop columns (the operator's "Cellular Master" style sheet) is refused with the missing columns named. |
| Invalid rows (reversed edges, missing stop, contradictory centre/bandwidth, unknown direction, duplicates, overlaps, result rows) | Pass. Listed with row numbers, plan disabled; duplicates collapsed and reported; result rows shown but never queued. |
| Ordering: bands by nominal frequency, DL before UL, TDD once, sub-band expansion with stated overlap, odd edges widened by 1 Hz and reported, repeat column | Pass. 11 plan/schedule tests. |
| Time zones (local and UTC), round IDs, delayed round keeps its scheduled time, missed round listed and run only on request, tight-schedule warning | Pass. |
| Waits start after confirmed finalization; Pause after current; Resume; Stop in the middle of a capture and during a countdown; pause during a countdown restarts the wait | Pass. 18 runner tests. |
| Capture outcomes: confirmed, short (held < 95 %), unknown (never counted, never resent), not_sent (one automatic retry), sensor_error; round reported *partial* | Pass. |
| Application closed during a capture; restart shows *interrupted*; nothing resent; Reconcile confirms from the sensor's files via the recording history, otherwise *unknown*; Skip uncertain; Resume session re-queues the download that was pending at the crash | Pass. |
| Downloads start from a fresh listing: a sidecar that appears late is added and downloaded, a grown file keeps both sizes (`size_listed_at_confirmation`, `size`); a failed second listing falls back to the sizes listed at confirmation with a note | Pass (fake SFTP browser). Hardware confirmation above (16:33 and 16:37). |
| Non-WAVE recordings (`.xdat` + `.xmrk`) are reported *not checked*, never *corrupt*; mixed sets keep the WAVE checks; the handover lists them under *Content not inspected* | Pass. |
| Recorder busy (manual recording or series) -> retried, storage refusal -> paused with reason | Pass. |
| Download backlog over the limit -> new captures pause with the reason, continue when drained | Pass. |
| Low local storage (simulated `disk_usage`) -> pause with reason, existing files untouched, continues when space is back | Pass. |
| Workbook locked by Excel (simulated `ExcelBusyError`) -> row deferred, capture still *confirmed*, queued on a later tick | Pass. |
| Connection loss during a capture (simulated *unknown* outcome) -> not resent, next task proceeds, round *stopped/partial*, uncertain task kept for Reconcile | Pass. |
| Midnight folder change -> each capture keeps its own remote folder in its manifest | Pass. |
| End to end on the demo MainWindow: 3 captures of 2 parts each confirmed from the demo sensor's files, downloaded (2 parallel), SHA-256 verified, quality report and sampled preview PNG per capture, manifests with checksums, 3 rows on a new `Cellular Results` sheet (two parts per row), handover JSON/Markdown with 3 logical captures / 6 files / 3 images; manual Start held during the session and working afterwards; *Recording format* follows the recorder's Format box by default, a format chosen in the panel is what is sent and recorded in the manifests | Pass. |
| Pre-run checks (connection, identity, computer and sensor clock, GNSS, sensor and local storage, plan, recording format, test capture) with a disconnected sensor, a bad clock and a full sensor | Pass. 12 quality tests. |
| Post-capture validation on synthetic parts: clean parts OK with duration measured; truncation, time gap, missing part, tuning mismatch flagged; empty, corrupt, clipping, zero runs and level change detected | Pass. |
| Panel: template round-trips through the importer; settings and dates round-trip; mapping dialog requires both allocation edges; Test capture reserves and releases the sensor; collapsed panel has only its header (fewer than 10 widgets) until opened; a session left behind opens the panel at start with the *Interrupted* chip and recovery choices | Pass. 6 panel tests. |
| UI thread responsiveness: downloads, hashing, file checks and preview rendering on worker threads; one quality check at a time; the panel's minimum width fits the recording card (offscreen render checked at 1180 px window width) | Pass (offscreen). |

## Defects found and fixed during the pass

| Id | Severity | Finding | Fix |
| --- | --- | --- | --- |
| C1 | Medium | The first panel version built all of its ~180 widgets (11 of them combo-box popups, which are top-level windows) with the main window. Measured with a 6-window loop: the window went from 149 to 332 widgets, and because the test suite never frees closed windows (pre-existing: bound methods held by worker objects), every later `configure_appearance` re-polish grew 3-4x slower (GUI test setups 13-17 s instead of 3-4 s; `test_theme_toggle_persists` 38 s instead of 10 s) and two runs of the GUI module died with an access violation (0xC0000005) inside `processEvents` during `test_gui_excel_flush_runs_on_worker_and_keeps_ui_state`, which the 1.8.10 baseline run in a separate worktree did not. | The panel body is built on first opening (`_ensure_built`); collapsed, the main window has 153 widgets and the same 7 top-level widgets as 1.8.10 (measured). A saved or interrupted session opens the panel at start. |
| C2 | Medium | Quality workers rendered previews through `matplotlib.pyplot` from several threads at once (three downloads finishing together start three checks). pyplot's figure registry and FreeType text rendering are not thread-safe. | Previews use the object-oriented Agg API (`Figure` + `FigureCanvasAgg`) under a module lock, and the runner runs one quality check at a time (`_quality_pending`). |
| C3 | Low | Tests that build the main window wrote the panel settings into the developer's own config folder (`cellular/settings.json` next to the source). | Autouse fixture isolates `user_config_dir` for every test; `cellular/` and `merge_history.json` ignored in Git for source runs. |
| C4 | Low | The panel's one-row layouts (schedule, controls, recovery buttons, four-column grids) forced the recording card wider than the left pane. | Two-column grids, wrapped button rows, compact combo boxes; offscreen render at 1180 px checked. |
| C5 | High (hardware) | The collection records in the recorder's Format box setting; the operator's saved format is **XDAT**. The quality check assumed WAVE and reported every downloaded `.xdat` / `.xmrk` as *corrupt - not a WAVE file* (3 of 3 captures in the 15:55 run), which would have sent a good collection back as damaged, and neither the summary nor the manifest said which format had been recorded. | The format sent is carried from `MainWindow` into `CaptureSummary`, the task state and the manifest (`recording_format`, `encoding`; *unknown* when not known). `validate_capture` checks WAVE content only, reports other formats as `not_checked` with the note *sizes and SHA-256 verified by the download; content not inspected*, and never calls them corrupt; previews are skipped. The handover lists these captures under *Content not inspected*. The panel's Advanced section gained **Recording format** (*Same as recorder* by default), the pre-run checks show the effective format, and the plan preview names it. Verified on hardware at 16:33. |
| C6 | Medium (hardware) | The sensor keeps writing for a few seconds after it reports *finished* and the listing has gone quiet: each WAVE part grows by its 648-byte trailer (125,706,156 -> 125,706,804 B), an XDAT recording gets its `.xhdr` sidecar (795 B) late and its `.xmrk` grows (8,192 -> 8,264 B). The downloads were queued from the sizes listed at confirmation, so manifests recorded the smaller size next to a checksum of the larger file, and `.xhdr` was never downloaded. | After confirmation the runner waits `FINALIZE_SETTLE_S` (5 s), lists the folder again on a worker thread, adds late files of the same stem (with a note), records `size_listed_at_confirmation` and the final `size`, and only then queues the downloads; a failed second listing falls back to the confirmation sizes with a note. The background-job count covers the gap so the handover is not written early. Verified on hardware at 16:33 (XDAT: `.xhdr` picked up, `.xmrk` 8,264 B) and 16:37 (WAVE: 125,706,804 B). |
| C7 | Low (hardware) | After **Stop** during a countdown the status line read *Session complete* although the round was *stopped: 1 of 3 confirmed, 2 not recorded*. | The finished state reads *Session stopped* when a round was stopped and *Session finished with problems* when a round is partial or failed; *Session complete* only when every round finished. |
| C8 | Medium | **Reconcile with sensor** ran its SFTP listing on a worker; an exception there (sensor gone, wrong password) went to the main window's generic job-failure handler, which only logs, so the panel's completion callback never ran: the Reconcile button stayed disabled and the recovery text stuck at *Reconciling with the sensor's files…* until a restart. Found by review during the hardware session. | The listing returns `{"error": …}` instead of raising; the callback always runs, the log reads *Reconciliation failed: … Nothing was changed; try again when the sensor answers.* and the recovery buttons stay usable. |
| C9 | Medium | Downloads that were pending or running when the application was closed mid-session were not resumed by **Resume session**: the task stayed *confirmed* with files *pending* for ever. Found by the restart test when it was extended with `auto_download` on. | `resume_after_restart` re-queues every file of a confirmed task whose status is *pending*, *active* or *present* and drains the download queue; the log line says how many were re-queued. |

## Not done / still to verify

- Scheduled rounds at real times of day over several hours, and a missed round after a real sleep/restart: covered by the scheduler tests (time injected) and by the restart case on hardware; not left running overnight.
- Pause/Stop while a hardware download is in flight (downloads were off in the Pause/Stop case because of the 0.5-1.3 MB/s tunnel); covered by the runner tests.
- Excel rows from hardware captures (Excel logging was off in every hardware case so the operator's workbook stayed untouched; the `Cellular Results` sheet is tested on a temp workbook).
- The 1.9.0 EXE on the desktop (headless start only; see "Test and release status").
- Real locked-workbook behaviour with Excel holding the file (simulated through `ExcelBusyError`; the main app's own Excel-busy path is hardware-tested in Part 1).
- Other recording formats than WAVE and XDAT on the sensor (NCP, HDF5 are offered by the recorder; the collection treats every non-WAVE format the same way: download, checksum, *not checked*).

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (`crfs_iq_recorder/tests`, offscreen Qt) | 351 collected: **350 pass, 1 skip** (live-sensor test); final run 5.0 min after the hardware fixes (JUnit record: 351 tests, 0 failures, 0 errors). Added: 62 cellular tests (15 protocol, 11 plan/schedule, 18 runner, 12 quality/manifest/Excel, 6 panel). Full-suite history of the pass: two early runs passed, one died with the C1 access violation, one passed after C2 alone (11.6 min, still with the slow setups), two passed after C1 (6.5 and 10.3 min; the second ran while the PC was busy elsewhere), and the final run passed after C5-C9. The slow GUI setups that remain come from the pre-existing never-freed-window leak, which grows with every window a test opens and is unchanged by this release. |
| Converter suite | Unchanged (no Converter change). |
| Hardware harness (outside the repository, disposable; contains the sensor address) | `hw_probe.py` (read-only identity), `hw_cell.py` cases checks / plan3 / plan1 / subband / pausestop / interrupt / reconcile driven through the real `MainWindow`, `hw_list.py` (read-only folder listing). Logs `hw_*.txt`. Downloads in `QA-20261102-190` (kept, outside the repository). |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`, rebuilt after the last code change: `dist\CRFS_IQ_Recorder.exe` 57,100,981 B (SHA-256 `9F64C0E9…0CE7EC`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,265,596 B (SHA-256 `6DA6E566…371084`), version resource 1.9.0. Not Authenticode-signed (no signing credentials on this PC; see `docs/DISTRIBUTION.md`). |
| EXE smoke test | The portable EXE started headlessly (`QT_QPA_PLATFORM=offscreen`, isolated `%LOCALAPPDATA%`), stayed up for nearly three minutes with no stderr output and wrote nothing into the isolated settings folder; it was then stopped. Not driven on the desktop (desktop in use). |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.9.0`, pushed to `personal`; GitHub Release `v1.9.0` with both files. 1.8.10 and older releases kept. |

---

# Part 0 - Completion status, split recordings and merge (1 November 2026, Recorder 1.8.10)

## Scope and setup

Same sensor as Part 1 (RFeye Node R40-8, serial rfeye300540, firmware 2.25-325, time zone UTC, no GPS fix), same PC. The operator's own Recorder 1.8.9 stayed open and connected during the whole pass, so every hardware case was driven **offscreen** (`QT_QPA_PLATFORM=offscreen`) through the real `MainWindow`, `SftpWindow` and `MergeController` with an isolated settings folder and the download folder `QA-20261101-1810`; nothing was clicked on the desktop and nothing on the sensor was deleted. Demo mode off for every hardware case. All recordings in this part: centre 511 MHz, 22 MHz bandwidth (the band of the operator's 08:36 recording), WAVE, which the Node records at 31.25 MS/s = 125 MB/s.

## D1 - "Time elapsed - file not confirmed" (fixed)

**What the operator saw (1.8.9, 08:36 local):** `Sensor reported: recording finished.` at 08:36:36, `File not confirmed.` at 08:36:48, and the status stayed on *Time elapsed - file not confirmed* although `file_063625_0001.wav` was complete on the sensor.

**Trace (hardware + code).** The sensor stream was fine: `time.RemoteTimeData` named `file_063625_0001.wav` and the INFO "Remote recording is now finished" arrived. The file was on the sensor with a correct header (`riff_size + 8 == size`). File names are UTC and SFTP mtimes are local; that two-hour offset is handled by `ranked_file_times` and was not the cause. The cause was the completion path for a **plain recording (Excel off, Repeat off)**: 1.8.9 used a legacy three-attempt check (2 s / 6 s / 14 s after the timer) that depended on the Sensor Files window being open and ignored both the sensor's "finished" message and the file names the sensor had announced; the stream-aware watch (`_poll_collection_files` / `capture_is_settled`) only ran when Excel logging or Repeat was on. With Sensor Files closed, the three attempts found nothing and the recording was marked *file not confirmed*.

**Fix.** Every recording now runs the same stream-aware file watch (the SFTP poll starts when the timer elapses, whether or not Sensor Files is open). The Start button reads **Confirming files…** while the watch runs; the status line separates the two facts: *Time elapsed. Sensor is writing file_…* -> *Sensor finished. Confirming file_…* (or *Sensor finished. 2 of 3 file(s) confirmed; waiting for the rest…*) -> *Recording finished (N file(s))*. A sensor "finished" without its file gives *Sensor finished, but file_… was not found on the sensor* after the grace period, never *Completed*. Two side defects found by the new tests were fixed with it: a neighbour's file adopted before the sensor named its own file was never forgotten (`prune_file_watch`), and a recording with no history record held the Start button for ever (the watch now ends with *Check Sensor Files for the recording*). Removed: `_try_confirm_files`, `_schedule_file_confirm`, `_refresh_open_sftp`, `_uses_collection_watch`.

**Hardware result (1.8.10 source, offscreen, Sensor Files closed, Excel off, Repeat off):** 2 s recording `file_075545_0001.wav`: *Recording… 2 s* (0.0 s) -> *Confirming files… / Time elapsed. Sensor is writing file_075545_0001.wav…* (2.4 s) -> *Sensor finished. Confirming file_075545_0001.wav…* (3.8 s) -> **confirmed, *Recording finished (1 file(s))*, Start Recording** (7.2 s); log `Capture finished with 1 file(s), 251.4 MB (sensor confirmed)`. 10 s recording `file_075553_0001-0003`: parts announced at +1 s, +6 s, +11 s; *Sensor finished. 1 of 3 file(s) confirmed; waiting for the rest…* (13.2 s), *2 of 3* (15.2 s), **confirmed with 3 files** (17.2 s, 1,252.9 MB). A 3 s recording started while a 1.25 GB download was running confirmed in 8.2 s with the download untouched.

**Regression tests:** `test_single_recording_without_excel_is_confirmed_from_the_files`, `test_sensor_task_completion_is_shown_apart_from_file_confirmation`, `test_sensor_finished_without_its_file_is_not_reported_as_confirmed`, `test_gui_marks_recording_finished_after_recording_time` (rewritten), `test_pruning_forgets_a_neighbours_file_adopted_before_the_sensor_named_its_own`, `test_parts_named_by_the_sensor_are_waited_for_before_completion`, `test_hung_http_does_not_block_the_next_recording` (existing, now covers the no-record case).

## Why the Node splits recordings (investigation)

| Question | Finding | Evidence |
| --- | --- | --- |
| Is it a size limit? | **Yes.** The Node's recording service closes a part after 1023 blocks of 512,000 bytes = **523,776,000 bytes of IQ** and opens the next number, independent of the sample rate. | Hardware: every full part of `file_063929` (8 parts), `file_075553` (3 parts) and all Part 1 captures holds exactly 523,775,872 data bytes (130,943,968 complex samples at 31.25 MS/s = 4.190 s; 2.095 s at 62.5 MS/s). Hardware: `/etc/node/ncpd/ncpd.conf` on the sensor, section `storage`: `"filename": "/mnt/1/remdata/{date}/file_{time}_{n:4}"`, `"filesize": "500MB"`; the factory default `/etc/node_default/ncpd/ncpd.conf` has `iq_{date}_{time}_{n:4}` and `100MB`. This is also why today's files are `file_HHMMSS_000N.wav` and not `iq_…`. |
| Is it documented? | **Yes.** The RFeye Site User Guide (Issue 42, Site 1.48) describes the storage `filesize` setting as "the maximum size of a file" after which a new file is started, and WAV-E (Site 1.49) adds the signal time / frequency / bandwidth metadata in the `meta` chunk. | Documents on file; see the Confluence post for the citation. |
| Do `duration` / `rate` / `capture_length` cause it? | **No.** The app sends the same number of seconds in all three fields (`request_builder`). Measured on the sensor: `duration` sets the recorded length, `rate` is the repeat interval and must be >= `duration` (the Node rejects `rate < duration`), and `capture_length` changed nothing in these tests; a 2 s and a 4 s capture each came back as one file whatever the other two fields held. The parts' `meta` `signaltime` stamps are contiguous: each part starts where the previous one ends to within 1 ms (|delta| <= 0.8 ms over 7 joins of `file_063929`, 30.002 s in total), so a split recording is one continuous capture. | Hardware: `probe_parts.py` (`parts_file_063929.json`), merge plans of `file_063929` and `file_075553`, `emp_timing_semantics.py` (four raw EMP tasks, see "Hardware results"). The EMP schema is not public. |
| Are the parts consecutive time segments? | **Yes**, verified from the Node's own `signaltime` (UTC, millisecond) in every part and from the per-part sample counts. | Same evidence; the merge refuses parts whose stamps show a gap or an overlap. |
| Can the sensor write one longer file? | Only by raising `filesize` in `ncpd.conf` on the Node (CRFS configuration, outside this app; not changed). Even then a plain RIFF/WAVE file cannot exceed 4 GiB (32 s at 31.25 MS/s), and the Node 40-8 sustains gapless local IQ storage only up to 20 MHz (CRFS specification; Part 1 measured dropped time at 62.5 MS/s). The app therefore keeps the sensor's behaviour as is and merges on the PC. | Part 1 H4; CRFS Node 40-8 datasheet. |
| Part layout | 44-byte RIFF header (`fmt ` PCM, 2 channels, 16 bit, block align 4), `data`, then a 652-byte trailer: `meta` chunk (644 bytes) with `<signalinfo>` XML: `signaltime`, `rf_frequency`, `bandwidth`, `data_type complex`, `sample_rate`, `if_frequency`, `antenna`, `receiver`. | Hardware: `parts_file_063929.json`. |

## Merge recording parts (new, Recorder only)

- **Controls.** *Merge recording parts* check box in the main window (off by default, saved in settings as `merge_parts`), *Cancel merge*, a status line and a thin progress bar; *Merge parts* button in Sensor Files with a confirmation that lists the parts, the verified duration and sample count, the output name/size/container and the folder (or, when parts are missing locally, how many will be downloaded first and the estimated size).
- **When it runs.** Automatic: after a download batch finishes with every part of a split recording present locally at the sensor's size (failed or cancelled batches never merge; a recording that is still being confirmed is skipped and logged). Manual: from Sensor Files, missing parts are downloaded through the normal download first; the toggle is not needed. Turning the toggle off drops queued automatic merges and leaves the running one to *Cancel merge*.
- **Checks before writing (`iq_merge.plan_merge`).** Same stem, numbered parts sorted numerically, no duplicate or missing number, at least two parts, identical `fmt` (PCM, channels, rate, bits), identical `rf_frequency` / `bandwidth` / `receiver` from the `meta` chunk, every part readable and whole (truncated data, mid-sample end, cut trailer, RIFF size mismatch are refused), and continuity from `signaltime` with a 1.5 ms tolerance: a gap or an overlap refuses the merge with the measured delta; parts without stamps merge as *continuity unknown*. Different frequency windows are never joined.
- **Output.** `<stem>_merged.wav` next to the parts (never overwritten; a second manual merge gets ` (2)`), RIFF up to 4 GiB, **RF64 with `ds64`** above, samples copied in 8 MiB chunks without any processing, the first part's `meta` chunk carried over, a `LIST/INFO` note (`ISFT`, `ICMT` listing the parts), and `<output>.merge.json` with parts, sizes, stamps, join results and the verification record. Written to `<output>.partial`, then verified (frame count = sum of parts, 4,096-byte windows at every join compared to the parts) and renamed; a failure or cancel deletes the partial; a `.partial` left by a crash is removed at the next start. Disk space is checked first (output size + 64 MiB).
- **Dedupe.** `merge_history.json` (settings folder) plus the sidecar next to the parts: the same parts (sensor names and sizes) are not merged again automatically after a refresh, a reconnect, a second download or a restart.
- **Separation of states.** Recording, download and merge keep their own status; a failed or cancelled merge logs *The recording itself is not affected.* and changes neither the recording result nor the download result.

## Converter compatibility (no Converter change needed)

| Check | Result |
| --- | --- |
| Merged RIFF from real parts: `file_063929_merged.wav` (8 parts, 3,750,236,914 B) | Hardware files. libsndfile (the Converter's reader) reports WAV / PCM_16 / 2 ch / 31,250,000 Hz / 937,559,008 frames = 30.001888 s = the sum of the parts; 8,192 samples around each of the 7 joins equal the parts; SHA-256 of every sample streamed from the merged file equals the parts streamed in order (11.6 s). `process_file` rendered it in 112.6 s (RBW 50 kHz, n_fft 2048); the waterfall shows no line at any join time (4.19, 8.38, … 29.33 s). |
| Same payload forced to RF64 | libsndfile reports RF64 / PCM_16 / 937,559,008 frames; joins equal; `process_file` rendered it (125.6 s). |
| Genuine > 4 GiB output: two synthetic 2.2 GB Node-style parts -> `file_070000_merged.wav`, 4,400,000,568 B, RF64 chosen by size | libsndfile reports RF64 / 1,100,000,000 frames = 35.2 s (frame count beyond the 32-bit RIFF limit read correctly); join equal; `process_file` rendered it (130.9 s). Simulated (synthetic payload). |
| Naming | `<stem>_merged.wav` is not a numbered part: neither application groups it with the parts, and the Converter's `part_time_offset` returns no part, so the time axis of a merged file starts at 0 s as it should. Unit-tested. |

## Hardware results (1.8.10 source, offscreen harness `hw_merge_drive.py`)

| Case | Result |
| --- | --- |
| Completion status, 2 s, Sensor Files closed, Excel off, Repeat off (the operator's case) | **Pass**, see D1. |
| Completion with 3 parts, parts announced over 11 s | **Pass**, see D1. |
| Recording while a download runs | **Pass**: 3 s recording confirmed in 8.2 s during the 1.25 GB download; the download continued. |
| Download + automatic merge (toggle on): `file_075553` 3 parts, 1,252.9 MB | **Pass**: downloaded in about 5 min (3 parallel streams), merged in 3.0 s -> `file_075553_merged.wav`, 313,218,016 samples = 10.023 s, 1.25 GB RIFF, continuity verified, 4,096-byte windows at both joins equal, originals kept at their sizes; main-window status *Merged file_075553 -> file_075553_merged.wav (10.02 s, RIFF)*. |
| Second download of the same recording (refresh / reconnect case) | **Pass**: the three parts of `file_075553` were downloaded again with the toggle on (they land as `… (2).wav` copies next to the first download); the controller answered *already merged (file_075553_merged.wav)*, logged `file_075553: already merged -> file_075553_merged.wav.` and wrote no second file. |
| Manual merge with missing parts (toggle off) | **Pass**: 6 s recording `file_080547` -> 2 parts (523.78 MB + 226.27 MB), confirmed as *Recording finished (2 file(s))*. Only part 1 downloaded -> no merge (toggle off, 0 merge events). Selecting the recording enabled **Merge 2 parts**; the confirmation read "1 part(s) are not in the download folder yet and will be downloaded first (226.27 MB). Estimated output: about 750.05 MB in …". Part 2 was downloaded through the normal download and the merge followed: `file_080547_merged.wav`, 187,511,776 samples = 6.000 s, 750.05 MB RIFF, continuity verified (join delta -0.2 ms), the 4,096-byte join window equals the parts; 30.1 s from the click to the finished file; the toggle stayed off. |
| Cancel a running merge (8 parts, 3.75 GB, cancelled at >= 30 %) | **Pass** after a fix found by this case: the first run showed *0 %* for the whole 3.75 GB merge and finished before the harness could cancel, because the byte counts were marshalled through a `Signal(int, int)` (32-bit) and overflowed above 2 GiB. The progress signal now carries Python integers (regression test `test_progress_is_right_for_outputs_larger_than_2_gib`). Re-run: *Merging file_063929 (8 parts, 30.00 s)… 6 % … 32 %*, *Cancelling merge of file_063929…*, then *Merge cancelled: file_063929.*; log `Merge cancelled for file_063929. The recording itself is not affected.`; no new file, no `.partial`, Cancel button hidden, the queue free. |
| Restart recovery (`.partial` sweep) and dedupe across a restart | **Pass**: a planted `file_000000_merged.wav.partial` was removed at the next start with the log line *Removed unfinished merge output …*; after the restart the registry file `merge_history.json` was present and an automatic request for the already merged `file_075553` answered *already merged (file_075553_merged.wav)*. The toggle came back in the state it had been left in (off). |
| Failed merge (one part truncated locally) | **Pass**: a local copy of `file_075553_0002.wav` cut by 99,916 bytes -> *Merge failed: file_075553. file_075553_0002.wav: truncated: header promises 523,776,000 sample bytes, the file holds 523,676,652.*, log `… The recording itself is not affected.`; no output, no `.partial`, recording phase and *Recordings completed* unchanged, and the group is not remembered as merged (a later complete download merges normally). |
| `duration` / `rate` / `capture_length` semantics (raw EMP, disposable 2-4 s files) | **Done**: `duration=rate=capture_length=4` -> one file, 4.023 s; `capture_length=2` with `duration=rate=4` -> still one file of 4.023 s (`capture_length` neither shortens nor splits the capture at this firmware); `duration=2` with `rate=capture_length=4` -> one file of 2.011 s (`duration` sets the length); `rate=2` with `duration=4` -> HTTP 400, `ERROR rate cannot be less than duration` (`rate` is the repeat interval, not a sample rate). None of the three fields produces a split: the splitting comes from the Node's `filesize` setting only. |
| Large output on the sensor's files | 3.75 GB (8 parts) merged from real parts in 5.5 s, read by the Converter; a > 4 GiB RF64 was produced from synthetic parts (see Converter compatibility). A > 4 GiB recording from the sensor itself (more than 32 s at 25 MHz) was not downloaded in this pass (Untested on real parts; the RF64 path is identical). |
| Insufficient disk space | Simulated only (unit test with a patched free-space probe): the merge stops before writing. |

## Simulated results

- Engine: `tests/test_iq_merge.py` (18 tests) - Node-style containers, byte-exact 3-part merge, numeric (not lexical) part order, duplicates / missing / single part refused, incompatible rate / channels / frequency / receiver refused, truncated or unfinished part refused, gap and overlap refused with the measured delta, parts without `meta` merge as *unknown*, RF64 header fields, RIFF/RF64 choice by size, insufficient disk space, cancel and failed verification leave no output, existing output protected and names unique, stale `.partial` replaced, merged names never mistaken for parts, local ` (2)` copies of parts recognised.
- Controller: `tests/test_merge_controller.py` (6) - toggle and live-recording gates, manual requests ignore the toggle, registry across a restart and after the parts change, toggle off drops queued automatic merges but keeps manual ones, progress percentages right above 2 GiB, planning failures reported without a thread.
- GUI (offscreen Qt, demo transport): `tests/test_merge_gui.py` (7) - toggle default off and persisted, automatic merge once after a full download with no second merge on re-download or after a restart, toggle off leaves downloads alone and a manual merge fetches the missing part with the right dialog text, failed merge leaves recording status untouched and is not remembered, cancel removes the partial and frees the queue, automatic merge waits while the recording is live, start-up removes `.partial` files.

## Not verified on hardware in this part

- A real recording longer than 4 GiB downloaded and merged (RF64 from sensor parts); the RF64 writer and reader were verified on the real 3.75 GB payload and on a synthetic 4.4 GB one.
- Insufficient disk space during a merge (not provoked on this PC).
- The 1.8.10 EXE driven on the desktop (the desktop was in use): the EXE was built, started and checked headlessly, see "Test and release status".

## Test and release status

| Item | Result |
| --- | --- |
| Recorder unit + GUI suite (`crfs_iq_recorder/tests`, offscreen Qt) | 289 collected (276 test functions, 240 at `v1.8.9`): **288 pass, 1 skip** (live-sensor test). Added: 31 merge tests (18 engine, 6 controller, 7 GUI) and the completion tests listed under D1. Run twice, the second time after the progress fix. |
| Converter suite | Unchanged from Part 1 (no Converter change in this part). |
| Hardware harness (outside the repository, disposable) | `hw_merge_drive.py` scenarios bug1, split10, automerge, manual, cancel, restart, failed - all pass after the progress fix; `emp_timing_semantics.py` (4 raw EMP tasks); `verify_merged.py` (libsndfile + `process_file` on the merged outputs); `probe_parts.py`. Sensor recordings made in this part are disposable 2-30 s captures at 511 MHz and were left on the sensor; nothing was deleted from the sensor and no workbook was touched. |
| Build | `build_exe.bat` with `CRFS_BUILD_INSTALLER=1`: `dist\CRFS_IQ_Recorder.exe` 56,896,978 B (SHA-256 `DF917B6C…C6BF86`), `dist\CRFS_IQ_Recorder_Setup.exe` 43,061,791 B (SHA-256 `FCAC4327…15532D`), version resource 1.8.10. Not Authenticode-signed (no signing credentials on this PC, as for 1.8.9; see `docs/DISTRIBUTION.md`). |
| EXE smoke test | The portable EXE started headlessly (`QT_QPA_PLATFORM=offscreen`, isolated `%LOCALAPPDATA%`), stayed up with the GUI loaded (71 MB working set), wrote nothing but its cache folder and no stderr; the operator's running 1.8.9 instance was not touched. Driving the EXE on the desktop was not possible (desktop in use). |
| Git | Branch `cursor/polish-gui-easy-launch`, tag `v1.8.10`, pushed to `personal`; GitHub Release `v1.8.10` with both files. 1.8.9 and older releases kept. |

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
