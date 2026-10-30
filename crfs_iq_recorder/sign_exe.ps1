<#
.SYNOPSIS
  Authenticode-sign CRFS IQ Recorder build outputs with a trusted code-signing identity.

.DESCRIPTION
  Signing names the publisher to Windows. It turns "Unknown publisher" into the
  company name in SmartScreen and Explorer, and it is what Smart App Control and
  App Control (WDAC) publisher rules check. It does not by itself give a new file
  SmartScreen reputation; that still accumulates from clean downloads.

  Smart App Control checks every EXE, DLL and PYD the app loads, so build_exe.bat
  runs this script on the bundled binaries (via the .spec) and on the final EXE
  and installer. Vendor files that already carry a valid signature (Python, Qt,
  Microsoft) are left untouched when -SkipSigned is given.

  No key or credential lives in this repository. Pick one mode with environment
  variables, checked in this order:

    1. Azure Artifact Signing (formerly Trusted Signing) - Microsoft-managed,
       recommended. Point at the metadata JSON that names your account:
         $env:CRFS_SIGN_ARTIFACT_METADATA = "C:\secure\artifact-signing.json"
       The JSON has Endpoint, CodeSigningAccountName, CertificateProfileName
       (see docs\DISTRIBUTION.md). Authentication uses Azure DefaultAzureCredential:
       run "az login" first, or set AZURE_TENANT_ID / AZURE_CLIENT_ID /
       AZURE_CLIENT_SECRET for a service principal. signtool.exe and the
       Azure.CodeSigning.Dlib.dll plug-in are downloaded from NuGet into
       .signing-tools\ (git-ignored) on first use; .NET 8 runtime is required.

    2. A certificate in the Windows certificate store (hardware token or HSM
       provider counts):
         $env:CRFS_SIGN_THUMBPRINT = "<SHA1 thumbprint of the code-signing cert>"

    3. A PFX file plus its password, both outside the repo (only possible for
       certificates issued before the June 2023 hardware-key requirement):
         $env:CRFS_SIGN_PFX = "C:\secure\company-codesign.pfx"
         $env:CRFS_SIGN_PFX_PASSWORD = "<password>"

  Optional:
         $env:CRFS_TIMESTAMP_URL   override the RFC 3161 timestamp server
         $env:CRFS_SIGNTOOL        path to a specific signtool.exe
         $env:CRFS_SIGN_DLIB       path to a specific Azure.CodeSigning.Dlib.dll

  Self-signed certificates are refused: SmartScreen and Smart App Control treat
  them like no signature, and they would only mislead users.

.PARAMETER Files
  Files to sign. Default: dist\CRFS_IQ_Recorder.exe. Several paths may be
  separated with ";" (handy when calling through powershell -File).
.PARAMETER FileList
  Text file with one path per line (used by the .spec for bundled binaries).
.PARAMETER SkipSigned
  Leave files that already have a valid Authenticode signature alone.
.PARAMETER VerifyOnly
  Do not sign; only report the signature status of the files.
.PARAMETER AllowUntrustedTestCert
  Build-pipeline self-test only: accept a self-signed certificate from the store
  and treat its (untrusted) signatures as present. Output built this way must
  never be distributed. Also enabled by CRFS_SIGN_ALLOW_TEST_CERT=1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File sign_exe.ps1
  powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -Files dist\CRFS_IQ_Recorder.exe, dist\CRFS_IQ_Recorder_Setup.exe
  powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -VerifyOnly

.NOTES
  Exit codes: 0 signed and verified, 1 failure, 2 no signing credentials configured.
#>
[CmdletBinding()]
param(
    [string[]]$Files = @(),
    [string]$FileList = "",
    [switch]$SkipSigned,
    [switch]$VerifyOnly,
    [switch]$AllowUntrustedTestCert,
    [string]$Description = "CRFS IQ Recorder"
)

$ErrorActionPreference = "Stop"
$toolsDir = Join-Path $PSScriptRoot ".signing-tools"
if ($env:CRFS_SIGN_ALLOW_TEST_CERT -eq "1") { $AllowUntrustedTestCert = $true }
if ($AllowUntrustedTestCert) {
    Write-Warning "Test mode: untrusted test certificate accepted. Do not distribute this output."
}

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
if ($FileList) {
    if (-not (Test-Path -LiteralPath $FileList)) { Write-Error "File list not found: $FileList" }
    $Files += Get-Content -LiteralPath $FileList | Where-Object { $_.Trim() }
}
if (-not $Files -or $Files.Count -eq 0) {
    $Files = @((Join-Path $PSScriptRoot "dist\CRFS_IQ_Recorder.exe"))
}
$Files = @($Files | ForEach-Object { $_ -split ";" } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f)) { Write-Error "File not found: $f" }
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Get-NuGetPackage {
    param([string]$Id, [string]$Dest)
    $target = Join-Path $Dest $Id
    if (Test-Path -LiteralPath $target) { return $target }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $lower = $Id.ToLowerInvariant()
    Write-Host "Downloading NuGet package $Id ..."
    $index = Invoke-RestMethod -Uri "https://api.nuget.org/v3-flatcontainer/$lower/index.json" -TimeoutSec 60
    $version = @($index.versions | Where-Object { $_ -notmatch "-" }) | Select-Object -Last 1
    if (-not $version) { Write-Error "No stable version of $Id on nuget.org." }
    $zip = Join-Path $Dest "$Id.$version.zip"
    Invoke-WebRequest -Uri "https://api.nuget.org/v3-flatcontainer/$lower/$version/$lower.$version.nupkg" -OutFile $zip -TimeoutSec 300
    Expand-Archive -LiteralPath $zip -DestinationPath $target -Force
    Remove-Item -LiteralPath $zip -Force
    Write-Host "  -> $target ($version)"
    return $target
}

function Find-SignTool {
    if ($env:CRFS_SIGNTOOL -and (Test-Path -LiteralPath $env:CRFS_SIGNTOOL)) { return $env:CRFS_SIGNTOOL }
    $local = Get-ChildItem (Join-Path $toolsDir "Microsoft.Windows.SDK.BuildTools\bin\*\x64\signtool.exe") -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($local) { return $local.FullName }
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe",
        "${env:ProgramFiles}\Windows Kits\10\bin\*\x64\signtool.exe"
    )
    foreach ($pattern in $kits) {
        $found = Get-ChildItem $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    # Not installed anywhere: fetch the SDK build tools (signtool only needs the bin folder).
    $pkg = Get-NuGetPackage -Id "Microsoft.Windows.SDK.BuildTools" -Dest $toolsDir
    $found = Get-ChildItem (Join-Path $pkg "bin\*\x64\signtool.exe") -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Find-ArtifactSigningDlib {
    if ($env:CRFS_SIGN_DLIB -and (Test-Path -LiteralPath $env:CRFS_SIGN_DLIB)) { return $env:CRFS_SIGN_DLIB }
    $candidates = @(
        (Join-Path $toolsDir "Microsoft.ArtifactSigning.Client\bin\x64\Azure.CodeSigning.Dlib.dll"),
        "${env:ProgramFiles}\Artifact Signing Client Tools\Azure.CodeSigning.Dlib.dll",
        "${env:ProgramFiles}\Trusted Signing Client Tools\Azure.CodeSigning.Dlib.dll"
    )
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { return $c } }
    $pkg = Get-NuGetPackage -Id "Microsoft.ArtifactSigning.Client" -Dest $toolsDir
    $found = Get-ChildItem -Path $pkg -Recurse -Filter "Azure.CodeSigning.Dlib.dll" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\" } | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Get-SignatureRows {
    param([string[]]$Paths)
    foreach ($p in $Paths) {
        $s = Get-AuthenticodeSignature -LiteralPath $p
        [pscustomobject]@{
            File   = $p
            Status = [string]$s.Status
            Signer = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { "" }
        }
    }
}

function Test-ValidSignature {
    param([string]$Path)
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    if ($sig.Status -eq "Valid") { return $true }
    # Test mode: a signature from the untrusted test certificate counts as present.
    return ($AllowUntrustedTestCert -and $sig.SignerCertificate -and $sig.Status -ne "HashMismatch")
}

# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------
if (-not $VerifyOnly) {
    $mode = $null
    if ($env:CRFS_SIGN_ARTIFACT_METADATA) { $mode = "artifact" }
    elseif ($env:CRFS_SIGN_THUMBPRINT) { $mode = "store" }
    elseif ($env:CRFS_SIGN_PFX) { $mode = "pfx" }

    if (-not $mode) {
        Write-Host "No signing credentials configured (CRFS_SIGN_ARTIFACT_METADATA, CRFS_SIGN_THUMBPRINT or CRFS_SIGN_PFX)."
        Write-Host "Files stay unsigned. See docs\DISTRIBUTION.md for how to enroll."
        exit 2
    }

    $toSign = @($Files)
    if ($SkipSigned) {
        $toSign = @($Files | Where-Object { -not (Test-ValidSignature $_) })
        Write-Host ("{0} of {1} files need a signature (the rest already carry a valid one)." -f $toSign.Count, $Files.Count)
    }

    if ($toSign.Count -gt 0) {
        $signtool = Find-SignTool
        if (-not $signtool) { Write-Error "signtool.exe not found and could not be downloaded." }
        Write-Host "signtool: $signtool"

        $timestamp = if ($env:CRFS_TIMESTAMP_URL) { $env:CRFS_TIMESTAMP_URL }
                     elseif ($mode -eq "artifact") { "http://timestamp.acs.microsoft.com" }
                     else { "http://timestamp.digicert.com" }
        $common = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $timestamp, "/d", $Description)

        switch ($mode) {
            "artifact" {
                $metadata = $env:CRFS_SIGN_ARTIFACT_METADATA
                if (-not (Test-Path -LiteralPath $metadata)) { Write-Error "Artifact Signing metadata not found: $metadata" }
                $meta = Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json
                foreach ($key in @("Endpoint", "CodeSigningAccountName", "CertificateProfileName")) {
                    if (-not $meta.$key) { Write-Error "Artifact Signing metadata is missing '$key'." }
                }
                # The plug-in's JSON reader rejects a UTF-8 byte-order mark (Windows
                # PowerShell's Set-Content -Encoding UTF8 writes one). Feed it a clean copy.
                $bytes = [System.IO.File]::ReadAllBytes($metadata)
                if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
                    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
                    $clean = Join-Path $toolsDir "metadata-nobom.json"
                    [System.IO.File]::WriteAllBytes($clean, $bytes[3..($bytes.Length - 1)])
                    $metadata = $clean
                }
                $dlib = Find-ArtifactSigningDlib
                if (-not $dlib) { Write-Error "Azure.CodeSigning.Dlib.dll not found and could not be downloaded." }
                $runtimes = & dotnet --list-runtimes 2>$null
                if (-not ($runtimes | Select-String "Microsoft.NETCore.App 8\.")) {
                    Write-Warning "The Artifact Signing plug-in needs the .NET 8 runtime (dotnet --list-runtimes shows none)."
                }
                $common += @("/dlib", $dlib, "/dmdf", $metadata)
                Write-Host ("Signing with Azure Artifact Signing: account {0}, profile {1}" -f $meta.CodeSigningAccountName, $meta.CertificateProfileName)
            }
            "store" {
                $thumb = ($env:CRFS_SIGN_THUMBPRINT -replace "\s", "")
                $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
                    Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1
                if (-not $cert) { Write-Error "No certificate with thumbprint $thumb in CurrentUser\My or LocalMachine\My." }
                if ($cert.Issuer -eq $cert.Subject -and -not $AllowUntrustedTestCert) {
                    Write-Error "That certificate is self-signed. SmartScreen and Smart App Control treat it like no signature. Use a certificate from a trusted CA."
                }
                $common += @("/sha1", $thumb)
                Write-Host "Signing with store certificate: $($cert.Subject)"
            }
            "pfx" {
                if (-not (Test-Path -LiteralPath $env:CRFS_SIGN_PFX)) { Write-Error "PFX not found: $env:CRFS_SIGN_PFX" }
                if (-not $env:CRFS_SIGN_PFX_PASSWORD) { Write-Error "CRFS_SIGN_PFX_PASSWORD is not set." }
                $common += @("/f", $env:CRFS_SIGN_PFX, "/p", $env:CRFS_SIGN_PFX_PASSWORD)
                Write-Host "Signing with PFX: $env:CRFS_SIGN_PFX"
            }
        }

        # signtool accepts many files per call; keep the command line comfortably short.
        $chunk = 40
        for ($i = 0; $i -lt $toSign.Count; $i += $chunk) {
            $batch = @($toSign[$i..([Math]::Min($i + $chunk, $toSign.Count) - 1)])
            Write-Host ("Signing {0} file(s) ..." -f $batch.Count)
            & $signtool @common @batch | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Error "signtool sign failed ($LASTEXITCODE)." }
        }
    }
}

# ---------------------------------------------------------------------------
# Verify (signtool verify when available, PowerShell otherwise)
# ---------------------------------------------------------------------------
$rows = @(Get-SignatureRows -Paths $Files)
$bad = @($rows | Where-Object { -not (Test-ValidSignature $_.File) })
$rows | Format-Table -AutoSize -Property Status, Signer, @{ n = "File"; e = { Split-Path $_.File -Leaf } } | Out-Host
if ($bad.Count -gt 0) {
    Write-Host ("{0} file(s) are NOT validly signed." -f $bad.Count)
    exit 1
}
if ($AllowUntrustedTestCert) {
    Write-Host ("All {0} file(s) are signed with the UNTRUSTED test certificate (pipeline self-test only)." -f $rows.Count)
    exit 0
}
Write-Host ("All {0} file(s) carry a valid Authenticode signature." -f $rows.Count)
exit 0
