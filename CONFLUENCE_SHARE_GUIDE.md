# IQ Spectrogram Converter - share guide

How to package and share the converter with people who only need to run it.

## What it is

The app turns stereo IQ WAV recordings into PNG images.

- Input: stereo WAV with I on the left channel and Q on the right
- Output: PNG with a spectrum (dBFS/bin) on top and a spectrogram underneath. Default folder: `IQ Results`
- UI: choose a WAV, set RBW, click Convert. You can also drop a file or convert a folder.

## Preferred share: standalone EXE

Recipients do not need Python.

1. Build (or reuse) `dist\IQ_Spectrogram_Converter.exe`
2. Zip that single file
3. Upload the zip (OneDrive, SharePoint, or a Confluence attachment)
4. Recipients unzip and run the EXE

PNGs go to an `IQ Results` folder next to the EXE (created on first convert if needed).

If the EXE is not practical, share the project folder and ask people to run `Start IQ Converter.bat`. That needs Python 3 on PATH and a first-run venv install. Prefer the EXE when you can.

## Publisher: build the EXE

On a Windows machine with Python 3 on PATH:

1. Open the project folder
2. Double-click `build_exe.bat`
3. Wait for the build (the first one can take several minutes)
4. Confirm:

   `dist\IQ_Spectrogram_Converter.exe`

Rebuild when the app changes. Reuse the same EXE until then.

## Publisher: package and upload

1. Copy `dist\IQ_Spectrogram_Converter.exe`
2. Zip it (for example `IQ_Spectrogram_Converter.zip`). A zip is easier to transfer than a raw `.exe`.
3. Upload to OneDrive, SharePoint, or attach it on a Confluence page
4. Point teammates at this guide, or paste the "How recipients use it" section below

Optional note on the upload: Windows may show a SmartScreen warning on first run (see Notes).

## How recipients use it

1. Download the zip and extract it (Desktop or Documents is fine)
2. Run `IQ_Spectrogram_Converter.exe`
3. Choose a stereo IQ WAV (I left / Q right), or drop a file or folder on the window
4. Confirm the output folder (default: `IQ Results` next to the EXE)
5. Set RBW if needed (presets 5 / 15 / 30 / 50 kHz, or custom Hz)
6. Click Convert
7. When it finishes, use Open image or Open output folder. Open when done opens the PNG after a single-file convert.

No extra install. No Python on that PC.

## Notes

- Windows SmartScreen: the EXE is unsigned, so Windows may warn on first open ("Windows protected your PC"). Choose More info, then Run anyway, if you trust the internal build.
- First launch: a one-file EXE can be slower the first time while it unpacks.
- Taskbar: you can pin the EXE. It uses the Sensorz icon when that file is bundled.
- WAV format: stereo IQ only. Mono or non-IQ audio is not supported.
- Output: PNGs default to `IQ Results` beside the EXE. You can pick another folder in the UI.
- Large files: the app reads IQ in float32 chunks. If a convert runs out of memory, try 30 or 50 kHz RBW.

## Fallback: project folder + Python

Use this only when you cannot share or run the EXE.

1. Share the project folder (or a zip of it)
2. Recipient needs Python 3 with Add python.exe to PATH
3. Double-click `Start IQ Converter.bat`
4. First run creates a virtual environment and installs `requirements.txt`
5. Same GUI workflow: WAV, Convert, PNG in `IQ Results`

After setup, `Run_IQ_GUI.bat` starts the GUI without checking packages.

## Quick reference

- Publisher: run `build_exe.bat`, zip `dist\IQ_Spectrogram_Converter.exe`, then upload
- Recipient: unzip, run the EXE, convert a stereo IQ WAV, PNG lands in `IQ Results`

Paste this page into Confluence as-is. Add the zip link after you attach it.
