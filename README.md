# CRFS IQ Spectrogram Converter

Sister app of the CRFS IQ Recorder (same Sensorz look, light and dark theme, status bar). Current version: 1.5.0.

Turns a stereo IQ WAV into a PNG. I must be on the left channel and Q on the right. Mono files and ordinary audio are not supported.

The PNG has two plots that share a frequency axis, centered at 0 Hz:

- Spectrum (top): frequency vs bin power in dBFS/bin. Blue is the average. Orange is peak-hold.
- Spectrogram (bottom): frequency vs time. Color is dBFS/bin.

Large files are read in chunks, so the whole capture does not have to sit in RAM.

The CRFS IQ Recorder lives in [`crfs_iq_recorder/`](crfs_iq_recorder/). Use that app to start a recording on a sensor and download WAVE files. System layout, recording flow, and how the two apps hand off WAVE files are in [CRFS IQ Recorder architecture](crfs_iq_recorder/docs/ARCHITECTURE.md).

![Example spectrum and spectrogram](assets/examples/example_spectrum_spectrogram_081608.png)

## What you can do

- Convert one WAV, or every `.wav` in a folder
- Drop a file or folder onto the window
- Set RBW in Hz, or use 5 / 15 / 30 / 50 kHz
- Switch dark / light theme (remembered)
- Pick a colormap, optional frequency smoothing, and auto or manual dB range
- Watch progress, elapsed time, and ETA in the status bar
- Cancel a running conversion (no half-written PNG is left behind)
- Preview the last PNG
- Run the same conversion from the command line, with the source or with the EXE

## Run it on this PC

You need Python 3 on PATH.

1. Double-click `Start IQ Converter.bat`.
2. The first run creates `venv` and installs `requirements.txt`. Later runs just open the GUI.

`Run_IQ_GUI.bat` starts the app without checking packages.

The launcher uses `IQ Collection` (WAVs) and `IQ Results` (PNGs) next to the app. Folders, RBW, theme, and display options are saved in `.iq_gui_settings.json` next to the app. That file is not committed.

### Command line

After the first launcher run:

```text
venv\Scripts\python.exe iq_data.py --input path\to\file.wav --output "IQ Results" --rbw 15000
venv\Scripts\python.exe iq_data.py --folder path\to\wavs --output "IQ Results" --rbw 15000
```

Use either `--input` or `--folder`, not both. Default RBW is 15000 Hz. Optional flags: `--cmap jet|turbo|viridis|gray`, `--freq-smooth`, `--db-min`, `--db-max`.

The EXE converts without opening a window when `--input` is given:

```text
dist\IQ_Spectrogram_Converter.exe --input path\to\file.wav --output "IQ Results" --rbw 15000 --log run.txt
```

The window opens right away and shows "Loading engine..." until scipy and matplotlib have loaded in the background (about a second), then "Ready".

## Build the Windows EXE

On a PC with Python:

1. Double-click `build_exe.bat` (set `IQ_BUILD_NOPAUSE=1` for a scripted build). The first build can take several minutes. The build reads the version from `__version__` in `iq_gui.py` and embeds it, the Sensorz icon, and the company name into the EXE.
2. Run `dist\IQ_Spectrogram_Converter.exe`. You can copy that one file to another Windows PC.

The EXE is not stored in Git. Attach it to a GitHub Release when you share it.

Windows shows "Unknown publisher" on first open because the EXE is not code-signed. `sign_exe.ps1` signs it when a trusted certificate is available; what is needed and what was checked is in [SECURITY_NOTES.md](SECURITY_NOTES.md). Test results for this release are in [QA_REPORT.md](QA_REPORT.md); `qa_run.py` reruns them.

## Files

| Path | What it is |
| --- | --- |
| `iq_gui.py` | Desktop GUI |
| `iq_data.py` | STFT and PNG conversion |
| `iq_theme.py` | Light and dark theme |
| `requirements.txt` | Python packages |
| `IQ_Spectrogram_Converter.spec` | PyInstaller config (version resource, icon, excludes) |
| `pyi_rth_iq.py` | Runtime hook for the EXE (matplotlib cache location) |
| `build_exe.bat` | EXE build |
| `sign_exe.ps1` | Authenticode signing with a trusted certificate |
| `qa_run.py` | Reusable QA suite (engine, GUI, EXE) |
| `Start IQ Converter.bat` | Create venv and launch GUI |
| `assets/` | Icons, logo, example PNG |
| `crfs_iq_recorder/` | CRFS IQ Recorder |

Packages: numpy, matplotlib, scipy, soundfile, Pillow, and tkinterdnd2 (drag-and-drop; the GUI still opens if that import fails). seaborn is no longer needed. Tkinter comes with a typical Windows Python install. `build_exe.bat` also installs PyInstaller.
