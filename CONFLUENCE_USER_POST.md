# IQ Spectrogram Converter - install and use

Windows app that turns a **stereo IQ WAV** into one PNG. I is the left channel, Q is the right. Mono or ordinary audio is not supported.

The PNG has two panels that share a frequency axis:

- **Top - spectrum:** frequency vs **Bin Power (dBFS/bin)**. Blue is the average (Welch mean of STFT frames). Orange is peak-hold. Peak frequency is marked, with peak and noise-floor labels.
- **Bottom - spectrogram:** frequency vs time. Color is dBFS/bin (colormap `jet` by default; `turbo`, `viridis`, and `gray` are available).

If you use the shared EXE, you do not need Python.

## What you can do

- Browse to a WAV, or drop a file or folder on the window
- Convert one file, or **Convert folder** for every `.wav` in that folder (not subfolders)
- Remembers last folders, RBW, theme, and display settings
- Shows sample rate, duration, size, progress, and ETA
- **Open image** / **Open output folder**
- Dark / light theme
- Large files are read in float32 chunks so the whole capture does not have to load at once

## Get started

1. Download the shared zip (or the EXE attached on this page).
2. Unzip if needed, then run **`IQ_Spectrogram_Converter.exe`**.

No installer. No Python on the PC that only runs the EXE.

Windows may show SmartScreen on first run ("Windows protected your PC") because the EXE is unsigned. If you trust this internal build: **More info**, then **Run anyway**. The first launch of a one-file EXE can be slower while it unpacks.

Do not run `build_exe.bat` unless you are packaging a new EXE.

If you have the project folder and Python 3 on PATH: double-click **`Start IQ Converter.bat`**. The first run creates a venv and installs packages.

## How to convert

1. Choose a stereo IQ WAV, or drop a file or folder on the window.
2. Confirm the **output folder** (default: `IQ Results` next to the EXE or project folder).
3. Set **RBW** - presets **5 / 15 / 30 / 50 kHz**, or type a value in Hz.
   Lower RBW = finer frequency detail (slower). Higher RBW = faster overview.
4. Optional **Display** settings:
   - Colormap: jet (default), turbo, viridis, gray
   - **Frequency smooth** - off by default (5-tap along frequency)
   - **Auto dB** (2nd-98th percentile), or set dB min / max yourself
5. Click **Convert** (one file) or **Convert folder**.
6. Watch the progress bar and ETA. When it finishes, use **Open image** or **Open output folder**. Tick **Open when done** if you want a single-file PNG to open automatically.

## What is in the PNG

- **Spectrum (top):** frequency vs dBFS/bin, average + peak-hold, peak marker, peak and noise-floor labels. Title shows the filename and true RBW (and the requested RBW if they differ by more than about 2%).
- **Spectrogram (bottom):** frequency (X) vs time (Y). Color = dBFS/bin.

**Example: spectrum (top) + spectrogram (bottom) from a stereo IQ WAV**

![Example: spectrum (top) + spectrogram (bottom) from a stereo IQ WAV - narrowband capture with CW-like lines](assets/examples/example_spectrum_spectrogram_081608.png)

**Example: wider-band capture (same two-panel layout)**

![Example: wider-band capture (same two-panel layout)](assets/examples/example_spectrum_spectrogram_101228.png)

## Accuracy notes

- **0 dBFS** is a full-scale complex IQ tone with `|a| = 1` (WAV samples in the range -1.0 to +1.0).
- **True RBW** is Blackman-Harris ENBW × bin width.
- Frequency smooth is off by default.

## Requirements and tips

- Windows PC
- Stereo IQ WAV only (I left / Q right)
- Default output: `IQ Results` beside the EXE or project folder
- You can pin the EXE to the taskbar when the Sensorz icon is present
- If a convert runs out of memory, try a higher RBW (30 or 50 kHz) and close other apps
