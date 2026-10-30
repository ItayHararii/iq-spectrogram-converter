# Distributing CRFS IQ Recorder on Windows

**Version documented:** 1.8.6 (October 2026)

This page explains why Windows warns about or blocks `CRFS_IQ_Recorder.exe` on
other computers, what was changed in the build, what still has to happen on the
Sensorz side (code-signing enrollment) and on the IT side (managed PCs), and how
to find out what is blocking a specific PC.

Short version:

- "Windows protected your PC / Unknown publisher" = SmartScreen. The EXE is not
  signed. Signing puts the company name in that dialog; the warning itself only
  stops once the signed files have download history.
- "Cannot run at all" on a PC with **Smart App Control** on = unsigned code is
  refused outright, including the DLL/PYD files the EXE unpacks. Only a
  signature from a CA in the Microsoft Trusted Root Program fixes this.
- "Cannot run at all" on a **company-managed** PC = App Control (WDAC),
  AppLocker or a Defender ASR rule. Signing is necessary but IT also has to
  allow the publisher (or the installer path).
- None of this was a Defender detection. Defender scans of every release are
  clean and the event logs show no detections.
- Separately, 1.8.5 and earlier crashed on every exit (Windows Error Reporting
  event, exit code 0xC0000409). That was a Qt thread-shutdown bug, fixed in 1.8.6.

## 1. What was actually blocking execution

Evidence collected on the build PC (Windows 11 Pro 26300, Smart App Control
**On**, standard user) and from the 1.8.5 release artifact:

| Symptom | Mechanism | Evidence | What fixes it |
| --- | --- | --- | --- |
| "Windows protected your PC", "Unknown publisher", Run anyway works | Microsoft Defender SmartScreen | EXE has no Authenticode signature (`Get-AuthenticodeSignature` = NotSigned); reproduced with a Mark-of-the-Web copy of the GitHub download; no SmartScreen policy on the PC | Sign every release with the same trusted identity; reputation then accumulates per publisher instead of per file hash |
| App does not start, no dialog or a brief "blocked" toast | Smart App Control (Windows 11, enforcing) | Registry `CI\Policy\VerifiedAndReputablePolicyState = 1`; `Microsoft-Windows-CodeIntegrity/Operational` events 3077/3033 "did not meet the Enterprise signing level requirements" for the freshly built 1.8.6 EXE, for the installer's extracted `CRFS_IQ_Recorder_Setup.tmp`, and (older) for a bundled `.pyd` unpacked to `%TEMP%\_MEIxxxx`. Event 3118 shows the decision inputs: Defender cloud reputation asked = true, threat named = none, signature count = 0 | Trusted signature on the EXE **and** on every bundled DLL/PYD. There is no per-app exception in Smart App Control |
| App does not start on a work PC | App Control for Business (WDAC) / AppLocker / Defender ASR | Not present on the build PC (no AppLocker rules, no ASR rules, workgroup machine). These are the standard reasons on Intune/GPO-managed PCs; AppLocker default rules forbid running from `%TEMP%`, which is exactly where a one-file PyInstaller EXE unpacks | Signed build plus an IT publisher rule; install to Program Files (installer) instead of running from `%TEMP%` |
| Windows Error Reporting entry on every close | Application bug | 32 `Application` log events 1000 for `CRFS_IQ_Recorder.exe` 1.8.5, exception 0xC0000409 in `Qt6Core.dll`; reproduced from source: `QThread: Destroyed while thread '' is still running` | Fixed in 1.8.6 (`worker_threads.py`): worker threads are stopped before Python tears down |

Why the same unsigned file runs on one Smart App Control PC and not on
another: Smart App Control first asks Microsoft's Intelligent Security Graph
whether it already knows the file as safe (by hash and prevalence), and only
then falls back to the signature. A brand-new build is unknown and unsigned,
so it is blocked; a file that has been downloaded and scanned by many machines
for a while may later be allowed. Observed on the build PC on 30 Oct 2026 with
Smart App Control on: the 1.8.5 release downloaded from GitHub ran (it crashed
on exit, see below), the freshly built unsigned 1.8.6 one-file EXE and the
unsigned installer were blocked, the unsigned folder build ran. Signing is the
only deterministic way through.

The bundle of 1.8.5 contained 89 executable files: 48 signed by their vendors
(Python Software Foundation, The Qt Company, Microsoft) and **41 unsigned**
(numpy/OpenBLAS, cryptography, Pillow, matplotlib, pywin32, bcrypt, PyNaCl,
cffi, charset_normalizer, kiwisolver, contourpy) plus the unsigned outer EXE.
Smart App Control evaluates each of those files when they load, so signing only
the outer EXE would not have been enough. `packaging\verify_bundle_signatures.py`
prints this inventory for any build.

## 2. Code signing: options and decision

Sources: Microsoft Learn "Artifact Signing" quickstart, FAQ and pricing;
"Sign your app for Smart App Control compliance"; "SmartScreen reputation for
Windows app developers"; "Code signing options for Windows app developers"
(all checked October 2026).

| Option | Cost | Who can get it | Key handling | SmartScreen | Smart App Control | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| **Azure Artifact Signing** (formerly Trusted Signing) | $9.99/month Basic (5,000 signatures, then $0.005 each); $99.99 Premium. Billed per account month, not pro-rated. Needs a paid Azure subscription (no free/trial) | Organizations in the USA, Canada, EU, UK, Australia, New Zealand, Japan, South Korea, Singapore, Switzerland, Norway and **Israel** (quickstart prerequisites). Individuals only USA/Canada | Microsoft-managed HSM; no token to ship or lose; short-lived certificates, so timestamping is mandatory | Publisher name shown; reputation builds over releases like any signature | Explicitly supported ("Public Trust ... Smart App Control in Windows 11") | Identity validation 1-20 business days. Recommended by Microsoft for non-Store distribution |
| OV code-signing certificate (DigiCert, Sectigo, GlobalSign, SSL.com, Certum) | Roughly $150-300/year plus a hardware token or cloud HSM | Worldwide | Since June 2023 the private key must live on FIPS hardware (USB token or cloud HSM); a plain PFX is no longer issued | Same as above | Yes, if the CA is in the Microsoft Trusted Root Program | Token must be on the build PC, or extra cloud-signing fees |
| EV code-signing certificate | $400+/year, token required | Worldwide | Hardware token | **No longer** an instant SmartScreen bypass (Microsoft, 2024) | Yes | Not worth the premium for this app |
| Microsoft Store (MSIX) | Free | Worldwide | Store re-signs | No warnings | Yes | Public store listing for an internal lab tool; MSIX container semantics (Excel COM, paths) untested. Not pursued |
| Self-signed certificate | Free | - | - | Treated like unsigned | Blocked | Refused by `sign_exe.ps1` |

**Decision: Azure Artifact Signing, Basic SKU.** Cheapest, no hardware to
manage, documented Smart App Control support, and SENSORZ as an Israeli
organization is in the supported list. Fallback if the organization validation
is refused: an OV certificate on a USB token from one of the CAs above, used
through the same `sign_exe.ps1` (`CRFS_SIGN_THUMBPRINT` mode).

The build integration is complete and was exercised end to end (see section 4).
**No release is signed yet.** Nothing was purchased. The steps below are what
is still needed.

### 2.1 Enrollment steps (Itay / Sensorz, about 1 hour of work plus the validation wait)

1. Azure: use a **paid** Azure subscription in the Sensorz Microsoft Entra
   tenant (pay-as-you-go is enough; free/trial/sponsored subscriptions are
   rejected). Register the resource provider `Microsoft.CodeSigning`
   (Subscription -> Resource providers).
2. Create an **Artifact Signing account**: resource group, name (for example
   `sensorz-signing`), region **West Europe** (`https://weu.codesigning.azure.net`)
   or North Europe, SKU **Basic**. Billing starts when the account exists.
3. Assign yourself the roles **Artifact Signing Identity Verifier** and
   **Artifact Signing Certificate Profile Signer** on the account (Access
   control / IAM).
4. **Identity validation -> Organization -> Public.** Enter the legal entity
   name exactly as registered, website, a monitored primary e-mail on the
   company domain, a second company e-mail, business identifier (company
   registration number), business address, and the first/last name of the
   person who completes the individual verification (government ID through
   Microsoft Authenticator Verified ID). Processing time is 1-20 business
   days; Microsoft may ask for registration documents from the last 12 months.
5. When the validation is **Completed**, create a **Certificate profile** of
   type **Public Trust** (for example `sensorz-public`) linked to that identity.
   The certificate subject becomes the validated legal name; it cannot be
   customized.
6. On the build PC, create `C:\secure\artifact-signing.json` (outside the
   repository; `.gitignore` already excludes these names):

   ```json
   {
     "Endpoint": "https://weu.codesigning.azure.net",
     "CodeSigningAccountName": "sensorz-signing",
     "CertificateProfileName": "sensorz-public"
   }
   ```

   Save it as UTF-8 **without** BOM (Windows PowerShell's `Set-Content
   -Encoding UTF8` adds one; `sign_exe.ps1` strips it, but other tools do not).
7. Sign in once on the build PC: install Azure CLI (`winget install
   Microsoft.AzureCLI`) and run `az login` with the account that has the Signer
   role. For unattended builds create a service principal with that role and
   set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` instead.
8. Set `CRFS_SIGN_ARTIFACT_METADATA=C:\secure\artifact-signing.json` (user
   environment variable) and run `build_exe.bat`. The first run downloads
   `signtool.exe` (NuGet `Microsoft.Windows.SDK.BuildTools`) and the plug-in
   `Azure.CodeSigning.Dlib.dll` (NuGet `Microsoft.ArtifactSigning.Client`) into
   `.signing-tools\` (git-ignored). .NET 8 runtime is required (present on the
   build PC). Each build uses about 45 signatures out of the 5,000 monthly quota.

Costs: $9.99 per month while the account exists (about $120/year). No other
fees for this volume.

### 2.2 What signing does and does not change

- **Does:** SmartScreen shows "Publisher: <legal name>" instead of "Unknown
  publisher"; Smart App Control allows the app (all files are signed by a
  trusted CA); IT can write one WDAC/AppLocker **publisher** rule that covers
  every future release; Explorer's Properties shows a Digital Signatures tab;
  tampering with the file breaks the signature.
- **Does not, by itself:** remove the SmartScreen warning on the very first
  downloads of a new release. Microsoft is explicit that reputation builds
  from download volume and that there is no manual submission for consumer
  SmartScreen. Signing every release with the same identity is what lets the
  publisher reputation carry over, so the warnings fade across releases rather
  than restarting at zero for each new file hash. IT can also submit the file
  through the Microsoft Security Intelligence portal to speed up trust for
  managed deployments.
- **Does not:** override company policies. A WDAC or AppLocker policy that only
  allows approved publishers still needs IT to add this publisher. An ASR rule
  "Block executable files from running unless they meet a prevalence, age, or
  trusted list criterion" still needs an IT exclusion or indicator.

## 3. Build artifacts and channels

`build_exe.bat` produces:

| Artifact | For | Notes |
| --- | --- | --- |
| `dist\CRFS_IQ_Recorder.exe` | Portable use on personal or lightly managed PCs | One file; unpacks into `%TEMP%\_MEIxxxx` on every start (slower start, blocked by AppLocker `%TEMP%` rules) |
| `dist\CRFS_IQ_Recorder_Setup.exe` (with `CRFS_BUILD_INSTALLER=1`) | Managed PCs, IT deployment, anyone who prefers an installed app | Inno Setup; installs the folder layout to `Program Files\CRFS IQ Recorder` (admin) or `%LOCALAPPDATA%\Programs` (per user); Start-menu shortcut; uninstaller; silent switches `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`; settings in `%LOCALAPPDATA%\CRFS IQ Recorder` are kept on uninstall |

Repackaging is not a trust mechanism: an unsigned installer is treated by
SmartScreen and Smart App Control exactly like an unsigned EXE (observed: Smart
App Control blocked the unsigned installer's extracted setup program
`%TEMP%\is-xxxx.tmp\CRFS_IQ_Recorder_Setup.tmp`, CodeIntegrity 3077). The
installer solves the `%TEMP%` execution problem for the *installed app* and
gives IT a fixed path and a normal install/uninstall; signing is what
identifies the publisher. When signing is configured, Inno Setup signs the
`Setup.exe` loader, the embedded setup program that becomes that `.tmp`
(`SignedUninstaller=yes` signs the shared setup/uninstaller image before it is
embedded, verified in the Inno Setup compiler source), and therefore the
uninstaller too, so no unsigned executable is ever run from the installer.

Both artifacts are attached to the GitHub Release for the tag. The EXE is not
stored in Git.

### 3.1 For company IT (managed PCs)

Once releases are signed, the simplest durable approval is a **publisher
rule** so that every future version is covered:

- **App Control for Business (WDAC) / Intune:** add an allow rule at the
  Publisher level for the signing certificate (leaf CN = Sensorz legal name,
  issuer chain Microsoft Identity Verification Root Certificate Authority 2020
  for Artifact Signing). If the policy relies on the Intelligent Security
  Graph, new signed files may still be unknown for a while; an explicit rule
  avoids that. Deploy the installer as an Intune Win32 app (`/VERYSILENT
  /SUPPRESSMSGBOXES /NORESTART`) so the app runs from Program Files.
- **AppLocker:** publisher rule for the same certificate on the Executable and
  DLL collections, or a path rule for `%PROGRAMFILES%\CRFS IQ Recorder\*`.
  The portable EXE will keep failing under AppLocker because it executes DLLs
  from `%TEMP%`; use the installer.
- **Defender ASR** rule `01443614-cd74-433a-b99e-2ecdc07bfc25` ("Block
  executable files from running unless they meet a prevalence, age, or trusted
  list criterion") blocks new binaries regardless of signature; exclude the
  install folder or add a certificate indicator in Defender for Endpoint.
- **SmartScreen "Warn and prevent bypass"** policy: users cannot click Run
  anyway; distribute from an intranet share or install via Intune, and/or
  submit the signed file through the Microsoft Security Intelligence portal.
- Until a signed release exists, IT can only allow by **hash** or **path**,
  which has to be repeated for every version.

Nothing here asks IT to disable SmartScreen, Smart App Control or Defender,
or to add broad antivirus exclusions, and users should not remove the Mark of
the Web from downloads.

## 4. Repeatable build, sign and verify process

```
rem on the build PC, once per session
set CRFS_SIGN_ARTIFACT_METADATA=C:\secure\artifact-signing.json
set CRFS_BUILD_INSTALLER=1
build_exe.bat
```

What happens:

1. PyInstaller analysis; the spec filters Qt to what the app uses.
2. `CRFS_IQ_Recorder.spec` copies every DLL/PYD/EXE that will be bundled to
   `build\signed_binaries\`, runs `sign_exe.ps1 -FileList ... -SkipSigned`, and
   packs the signed copies. Vendor-signed files are left untouched; in 1.8.6
   that is 41 files to sign. The venv is never modified.
3. `sign_exe.ps1` signs `dist\CRFS_IQ_Recorder.exe` (SHA-256 digest, RFC 3161
   timestamp from `http://timestamp.acs.microsoft.com` for Artifact Signing,
   DigiCert for certificate-store signing).
4. `packaging\verify_bundle_signatures.py` unpacks the one-file EXE with
   PyInstaller's reader and prints the signature status of all ~90 files.
5. With `CRFS_BUILD_INSTALLER=1`: a second PyInstaller run in folder layout
   (`CRFS_BUILD_ONEDIR=1`, output `dist\onedir\CRFS_IQ_Recorder\`), the folder
   EXE is signed, Inno Setup compiles `packaging\CRFS_IQ_Recorder.iss`, and
   Inno signs Setup and the uninstaller through the same script.

Without signing credentials the script exits with code 2, the build continues
unsigned, and the bat says so. Signing modes, in the order checked:
`CRFS_SIGN_ARTIFACT_METADATA` (Artifact Signing), `CRFS_SIGN_THUMBPRINT`
(certificate in the Windows store, including USB tokens), `CRFS_SIGN_PFX` +
`CRFS_SIGN_PFX_PASSWORD`. Self-signed certificates are refused unless the
explicit self-test switch is used, and that output must never be shipped.

Manual checks after a signed build:

```
powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -VerifyOnly
.venv\Scripts\python.exe packaging\verify_bundle_signatures.py dist\CRFS_IQ_Recorder.exe
.venv\Scripts\python.exe packaging\verify_bundle_signatures.py dist\CRFS_IQ_Recorder_Setup.exe
```

Verification done for 1.8.6 on the build PC:

- The whole pipeline (41 bundled files + EXE + folder EXE + installer +
  uninstaller) was run with a throwaway self-signed certificate in self-test
  mode; all 89 bundled files ended up signed and the installer installed,
  launched, exited cleanly and uninstalled silently. That build was discarded.
- The Artifact Signing path was run up to the point where the plug-in submits
  the digest to Azure (it stops at authentication without an account), so the
  tool download, metadata parsing and signtool/dlib versions are known to work.
- The released (unsigned) 1.8.6 artifacts: Defender custom scan clean for the
  EXE and the installer; version resources 1.8.6; the folder build launched,
  opened and closed with exit code 0 and no Windows Error Reporting event;
  the one-file EXE and the installer could not be launched on the build PC
  because Smart App Control refused them (CodeIntegrity 3077/3118), which is
  the behaviour documented in section 1, not a regression. Full test suite:
  188 passed, 1 skipped.
- A real signed artifact and the first-run experience on a second, protected
  PC can only be tested after enrollment.

## 5. Finding out what blocks a specific PC

Ask the person to run, from the folder that has the downloaded EXE:

```
powershell -ExecutionPolicy Bypass -File diagnose_block.ps1
```

(`packaging\diagnose_block.ps1` in this repository; copy it next to the EXE or
pass `-Path`.) It needs no admin rights, changes nothing, prints the result and
writes `Desktop\CRFS_IQ_Recorder_diagnostics.txt` with user names and IP
addresses redacted. It reports: Windows build and management state, the file's
signature and Mark of the Web, Smart App Control state, WDAC enforcement and
active policies, AppLocker rules, SmartScreen settings and policies, Defender
mode, ASR rules and detections, and the relevant events from the last 30 days
(CodeIntegrity 3077/3033, AppLocker 8004, Defender 1116/1117/1121/1122,
Application 1000), ending with a plain-language summary of which mechanism
applies.

## 6. The exit crash (fixed in 1.8.6)

Every exit of 1.8.5 and earlier left an `Application` log event 1000
(exception 0xC0000409, fail-fast code 7, in `Qt6Core.dll`). Cause: when the
window closed while a sensor check, IQ-storage query or Sensor Files
connection was still in flight, Python's interpreter shutdown deleted the
`QThread` objects while the threads were running; Qt treats that as fatal
(`QThread: Destroyed while thread '' is still running`) and terminates the
process. Fix: `crfs_iq_recorder/worker_threads.py` tracks every worker thread;
`gui.run()` asks them to abort, waits up to 8 s, and deliberately leaks any
thread that is still blocked (then skips interpreter teardown) instead of
deleting it. The test suite stops the same threads at session end, which is
why the full `pytest` run also used to end with the same exit code.

Note: these crash reports were never a security block, but they do show up in
Reliability Monitor and Windows Error Reporting, which makes an unsigned tool
look worse to IT than it is.
