# QA report - CRFS IQ Spectrogram Converter 1.4.0

Date: 10 October 2026
Machine: Windows 11 (build 26200), Python 3.13.14, PyInstaller 6.22.0, display at 175% scaling (also exercised at 100% and 150% through Tk scaling).
Previous release: 1.3.0 (tag `converter-v1.3.0`). The earlier combined report for Recorder 1.8.0 / Converter 1.3.0 is in Git history at that tag.

## Summary

| Area | Result |
| --- | --- |
| Automated QA suite `qa_run.py` | 41 cases: 41 pass, 0 fail, 0 skipped (32 s) |
| Existing tests (`test_iq_convert_numerics.py`, `test_iq_local_paths.py`, `test_extract.py`) | 15 pass |
| GUI smoke (`test_gui_smoke.py`) | Pass |
| DSP output versus 1.3.0 engine | Identical (STFT arrays equal within 1e-5 dB, PNG bytes identical) |
| Packaged EXE | Builds, launches, converts with Python removed from PATH |
| Startup time (window visible) | Before: 26.0 s first run, 19.4 s warm median. After: 4.9 s first run, 2.2 s warm median |
| Defender | Scan clean, no detections, no exclusions added |
| Code signing | Not possible: no certificate or signtool on this PC (see SECURITY_NOTES.md) |

## How to rerun

```text
venv\Scripts\python.exe qa_run.py --reference <old iq_data.py> --real-wav <large stereo WAV> --exe dist\IQ_Spectrogram_Converter.exe --report results.json
```

`--reference`, `--real-wav`, and `--exe` are optional; the matching cases are skipped without them. `--no-gui` skips the Tk cases. The suite creates its own synthetic WAV files in a temp folder and never copies or modifies real recordings.

## Engine cases (30)

| Case | Result | Note |
| --- | --- | --- |
| Single file converts to `<stem>.png` | Pass | No temp files left |
| Batch of several files | Pass | 4 PNGs |
| Drag-and-drop string parser (braces, spaces, escapes) | Pass | 8 Tcl list forms |
| Mono WAV | Pass | "Need stereo IQ (I left, Q right)." |
| 0-byte file | Pass | "Could not read this WAV file" |
| Truncated WAV, data cut | Pass | Readable part converts |
| Truncated WAV, header cut | Pass | Rejected cleanly |
| Non-WAV data renamed to .wav | Pass | Rejected cleanly |
| Unsupported encoding (MPEG format tag) | Pass | Rejected cleanly |
| WAV without rf_frequency metadata | Pass | Converts, axis is -fs/2 .. +fs/2 |
| File shorter than one FFT | Pass | "File is too short for this RBW" with the sample counts |
| All-zero silence | Pass | No NaN crash |
| Clipped +/-1.0 samples | Pass | |
| Invalid RBW: 0, negative, text, inf, NaN, empty | Pass | 7 forms rejected with "RBW must be a positive number (Hz)." |
| Enormous RBW (1e12 Hz) | Pass | 2-point FFT still converts |
| dB min >= dB max | Pass | Engine swaps the limits, GUI shows "dB max must be greater than dB min" |
| Bad colormap name | Pass | Falls back to jet |
| Paths with spaces and Hebrew, input and output | Pass | |
| Long filenames (> 200 chars) | Pass | Input path 259 chars, output 380 chars; temp name is short so MAX_PATH is not hit |
| Missing input file | Pass | FileNotFoundError with the path |
| Read-only output folder (deny-write ACL) | Pass | Clean PermissionError; the GUI pre-check catches it before starting |
| Duplicate stems from two folders | Pass | Second file saved as `<stem> (2).png`, no silent overwrite |
| Existing PNG replaced atomically | Pass | Old PNG survives a failed run; no `.part.png` left |
| Cancel mid-conversion | Pass | Stopped at 85% progress, no PNG, no temp file |
| Signal: tone at +f0 lands at +f0 | Pass | Image at -f0 is more than 40 dB down; 45 frames cover the file |
| Signal: full-scale tone reads 0 dBFS/bin | Pass | -0.000 dBFS/bin |
| Signal: two tones separated by more than one RBW | Pass | Valley 47 dB down |
| Signal: reported RBW = ENBW x bin width | Pass | Blackman-Harris 2.0044 bins |
| DSP unchanged versus reference 1.3.0 `iq_data.py` | Pass | Arrays equal, PNG bytes identical |
| Original WAV unchanged (SHA-256 before/after) | Pass | |
| Large real recording (377 MB, 94.3 M frames) | Pass | Converted in chunks of 6.3 M samples in 8.4 s; hash unchanged; file read in place, never copied |

## GUI cases (10)

| Case | Result | Note |
| --- | --- | --- |
| Window opens, engine loads in the background, status becomes Ready | Pass | |
| Convert via button; repeated clicks and Enter ignored while busy | Pass | One job id, Convert disabled, Cancel enabled |
| Cancel stops the job, no PNG, controls recover | Pass | Status "Cancelled" |
| Failed job (mono) then a good job | Pass | Error dialog shown, next job saves a PNG |
| Batch with duplicate names | Pass | `same.png`, `same (2).png`, `other.png` |
| Drop event with braces and spaces | Pass | |
| Theme toggle persists in settings JSON and reloads | Pass | |
| Settings round-trip (output, RBW, colormap, dB range, smoothing) | Pass | |
| Resizing respects the minimum size, controls stay visible | Pass | Minimum 825 x 652 at 100% |
| Close during conversion | Pass | Window closes, worker stops, no PNG written |

Manual checks with screenshots (light and dark, Display section open and closed, themed error dialog, window at minimum size) at Tk scaling 1.33 (100%), 2.0 (150%), and native 2.33 (175%): Convert button, progress bar, and status bar stay visible at the minimum size in all three.

## Packaged EXE (1)

| Case | Result | Note |
| --- | --- | --- |
| Headless conversion from a temp folder with Python removed from the environment | Pass | EXE copied to %TEMP%, PATH stripped of every Python and venv entry, PYTHONHOME / PYTHONPATH / VIRTUAL_ENV unset; PNG created in 7.3 s |

This is a substitute for a clean machine. A true clean VM without Python installed was not available; Windows DLLs and the user profile of the build PC were still present.

## Startup time

Measured with a stopwatch script: launch the EXE, poll until a top-level window whose title contains "IQ Spectrogram Converter" belongs to the process tree, report the elapsed time. Four runs each; the first run after a build is the cold run.

| Build | Run 1 (cold) | Runs 2-4 | Median warm | Size |
| --- | --- | --- | --- | --- |
| 1.3.0 EXE (UPX, seaborn, all matplotlib backends) | 26.0 s | 19.4 s, 18.6 s, 25.3 s | 19.4 s | 95 MB |
| 1.4.0 EXE (this release) | 4.9 s | 2.4 s, 2.2 s, 2.2 s | 2.2 s | 66 MB |
| 1.4.0 EXE, PATH without Python | 2.7 s | 2.4 s, 2.1 s, 2.0 s | 2.1 s | 66 MB |

What changed: UPX off (every launch was decompressing 95 MB), seaborn and unused matplotlib backends dropped, matplotlib font cache moved to a stable per-user folder (it was rebuilt on every launch), scipy and matplotlib imported in a background thread after the window is shown. The window appears with the status "Loading engine..." and switches to "Ready" when the imports finish (about 1 s after the window on this PC).

## Defects found and fixed

1. Cancel arriving while the PNG was being encoded was ignored and the finished PNG was still moved into place. The save step now checks the cancel flag after encoding and deletes the temp file (found by the close-during-conversion case).
2. Opening a second window in the same process reused checkbox images and icons from the first window (`TclError: image "pyimageNN" doesn't exist`). Image elements are now cached per Tk interpreter and all PhotoImages are bound to their own window. Only reachable from tests and tools today, but it was a latent crash.
3. Worker threads called Tk directly (`RuntimeError: main thread is not in main loop` during engine preload). All worker-to-UI updates now go through a queue drained by the main loop.
4. Minimum window height was too large at 175% and the progress bar was hidden when the Display section was opened at minimum size. Log and preview heights were reduced, Files and Resolution merged into one card, and the minimum height grows while Display is open.
5. Clam-theme checkboxes rendered as a small cross; replaced by image-based indicators that match the Recorder.
6. Frozen EXE failed in `scipy.signal` because `numpy.f2py` had been excluded from the bundle (scipy's array-API shim touches every numpy attribute). Removed from the excludes; the EXE case now passes.
7. `pyinstaller.exe` in this venv exits 1 without output; `build_exe.bat` now runs `python -m PyInstaller`.
8. Duplicate output names in a batch silently overwrote each other; a `(2)` suffix is now added.
9. Read-only output folder failed late with a raw OSError; the GUI checks writability before starting and the engine returns a plain-English PermissionError.
10. RBW typed as text, zero, negative, or inf was accepted by the engine; `validate_rbw` now rejects these with one message used by both GUI and CLI.

## Not performed

- No true clean VM. The EXE was run from a temp folder with Python removed from the environment on the build PC only.
- No code signing: there is no Sensorz code-signing certificate or signtool on this PC. SmartScreen will still show "Unknown publisher". See SECURITY_NOTES.md.
- The Downloads sample files named in the brief (`iq_20260628_*`, `iq_20260709_*`) are not on this PC. The large-file case used `D:\CRFS IQ Recorder\Recordings\iq_20260923_142534_0001.wav` (377 MB, stereo, 125 MS/s) read in place.
- Live recordings from a sensor were not made; the extractor (`iq_extract.py`, `iq_extract_gui.py`) was only import-checked and its existing tests run.
- Exported PNG content was compared to 1.3.0 byte for byte; its visual design was deliberately not changed.
