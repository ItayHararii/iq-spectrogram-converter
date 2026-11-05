# Security notes - CRFS IQ Spectrogram Converter

Why Windows warns when the EXE is opened, what was checked, what was changed,
and what is still needed. Written for release 1.4.0; hash and version updated for 1.6.0 (5 November 2026; still unsigned - no certificate on the build PC).

## Why Windows shows a warning

`IQ_Spectrogram_Converter.exe` is a PyInstaller one-file build. It is not
Authenticode-signed, so Windows SmartScreen reports "Unknown publisher" and,
for a file downloaded from the internet or copied from a share, shows the
"Windows protected your PC" screen until the user clicks "More info" and "Run
anyway". This is SmartScreen reputation, not a malware detection: a new
unsigned file has no download history, so it is treated as unknown.

A one-file PyInstaller EXE also unpacks itself into %TEMP% on every launch,
which is a pattern some antivirus heuristics inspect. Nothing was flagged on
the build PC (see below), but a different corporate AV may still quarantine
an unknown unsigned executable. If that happens, send the file hash below to
the IT team so they can allow-list it, instead of disabling protection.

Three different Windows protections produce three different messages. They
need different actions, so identify which one is talking before changing
anything:

| Message | Protection | Meaning | What helps |
| --- | --- | --- | --- |
| "Windows protected your PC" with a "More info" link and a "Run anyway" button | SmartScreen (reputation) | Unsigned or new file with no download history. Not a malware verdict. | "Run anyway" once; the app then opens normally. Signing with a trusted certificate removes it. |
| "Part of this app has been blocked" or "This app has been blocked by Smart App Control", no "Run anyway" | Smart App Control (Windows 11, enforced on new PCs) | The policy refuses to run unsigned apps whose reputation Microsoft's cloud does not already know. There is no per-file override. | Only a signed release or an app that Microsoft's cloud already classifies as safe runs. Turning Smart App Control off is a one-way system setting the user would have to choose; do not ask for it in the field. |
| Defender notification "Threat found" / the file disappears into quarantine; `Get-MpThreatDetection` lists it | Antivirus detection | A signature or heuristic matched. | Submit the file to Microsoft for analysis, give the SHA-256 to IT for an allow-list entry. Do not disable protection. |

The Recorder's `crfs_iq_recorder/docs/DISTRIBUTION.md` has the full procedure
(including Controlled folder access, which blocks writes into Documents,
Desktop and Pictures) and the diagnosis script
`crfs_iq_recorder/packaging/diagnose_block.ps1`; both apply to this EXE too.

## What was checked on the build PC (10 October 2026)

| Check | Command | Result |
| --- | --- | --- |
| Defender state | `Get-MpComputerStatus` | Antivirus and real-time protection on, signatures 1.459.647.0 (updated 9 Oct 2026) |
| Scan of the EXE | `Start-MpScan -ScanPath <exe> -ScanType CustomScan` | Completed, nothing flagged |
| Detection history | `Get-MpThreatDetection` | No detections for this EXE, none at all in the last 30 days |
| Exclusions | `Get-MpPreference` | No exclusion was added for this app or folder |
| Signature | `Get-AuthenticodeSignature` | NotSigned |
| Code-signing certificates | `Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert`, same for `Cert:\LocalMachine\My` | None installed |
| signtool.exe | PATH and `Windows Kits\10\bin\*\x64` | Not installed |

No Defender exclusion was added and no protection was disabled. A self-signed
certificate was not created: SmartScreen treats a self-signed signature the
same as no signature, so it would only mislead users.

## What changed in this release

1.6.0 (requested-range markers) adds no package, no network access and no
change to the build settings. To label the plots, the app now reads, read-only,
the CRFS `meta` chunk at the end of the WAV, a `<recording>.cellular.json`
manifest beside it, and the CRFS IQ Recorder's recording history (by default
under %LOCALAPPDATA%\CRFS IQ Recorder, or the file named in the settings or the
`IQ_RECORDER_HISTORY` variable). These files are opened with full sharing so
the Recorder can keep replacing them, and nothing is written to them.

The items below were introduced in 1.4.0 and still apply:

- Version resource embedded in the EXE (`IQ_Spectrogram_Converter.spec`):
  CompanyName "Sensorz", ProductName and FileDescription "CRFS IQ Spectrogram
  Converter", FileVersion and ProductVersion 1.4.0, LegalCopyright,
  OriginalFilename. Explorer, Task Manager, and the SmartScreen dialog now show
  a real name instead of blanks.
- Sensorz icon embedded (`--icon`), UPX compression turned off (UPX-packed
  executables are a common heuristic trigger and also slowed startup).
- Unused packages left out of the bundle (seaborn, Qt bindings, test suites).
- `sign_exe.ps1` added: signs with a certificate from the Windows store or a
  PFX kept outside the repo, timestamps with SHA-256, refuses self-signed
  certificates, verifies the result. `build_exe.bat` calls it when
  `CRFS_SIGN_THUMBPRINT` or `CRFS_SIGN_PFX` is set (same variables as the
  Recorder build).

## What is still needed to remove the warning

1. A code-signing certificate from a trusted CA, issued to Sensorz. Options:
   - EV code-signing certificate (hardware token). Signed files start with
     SmartScreen reputation, so the warning normally disappears immediately.
   - OV code-signing certificate. Cheaper, but a newly signed file still has to
     build download reputation before the warning stops; this can take weeks.
   - Azure Trusted Signing (subscription, identity validation of the
     company). Behaves like EV for SmartScreen and does not need a token.
2. Install the Windows SDK "Signing Tools for Desktop Apps" (signtool.exe) on
   the build PC.
3. Sign after every build:
   `powershell -ExecutionPolicy Bypass -File sign_exe.ps1`
   (set `CRFS_SIGN_THUMBPRINT` or `CRFS_SIGN_PFX` + `CRFS_SIGN_PFX_PASSWORD`
   first). Signing uses `/fd SHA256 /td SHA256 /tr http://timestamp.digicert.com`.

Signing does not guarantee that every warning vanishes. SmartScreen reputation
still depends on the certificate type and download history, and third-party
antivirus products apply their own rules.

## Microsoft submission note (for a false positive or to build reputation)

If Defender or SmartScreen flags the file, submit it at
https://www.microsoft.com/wdsi/filesubmission (choose "Software developer").
Use these details for the current build:

| Field | Value |
| --- | --- |
| Product name | CRFS IQ Spectrogram Converter |
| Publisher / company | Sensorz |
| Version | 1.6.0 |
| File name | IQ_Spectrogram_Converter.exe |
| Size | 69,211,800 bytes (66 MB) |
| SHA-256 | BDA825A7A9242563C4758A6B2D1642D0914C163DCA27687D0B734DE66D424E33 |
| Build tool | PyInstaller 6.22.0, Python 3.13.14, one-file, windowed, no UPX |
| What it does | Reads a stereo IQ WAV, computes an STFT, writes a PNG spectrogram. Reads (never writes) the CRFS IQ Recorder's manifest and history files to label the requested frequency range. No network access, no installer, no registry changes. Settings are stored in a JSON file next to the EXE and a matplotlib cache under %LOCALAPPDATA%\CRFS IQ Spectrogram Converter. |

Recompute the hash after every rebuild (`Get-FileHash dist\IQ_Spectrogram_Converter.exe -Algorithm SHA256`);
a new build is a new file to SmartScreen.

## What stays out of Git

Certificates, PFX files, passwords, and thumbprints are read from environment
variables only. The EXE itself, recordings (`*.wav`), and local settings are
ignored by `.gitignore`.
