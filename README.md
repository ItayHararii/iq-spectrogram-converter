# SENSORZ IQ Spectrogram Converter

Windows app that turns a **stereo IQ WAV** into a PNG. The WAV must have I on the left channel and Q on the right. Mono or ordinary audio is not supported.

The PNG has two stacked plots that share a frequency axis centered at 0 Hz:

- **Spectrum (top):** frequency vs bin power in **dBFS/bin**. Blue is the average. Orange is peak-hold.
- **Spectrogram (bottom):** frequency vs time. Color is dBFS/bin.

Large files are read in float32 chunks so the full capture does not have to sit in RAM.

The **CRFS IQ Recorder** (start recordings on a sensor and download WAVE files) is in [`crfs_iq_recorder/`](crfs_iq_recorder/).

![Example spectrum and spectrogram](assets/examples/example_spectrum_spectrogram_081608.png)

## Features

- Convert one file or every `.wav` in a folder
- Drag-and-drop a file or folder onto the window
- RBW in Hz, with 5 / 15 / 30 / 50 kHz presets
- Dark / light theme, remembered between sessions
- Colormap, optional frequency smoothing, auto or manual dB range
- Progress, elapsed time, and ETA
- Preview of the last PNG
- Command-line conversion with the same engine

## Run locally

Needs **Python 3** on PATH.

1. Double-click `Start IQ Converter.bat`.
2. The first run creates `venv` and installs `requirements.txt`. Later runs open the GUI.

`Run_IQ_GUI.bat` starts the app without checking packages.

The launcher creates `IQ Collection` (WAVs) and `IQ Results` (PNGs). Folders, RBW, theme, and display options are stored in `.iq_gui_settings.json` next to the app (not committed).

### Command line

After the first launcher run:

```text
venv\Scripts\python.exe iq_data.py --input path\to\file.wav --output "IQ Results" --rbw 15000
venv\Scripts\python.exe iq_data.py --folder path\to\wavs --output "IQ Results" --rbw 15000
```

`--input` and `--folder` are mutually exclusive. Default RBW is 15000 Hz. Optional flags: `--cmap jet|turbo|viridis|gray`, `--freq-smooth`, `--db-min`, `--db-max`.

## Build the Windows EXE

On a machine with Python:

1. Double-click `build_exe.bat` (first build can take several minutes).
2. Run `dist\IQ_Spectrogram_Converter.exe`. Copy that one file to another Windows PC.

Packaging uses `IQ_Spectrogram_Converter.spec` and `assets/sensorz_icon.ico`. The EXE is **not** stored in Git. Attach a versioned file to a GitHub Release when you distribute it.

Windows may warn on first open because the EXE is unsigned. The first launch of a one-file EXE can be slower while it unpacks.

## Layout

| Path | Role |
| --- | --- |
| `iq_gui.py` | Desktop GUI |
| `iq_data.py` | STFT / PNG conversion |
| `iq_theme.py` | Light and dark theme |
| `requirements.txt` | Runtime Python packages |
| `IQ_Spectrogram_Converter.spec` | PyInstaller config |
| `build_exe.bat` | One-click EXE build |
| `Start IQ Converter.bat` | Create venv and launch GUI |
| `assets/` | Sensorz icons, logo, example PNG |

## Requirements

See `requirements.txt`: numpy, matplotlib, seaborn, scipy, soundfile, Pillow, tkinterdnd2 (drag-and-drop; the GUI still opens if that import fails). Tkinter comes with a typical Windows Python install. `build_exe.bat` also installs PyInstaller when you build the EXE.
