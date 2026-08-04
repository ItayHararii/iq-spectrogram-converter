# IQ Spectrogram Converter

Convert stereo IQ WAV files (I / Q) into spectrogram PNG images.

## Quick start (easiest)

1. Double-click **`Start IQ Converter.bat`**
2. On first run it creates a virtual environment and installs dependencies
3. The GUI opens — browse to a WAV, set RBW if needed, click **Convert & Open Image**

That’s it. Next time, the same `.bat` starts right away.

Optional: after setup, **`Run_IQ_GUI.bat`** launches the app without the dependency check.

## Standalone EXE (optional)

To build a single double-clickable `.exe` (no Python needed on other PCs after build):

1. Double-click **`build_exe.bat`**
2. Wait for the build to finish
3. Run `dist\IQ_Spectrogram_Converter.exe`

## GUI features

- Browse for IQ WAV input and output folder
- RBW presets (5 / 15 / 30 / 50 kHz) plus custom Hz
- Progress bar and activity log (sample rate, duration, etc.)
- Opens the PNG when done; button to open the output folder

Default folders (created automatically):

- `IQ Collection` — handy place for source WAVs
- `IQ Results` — default PNG output

## Command line

```bash
pip install -r requirements.txt
python iq_data.py --input <file.wav> [--output <dir>] [--rbw 15000]
python iq_data.py --folder <wav_folder> [--output <dir>] [--rbw 15000]
```

## Requirements

Python 3 with: `numpy`, `matplotlib`, `seaborn`, `scipy`, `soundfile`
