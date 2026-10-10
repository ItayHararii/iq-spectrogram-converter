<#
.SYNOPSIS
  Authenticode-sign dist\CRFS_IQ_Recorder.exe with a trusted code-signing certificate.

.DESCRIPTION
  Signing identifies the publisher to Windows. It is what turns "Unknown publisher"
  into a named publisher in SmartScreen and Explorer. It does not by itself give a
  new file SmartScreen reputation; that still accumulates from downloads.

  The certificate is never stored in this repository. Provide one of:

    1. A certificate in the Windows certificate store (recommended on a build PC
       or with a hardware token):
         $env:CRFS_SIGN_THUMBPRINT = "<SHA1 thumbprint of the code-signing cert>"

    2. A PFX file plus its password, both outside the repo:
         $env:CRFS_SIGN_PFX = "C:\secure\company-codesign.pfx"
         $env:CRFS_SIGN_PFX_PASSWORD = "<password>"

  Optional:
         $env:CRFS_TIMESTAMP_URL = "http://timestamp.digicert.com"   (default)

  Requires signtool.exe from the Windows SDK ("Windows SDK Signing Tools for
  Desktop Apps" feature). Self-signed certificates are NOT accepted here: they
  behave like no signature in SmartScreen and only mislead users.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File sign_exe.ps1
  powershell -ExecutionPolicy Bypass -File sign_exe.ps1 -VerifyOnly
#>
[CmdletBinding()]
param(
    [string]$Exe = (Join-Path $PSScriptRoot "dist\CRFS_IQ_Recorder.exe"),
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
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
    return $null
}

if (-not (Test-Path $Exe)) {
    Write-Error "EXE not found: $Exe"
}

$signtool = Find-SignTool
if (-not $signtool) {
    Write-Error "signtool.exe not found. Install the Windows SDK 'Signing Tools for Desktop Apps' feature."
}

if (-not $VerifyOnly) {
    $timestamp = if ($env:CRFS_TIMESTAMP_URL) { $env:CRFS_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $args = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $timestamp, "/d", "CRFS IQ Recorder")

    if ($env:CRFS_SIGN_THUMBPRINT) {
        $thumb = ($env:CRFS_SIGN_THUMBPRINT -replace "\s", "")
        $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1
        if (-not $cert) { Write-Error "No certificate with thumbprint $thumb in CurrentUser\My or LocalMachine\My." }
        if ($cert.Issuer -eq $cert.Subject) {
            Write-Error "That certificate is self-signed. SmartScreen treats it like no signature. Use a certificate from a trusted CA."
        }
        $args += @("/sha1", $thumb)
        Write-Host "Signing with store certificate: $($cert.Subject)"
    }
    elseif ($env:CRFS_SIGN_PFX) {
        if (-not (Test-Path $env:CRFS_SIGN_PFX)) { Write-Error "PFX not found: $env:CRFS_SIGN_PFX" }
        if (-not $env:CRFS_SIGN_PFX_PASSWORD) { Write-Error "CRFS_SIGN_PFX_PASSWORD is not set." }
        $args += @("/f", $env:CRFS_SIGN_PFX, "/p", $env:CRFS_SIGN_PFX_PASSWORD)
        Write-Host "Signing with PFX: $env:CRFS_SIGN_PFX"
    }
    else {
        Write-Host "No signing credentials configured (CRFS_SIGN_THUMBPRINT or CRFS_SIGN_PFX). EXE stays unsigned."
        exit 2
    }

    $args += $Exe
    & $signtool @args
    if ($LASTEXITCODE -ne 0) { Write-Error "signtool sign failed ($LASTEXITCODE)." }
}

& $signtool verify /pa /v $Exe
if ($LASTEXITCODE -ne 0) { Write-Error "signtool verify failed ($LASTEXITCODE)." }

$sig = Get-AuthenticodeSignature $Exe
Write-Host ("Authenticode: {0}  signer: {1}" -f $sig.Status, $sig.SignerCertificate.Subject)
if ($sig.Status -ne "Valid") { exit 1 }
