# IQ Spectrogram Converter

Windows app that turns a **stereo IQ WAV** into a PNG. The WAV must have I on the left channel and Q on the right. Mono or ordinary audio is not supported.

The PNG has two stacked plots that share a frequency axis centered at 0 Hz (half the sample rate on each side):

- **Spectrum (top):** frequency vs bin power in **dBFS/bin**. Blue is the average (Welch mean of STFT frames). Orange is peak-hold. The plot marks the peak frequency and shows peak and noise-floor values.
- **Spectrogram (bottom):** frequency vs time. Color is dBFS/bin.

Large files are read in float32 chunks so the full capture does not have to sit in RAM.

## Run the GUI

**From this folder (needs Python 3 on PATH):**

1. Double-click `Start IQ Converter.bat`.
2. The first run creates a `venv` and installs `requirements.txt`. Later runs skip that and open the GUI.

After that first setup, `Run_IQ_GUI.bat` starts the app without checking packages.

The launcher creates `IQ Collection` (a place to keep WAVs) and `IQ Results` (default PNG output). It also refreshes a desktop shortcut named `IQ Spectrogram Converter.lnk`.

**Standalone EXE (no Python on the PC that runs it):**

1. On a machine with Python, double-click `build_exe.bat`. The first build can take several minutes.
2. Run `dist\IQ_Spectrogram_Converter.exe`. You can copy that one file to another Windows PC.

Next to the EXE, output still defaults to an `IQ Results` folder.

Windows may warn on first open because the EXE is unsigned. If you trust the build, use More info, then Run anyway. The first launch of a one-file EXE can be slower while it unpacks.

## Use

1. Choose a stereo IQ WAV, or drop a `.wav` file (or a folder of them) onto the window.
2. Confirm the output folder (default: `IQ Results` next to the app or EXE).
3. Set **RBW** in Hz, or use a preset: 5, 15, 30, or 50 kHz. Lower RBW gives finer frequency detail and takes longer. Higher RBW is faster.
4. Click **Convert**.

Other controls:

- **Convert folder** processes every `.wav` in that folder (not subfolders).
- **Open image** and **Open output folder**.
- **Open when done** opens the PNG after a single-file convert (not after a batch).
- Dark / light theme, remembered between sessions.
- **Display** (collapsed by default): colormap `jet` (default), `turbo`, `viridis`, or `gray`; **Frequency smooth** (5-tap, off by default); **Auto dB** (2nd-98th percentile of the spectrogram) or manual dB min / max.
- Progress, elapsed time, and ETA. The selected file shows sample rate, duration, and size under the path.
- A preview of the last PNG.

Folders, RBW, theme, and display options are stored in `.iq_gui_settings.json` next to the app.

If a convert runs out of memory, try a higher RBW (30 or 50 kHz) and close other apps.

## What the PNG measures

- **0 dBFS** is a full-scale complex IQ tone with `|a| = 1` (WAV samples in the range -1.0 to +1.0).
- **True RBW** is Blackman-Harris ENBW × FFT bin width. The title shows that value. If it differs from the requested RBW by more than about 2%, the title also shows the requested RBW.
- The spectrum traces use the full FFT. The spectrogram image is peak-pooled to at most 2048 frequency bins × 1536 time bins so the PNG stays bounded.

## Command line

With the project `venv` (after `Start IQ Converter.bat` has run once):

```text
venv\Scripts\python.exe iq_data.py --input path\to\file.wav --output "IQ Results" --rbw 15000
venv\Scripts\python.exe iq_data.py --folder path\to\wavs --output "IQ Results" --rbw 15000
```

`--input` and `--folder` are mutually exclusive. Default RBW is 15000 Hz.

If `--output` is omitted: a single file is written to the current directory; a folder is written to `<folder>_output`. Folder mode only sees `.wav` files in that directory, not subfolders.

Optional flags: `--cmap jet|turbo|viridis|gray`, `--freq-smooth`, `--db-min`, `--db-max`. If you set dB min or max, auto scaling is off.

## Requirements

Python 3, plus the packages in `requirements.txt`:

- numpy
- matplotlib
- seaborn
- scipy
- soundfile
- Pillow
- tkinterdnd2 (drag-and-drop; the GUI still opens if this import fails)

Tkinter comes with a typical Windows Python install. `build_exe.bat` also installs PyInstaller in the venv when you build the EXE.
