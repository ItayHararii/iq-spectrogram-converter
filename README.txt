SENSORZ IQ Spectrogram Converter
================================

Turns a stereo IQ WAV (I left, Q right) into a PNG. Spectrum on top
(dBFS/bin, average + peak-hold) and spectrogram underneath.


RUN THE GUI
-----------
Double-click:  Start IQ Converter.bat

Needs Python 3 on PATH. First run creates a venv and installs
requirements.txt. Later runs just open the app.

After that, Run_IQ_GUI.bat starts the GUI without checking packages.

Optional EXE (no Python on the target PC):
  Double-click build_exe.bat
  Then run dist\IQ_Spectrogram_Converter.exe

The EXE is not stored in Git. Attach it to a GitHub Release when you
share it.


USE
---
- Choose a stereo IQ WAV, or drop a file or folder on the window
- Confirm the output folder (default: IQ Results)
- Set RBW in Hz, or use 5 / 15 / 30 / 50 kHz
- Click Convert

Also: Convert folder (WAVs in that folder only), dark/light theme,
drag-and-drop, Open image / Open output folder, Open when done.
Display options: colormap, frequency smooth (off by default), auto dB.


COMMAND LINE
------------
venv\Scripts\python.exe iq_data.py --input file.wav --output "IQ Results" --rbw 15000
venv\Scripts\python.exe iq_data.py --folder wav_folder --output "IQ Results" --rbw 15000

Optional: --cmap, --freq-smooth, --db-min, --db-max


REQUIREMENTS
------------
Python 3, plus: numpy, matplotlib, seaborn, scipy, soundfile, Pillow,
tkinterdnd2 (see requirements.txt).
