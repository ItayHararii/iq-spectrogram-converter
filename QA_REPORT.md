# QA report - CRFS IQ Recorder 1.8.0 and IQ Spectrogram Converter 1.3.0

Date: 20 September 2026  
Machine: Windows 10 (build 26200), Python 3.13.

## What was tested

| App | Version under test | Notes |
| --- | --- | --- |
| CRFS IQ Recorder | **1.8.0** (this build) | Previous tagged release was 1.7.0. 1.8.0 capability-profile / compact-Excel work was reverted earlier and is **not** in this build. |
| IQ Spectrogram Converter | **1.3.0** (this build) | Previous source version was 1.2.0. |

Implemented now (keep): Excel collection logging, split-file grouping, wait between repeats, encrypted credentials, shared Sensorz icon, **IQ Storage**.

Deliberately not present: hardware capability clamping, estimated-time strip, compact Excel window. Those are requirement choices, not bugs.

## Environments

| Layer | Result |
| --- | --- |
| Automated tests (Recorder `.venv`) | Pass (storage, GUI smoke, frequency, Excel grouping, SFTP listing, downloads, preview) |
| Automated tests (Converter `venv`) | Pass (numeric conversion, local path remap, GUI smoke) |
| Demo / simulation | Pass (no live EMP POST) |
| Live sensor HTTP GET + identity | Pass (TCP reachable, wrong password = `authentication_error`, unreachable IP = timeout) |
| Live SFTP storage query | Pass (`statvfs`/`df` on `/mnt/1/remdata/`, about 93% free, level ok) |
| Live EMP POST / live Delete | **Not executed** (existing sensor recordings preserved) |
| Packaged EXE | Pass - `CRFS_IQ_Recorder.exe` (~280 MB) and `IQ_Spectrogram_Converter.exe` (~91 MB) started on this PC without using the source venv |
| Display scaling 125% / 150% | **Not tested** |
| Excel VBA inside Excel | **Not tested** (openpyxl / COM writers tested; macros were not run in Excel) |

No passwords, sensor IPs, or field filenames are recorded here.

## CRFS IQ Recorder

### Connection

| Case | How | Result |
| --- | --- | --- |
| Valid HTTP | Live GET `/` | Pass - `reachable`, accepted |
| Wrong HTTP password | Live | Pass - `authentication_error`, not accepted |
| Wrong SFTP password | Live | Pass - login failed, distinct from HTTP |
| Unreachable IP | `192.0.2.1:80` | Pass - timeout |
| Demo identity | Simulation | Pass - model / firmware / serial / status |
| Switch host token | `test_stale_sensor_info_does_not_overwrite` | Pass |

Live recording POST, timeout-after-accept on hardware, and a second physical sensor at the same IP were **not** run. Duplicate-start protection after hung HTTP is covered in simulation (`test_hung_http_does_not_block_the_next_recording`).

### Recording and files

| Case | How | Result |
| --- | --- | --- |
| Integer Hz, both frequency modes | Unit + GUI tests | Pass |
| Reversed / identical start-end, zero bandwidth, fractional Hz | `test_frequency.py` | Pass - rejected, no silent rounding |
| Empty / zero / invalid time | GUI `_try_payload` | Pass |
| Hardware capability clamp | - | Not implemented (reverted). Invalid combinations are not silently changed. |
| Double-click Start | GUI | Pass - second click ignored |
| Repeat wait seconds/minutes | Code + existing GUI tests | Pass |
| Elapsed timer is not completion | `test_gui_marks_recording_finished_after_recording_time` | Pass - still finalizing / unknown until files settle |
| Growing files / late split parts | `test_collection_excel.py` file-watch | Pass - needs stable size + settle time |
| Midnight folder candidates | `folders_for_recording` | Pass in unit tests |
| Size estimate | 10 MHz x 1 s = 61.051 MB | Pass |

### SFTP

| Case | How | Result |
| --- | --- | --- |
| Up / root / missing today folder | GUI + `test_sftp_paths.py` | Pass |
| Newest-first, NEW, split groups | listing tests + demo window | Pass |
| Download unique names, `.part` not treated as done | `download_util` tests | Pass |
| Keyboard Delete only with list focus | code + demo | Pass in simulation |
| Preview token ignores stale jobs | SFTP window | Pass in code/tests |
| Live Delete | - | **Not executed** |

### Excel

| Case | How | Result |
| --- | --- | --- |
| Grouped split parts, Playback Start `N seconds` | `test_collection_excel.py` | Pass |
| Duplicate prevention / pending queue | Excel log tests | Pass |
| Copy of collection workbook | automated, not the live field file | Pass |
| Excel already open / COM retry | existing writer tests | Pass (simulated busy) |
| VBA macros | - | **Not tested in Excel** |

### IQ Storage (new)

| Case | How | Result |
| --- | --- | --- |
| Read `/mnt/1/remdata/` without today's folder | unit + demo `include_today=False` | Pass |
| Line format MB/GB/TB | unit tests | Pass |
| Warn &lt;10%, critical &lt;5%, no popup spam | GUI demo | Pass - log + yellow/red note |
| Block next recording if estimate + margin will not fit | GUI demo, 10 bytes free vs 10 s capture | Pass |
| Query fail keeps last reading as outdated | GUI demo | Pass |
| Delete increases free (demo) | unit + GUI | Pass |
| Never auto-delete to free space | code review + tests | Pass |
| Live query | SFTP to the sensor | Pass - level ok |

Safety margin: 15% of the empirical estimate, at least 32 MB, covering split parts as one total size.

## IQ Spectrogram Converter

| Case | How | Result |
| --- | --- | --- |
| Stereo IQ tone duration / FFT setup | `test_iq_convert_numerics.py` | Pass |
| Mono, empty, truncated header | same | Pass |
| Hebrew + spaces in output path | same | Pass |
| Existing PNG overwritten | same | Pass (current behaviour) |
| Converter Collection / Results defaults | `test_iq_local_paths.py`, settings on disk | Pass |
| GUI theme smoke | `test_gui_smoke.py` | Pass |
| Folder / drag-and-drop | code paths exist | **Not driven in this session** |
| Cancel mid-convert / close while busy | - | **Not executed** |
| Very large WAV memory | - | **Not executed** |
| Windows EXE without Python | packaged `IQ_Spectrogram_Converter.exe` | Pass - process started and unpacked (~141 MB working set) |

Requested vs actual RBW: ENBW is wider than the requested bin target (Blackman-Harris). That is expected, not a defect. Frequency axis is baseband `-fs/2` .. `+fs/2`.

## Issues

### Fixed in 1.8.0 / 1.3.0

1. **IQ Storage missing** (feature). Sensor free/total now on the main window; recording is blocked when it will not fit; warnings are logged, not popped repeatedly.
2. **`format_bytes` stopped at GB**. File sizes can show TB.
3. **Download and output folders.** Defaults are next to the app or under the user profile. The chosen folders are stored in local settings, not in Git.
4. **Converter taskbar showed the Python icon.** Window icon is applied with the same Sensorz `.ico` as Recorder (`WM_SETICON`). Recorder header uses the Converter Sensorz mark.

### Remaining / not a bug

- Hardware band limits are not enforced (reverted on purpose).
- Live capture and live remote Delete were not run in this QA pass.
- Display scaling 125% / 150% was not checked.

## Severity list

| Severity | Item | Status |
| --- | --- | --- |
| High | Full sensor disk could still start a recording | **Fixed** - pre-record space check |
| Medium | No on-screen free space | **Fixed** - IQ Storage |
| Medium | Converter taskbar icon | **Fixed** |
| Low | Byte formatter had no TB | **Fixed** |
| Info | Download folder is chosen in Settings | Local only |

## Version backup

Tag `v1.8.0` for Recorder 1.8.0 (EXE on the GitHub Release). Converter 1.3.0 is on the same tree; attach `IQ_Spectrogram_Converter.exe` to the same release. Older `v1.7.0` is kept.
