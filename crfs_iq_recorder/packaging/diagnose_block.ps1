<#
.SYNOPSIS
  Find out which Windows mechanism warns about or blocks CRFS_IQ_Recorder.exe on this PC.

.DESCRIPTION
  "Windows protected your PC", "cannot run", or a silent failure can come from
  different places that need different fixes:

    SmartScreen          unsigned/unknown file downloaded from the internet -> warning,
                         "Run anyway" works unless policy says "warn and prevent bypass"
    Smart App Control    Windows 11 (home / unmanaged PCs): blocks every unsigned EXE/DLL,
                         no per-app exception -> only a trusted signature helps
    App Control (WDAC)   company policy: allows only approved publishers/paths -> IT must
                         allow the publisher or the installer path
    AppLocker            company policy: often forbids running from %TEMP% -> use the
                         installer (Program Files) and ask IT for a rule
    Defender / ASR       detection or the "block executables unless prevalence/age/
                         trusted list" rule -> false-positive submission / IT exclusion
    Crash                the app started but died -> Application log event 1000

  This script needs no admin rights. It reads settings, signatures and event logs,
  prints a summary, and writes a redacted report you can send to IT or to the
  Sensorz app owner. It changes nothing.

.PARAMETER Path
  The EXE to check. Default: the newest CRFS_IQ_Recorder*.exe in Downloads or Desktop.
.PARAMETER Report
  Where to write the report. Default: Desktop\CRFS_IQ_Recorder_diagnostics.txt
.PARAMETER Days
  How many days of event logs to scan (default 30).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File diagnose_block.ps1
  powershell -ExecutionPolicy Bypass -File diagnose_block.ps1 -Path "$env:USERPROFILE\Downloads\CRFS_IQ_Recorder.exe"
#>
[CmdletBinding()]
param(
    [string]$Path = "",
    [string]$Report = "",
    [int]$Days = 30
)

$ErrorActionPreference = "Continue"
$lines = New-Object System.Collections.Generic.List[string]
$since = (Get-Date).AddDays(-$Days)

function Out-Line([string]$text = "") {
    $lines.Add($text)
    Write-Host $text
}
function Out-Section([string]$title) {
    Out-Line ""
    Out-Line ("=" * 78)
    Out-Line $title
    Out-Line ("=" * 78)
}
function Redact([string]$text) {
    if (-not $text) { return $text }
    $text = [regex]::Replace($text, "(?i)([A-Z]:\\Users\\)[^\\\s]+", '$1<user>')
    $text = [regex]::Replace($text, "(?i)(\\Device\\HarddiskVolume\d+\\Users\\)[^\\\s]+", '$1<user>')
    # IPv4 addresses, but not 4-part file versions ("version: 1.8.6.0")
    $text = [regex]::Replace($text, "(?<!version: )\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<ip>")
    return $text
}
function Try-Get([scriptblock]$block, $default = $null) {
    try { return (& $block) } catch { return $default }
}

# ---------------------------------------------------------------------------
# Locate the file
# ---------------------------------------------------------------------------
if (-not $Path) {
    $candidates = @()
    foreach ($folder in @("$env:USERPROFILE\Downloads", "$env:USERPROFILE\Desktop", (Get-Location).Path)) {
        $candidates += Get-ChildItem -Path $folder -Filter "CRFS_IQ_Recorder*.exe" -ErrorAction SilentlyContinue
    }
    $pick = $candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($pick) { $Path = $pick.FullName }
}
if (-not $Report) {
    $Report = Join-Path ([Environment]::GetFolderPath("Desktop")) "CRFS_IQ_Recorder_diagnostics.txt"
}

Out-Line ("CRFS IQ Recorder - Windows block diagnostics  {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm"))
Out-Line "Read-only: this script changes no settings."

# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
Out-Section "1. System"
$os = Try-Get { Get-CimInstance Win32_OperatingSystem }
$cv = Try-Get { Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" }
Out-Line ("Windows: {0} build {1}.{2}" -f $os.Caption, $os.BuildNumber, $cv.UBR)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Out-Line ("Running as administrator: {0}" -f $isAdmin)
$dsreg = Try-Get { & dsregcmd /status 2>$null }
$joined = @()
foreach ($key in @("AzureAdJoined", "DomainJoined", "EnterpriseJoined", "WorkplaceJoined")) {
    $m = $dsreg | Select-String ("^\s*{0}\s*:\s*(\w+)" -f $key) | Select-Object -First 1
    if ($m) { $joined += ("{0}={1}" -f $key, $m.Matches[0].Groups[1].Value) }
}
$mdm = $dsreg | Select-String "^\s*MdmUrl\s*:\s*(\S+)" | Select-Object -First 1
Out-Line ("Management: {0}{1}" -f ($joined -join ", "), $(if ($mdm) { "; MDM enrolled" } else { "" }))
# Workplace-joined alone means "registered for Office sign-in", not managed.
$managed = ($joined -match "^(AzureAdJoined|DomainJoined|EnterpriseJoined)=YES") -or $mdm
Out-Line ("Company-managed PC: {0}" -f $(if ($managed) { "YES - company policies (App Control / AppLocker / Intune) can apply" } else { "no sign of management (Smart App Control and SmartScreen defaults apply)" }))

# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
Out-Section "2. The file"
$fileName = "CRFS_IQ_Recorder"
if ($Path -and (Test-Path -LiteralPath $Path)) {
    $item = Get-Item -LiteralPath $Path
    $fileName = $item.BaseName
    Out-Line ("Path: {0}" -f (Redact $item.FullName))
    Out-Line ("Size: {0:N0} bytes   modified {1}" -f $item.Length, $item.LastWriteTime)
    $hash = Try-Get { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    Out-Line ("SHA-256: {0}" -f $hash)
    $vi = $item.VersionInfo
    Out-Line ("Version resource: product '{0}' version '{1}' company '{2}'" -f $vi.ProductName, $vi.ProductVersion, $vi.CompanyName)
    $sig = Try-Get { Get-AuthenticodeSignature -LiteralPath $Path }
    if ($sig) {
        Out-Line ("Authenticode: {0}" -f $sig.Status)
        if ($sig.SignerCertificate) {
            Out-Line ("  signer: {0}" -f $sig.SignerCertificate.Subject)
            Out-Line ("  issuer: {0}" -f $sig.SignerCertificate.Issuer)
            Out-Line ("  valid:  {0} -> {1}" -f $sig.SignerCertificate.NotBefore, $sig.SignerCertificate.NotAfter)
        }
        if ($sig.TimeStamperCertificate) { Out-Line ("  timestamp by: {0}" -f $sig.TimeStamperCertificate.Subject) }
        if ($sig.Status -eq "NotSigned") {
            Out-Line "  -> UNSIGNED. SmartScreen shows 'Unknown publisher'; Smart App Control and WDAC publisher rules block it."
        }
    }
    $zone = Try-Get { Get-Content -LiteralPath $Path -Stream Zone.Identifier -ErrorAction Stop }
    if ($zone) {
        $zoneId = ($zone | Select-String "^ZoneId=(\d)" | Select-Object -First 1)
        $host = ($zone | Select-String "^(HostUrl|ReferrerUrl)=(\S+)" | Select-Object -First 1)
        $hostText = ""
        if ($host) { $hostText = Try-Get { ([uri]$host.Matches[0].Groups[2].Value).Host } "" }
        Out-Line ("Mark of the Web: yes (ZoneId {0}{1}) - downloaded from the internet, SmartScreen checks apply" -f $(if ($zoneId) { $zoneId.Matches[0].Groups[1].Value } else { "?" }), $(if ($hostText) { ", from $hostText" } else { "" }))
    } else {
        Out-Line "Mark of the Web: none (copied locally or already unblocked) - SmartScreen download check does not apply"
    }
    $inTemp = $item.FullName -like "$env:TEMP*" -or $item.FullName -like "$env:LOCALAPPDATA\Temp*"
    if ($inTemp) { Out-Line "Location is a temp folder: AppLocker/WDAC path rules often block execution here." }
} elseif ($Path) {
    Out-Line ("File not found: {0}" -f (Redact $Path))
} else {
    Out-Line "No CRFS_IQ_Recorder*.exe found in Downloads/Desktop - pass -Path to check a specific file."
}

# ---------------------------------------------------------------------------
# Smart App Control
# ---------------------------------------------------------------------------
Out-Section "3. Smart App Control (Windows 11)"
$sacState = Try-Get { (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop).VerifiedAndReputablePolicyState }
$sacText = switch ($sacState) { 0 { "OFF" } 1 { "ON (enforcing)" } 2 { "EVALUATION (not blocking yet)" } default { "not present (Windows 10 or unsupported)" } }
Out-Line ("State: {0}" -f $sacText)
if ($sacState -eq 1) {
    Out-Line "  Enforcing: every EXE/DLL/PYD must be signed by a CA in the Microsoft Trusted Root Program or be known-good to Microsoft."
    Out-Line "  There is no per-app exception. Unsigned CRFS IQ Recorder builds are blocked here by design."
}

# ---------------------------------------------------------------------------
# App Control for Business (WDAC)
# ---------------------------------------------------------------------------
Out-Section "4. App Control for Business (WDAC) policies"
$dg = Try-Get { Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction Stop }
if ($dg) {
    $umci = switch ($dg.UsermodeCodeIntegrityPolicyEnforcementStatus) { 0 { "off" } 1 { "audit" } 2 { "ENFORCED" } default { "unknown" } }
    $kmci = switch ($dg.CodeIntegrityPolicyEnforcementStatus) { 0 { "off" } 1 { "audit" } 2 { "ENFORCED" } default { "unknown" } }
    Out-Line ("User-mode code integrity policy: {0}   kernel-mode: {1}" -f $umci, $kmci)
}
$SAC_POLICY_ID = "0283ac0f-fff1-49ae-ada1-8a933130cad6"   # Smart App Control ("VerifiedAndReputableDesktop")
$cip = @(Get-ChildItem "$env:SystemRoot\System32\CodeIntegrity\CiPolicies\Active" -Filter *.cip -ErrorAction SilentlyContinue)
Out-Line ("Active policy files: {0}" -f $cip.Count)
foreach ($f in $cip) {
    $id = $f.BaseName.Trim("{}").ToLowerInvariant()
    $label = if ($id -eq $SAC_POLICY_ID) { "Smart App Control" } else { "Windows inbox or company policy" }
    Out-Line ("  {0}  {1,8:N0} bytes  {2}" -f $f.BaseName, $f.Length, $label)
}
$otherPolicies = @($cip | Where-Object { $_.BaseName.Trim("{}").ToLowerInvariant() -ne $SAC_POLICY_ID })
if ($isAdmin) {
    # citool needs admin; feed it a newline so it never waits for "Press Enter".
    $policies = Try-Get { & cmd.exe /c "echo.| citool.exe --list-policies" 2>$null }
    if ($policies) {
        $names = $policies | Select-String "Friendly Name\s*:\s*(.+)$" | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() }
        if ($names) { Out-Line ("Policy names (citool): {0}" -f (($names | Select-Object -Unique) -join "; ")) }
    }
} else {
    Out-Line "  (IT can name the policies with 'citool --list-policies' from an elevated prompt.)"
}

# ---------------------------------------------------------------------------
# AppLocker
# ---------------------------------------------------------------------------
Out-Section "5. AppLocker"
$alXml = Try-Get { [xml](Get-AppLockerPolicy -Effective -Xml -ErrorAction Stop) }
if ($alXml -and $alXml.AppLockerPolicy.RuleCollection) {
    foreach ($rc in $alXml.AppLockerPolicy.RuleCollection) {
        $count = @($rc.ChildNodes | Where-Object { $_.NodeType -eq "Element" }).Count
        Out-Line ("  {0,-8} mode={1,-12} rules={2}" -f $rc.Type, $rc.EnforcementMode, $count)
    }
    $svc = Try-Get { (Get-Service AppIDSvc -ErrorAction Stop).Status }
    Out-Line ("  Application Identity service: {0}" -f $svc)
} else {
    Out-Line "No AppLocker rules in effect."
}

# ---------------------------------------------------------------------------
# SmartScreen
# ---------------------------------------------------------------------------
Out-Section "6. Microsoft Defender SmartScreen"
$ssUser = Try-Get { (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer" -ErrorAction Stop).SmartScreenEnabled }
Out-Line ("Explorer setting: {0}" -f $(if ($ssUser) { $ssUser } else { "(default: Warn)" }))
$pol = Try-Get { Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" -ErrorAction Stop }
if ($pol -and $null -ne $pol.EnableSmartScreen) {
    Out-Line ("Group policy: EnableSmartScreen={0} ShellSmartScreenLevel={1}" -f $pol.EnableSmartScreen, $pol.ShellSmartScreenLevel)
    if ($pol.ShellSmartScreenLevel -eq "Block") {
        Out-Line "  -> 'Warn and prevent bypass': users cannot click 'Run anyway'. Only reputation/signing or IT can help."
    }
} else {
    Out-Line "Group policy: none (user can choose 'More info' -> 'Run anyway')"
}
Out-Line "Note: SmartScreen warns about unsigned or not-yet-reputable downloads even when nothing else blocks them."

# ---------------------------------------------------------------------------
# Defender / ASR / other AV
# ---------------------------------------------------------------------------
Out-Section "7. Antivirus and Attack Surface Reduction"
$mp = Try-Get { Get-MpComputerStatus -ErrorAction Stop }
if ($mp) {
    Out-Line ("Defender: mode={0} realtime={1} tamper-protected={2} engine={3}" -f $mp.AMRunningMode, $mp.RealTimeProtectionEnabled, $mp.IsTamperProtected, $mp.AMEngineVersion)
}
$pref = Try-Get { Get-MpPreference -ErrorAction Stop }
if ($pref -and $pref.AttackSurfaceReductionRules_Ids) {
    $ids = @($pref.AttackSurfaceReductionRules_Ids)
    $acts = @($pref.AttackSurfaceReductionRules_Actions)
    $blockRules = @()
    for ($i = 0; $i -lt $ids.Count; $i++) {
        $action = switch ($acts[$i]) { 1 { "BLOCK" } 2 { "audit" } 6 { "warn" } default { "off" } }
        if ($acts[$i] -in 1, 6) { $blockRules += ("{0} ({1})" -f $ids[$i], $action) }
    }
    Out-Line ("ASR rules in block/warn mode: {0}" -f $(if ($blockRules) { $blockRules -join "; " } else { "none" }))
    if ($ids -contains "01443614-cd74-433a-b99e-2ecdc07bfc25" -and $acts[[array]::IndexOf($ids, "01443614-cd74-433a-b99e-2ecdc07bfc25")] -eq 1) {
        Out-Line "  -> 'Block executable files from running unless they meet a prevalence, age, or trusted list criterion' is ON."
        Out-Line "     New, rarely seen EXEs are blocked by this rule regardless of signature. IT exclusion or Intune allow needed."
    }
} else {
    Out-Line "ASR rules: none configured"
}
# Controlled folder access ("Ransomware protection"): only apps Microsoft already
# trusts may change files in Documents, Desktop, Pictures, Videos, Music, Favorites.
# It does not stop the app from starting, but it blocks an unknown installer or
# uninstaller inside those folders, and the app itself when it saves recordings there.
$cfa = if ($pref) { $pref.EnableControlledFolderAccess } else { $null }
$cfaText = switch ($cfa) { 0 { "off" } 1 { "ON (block)" } 2 { "audit only" } 3 { "block disk-sector writes only" } 4 { "audit disk-sector writes only" } default { "unknown" } }
Out-Line ("Controlled folder access (ransomware protection): {0}" -f $cfaText)
if ($cfa -eq 1) {
    Out-Line "  Unknown programs cannot create, change or delete files in Documents, Desktop, Pictures, Videos, Music or Favorites."
    Out-Line "  Install to Program Files or %LOCALAPPDATA%\Programs, and keep the download folder outside those folders."
}
$cfaEvents = Try-Get { @(Get-WinEvent -FilterHashtable @{LogName = "Microsoft-Windows-Windows Defender/Operational"; Id = 1123, 1124; StartTime = $since } -ErrorAction Stop -MaxEvents 400 |
        Where-Object { $_.Message -match ("(?i)" + [regex]::Escape($fileName) + "|CRFS_IQ_Recorder|CRFS IQ Recorder|_unins\.tmp|is-[A-Z0-9]+") }) } @()
Out-Line ("Controlled folder access blocks (1123) / audits (1124) involving the app or its installer: {0}" -f $cfaEvents.Count)
foreach ($e in ($cfaEvents | Select-Object -First 6)) {
    $x = [xml]$e.ToXml(); $d = @{}
    foreach ($row in $x.Event.EventData.Data) { $d[$row.Name] = $row.'#text' }
    Out-Line ("  {0}  id={1}  process: {2}" -f $e.TimeCreated.ToString("yyyy-MM-dd HH:mm"), $e.Id, (Redact $d["Process Name"]))
    Out-Line ("        target: {0}" -f (Redact $d["Path"]))
}
$av = Try-Get { Get-CimInstance -Namespace root\SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop }
if ($av) { Out-Line ("Registered AV products: {0}" -f (($av | ForEach-Object { $_.displayName }) -join "; ")) }
$threats = Try-Get { Get-MpThreatDetection -ErrorAction Stop }
$hits = @($threats | Where-Object { ($_.Resources -join " ") -match [regex]::Escape($fileName) })
Out-Line ("Defender detections naming {0}: {1}" -f $fileName, $hits.Count)
foreach ($h in $hits) { Out-Line ("  {0}  threat id {1}  {2}" -f $h.InitialDetectionTime, $h.ThreatID, (Redact ($h.Resources -join " "))) }

# ---------------------------------------------------------------------------
# Event logs
# ---------------------------------------------------------------------------
Out-Section ("8. Event log evidence (last {0} days)" -f $Days)
function Show-Events([string]$label, [hashtable]$filter, [string]$match, [int]$max = 12) {
    $ev = Try-Get { Get-WinEvent -FilterHashtable $filter -ErrorAction Stop -MaxEvents 400 } @()
    $ev = @($ev | Where-Object { -not $match -or $_.Message -match $match })
    Out-Line ("{0}: {1} event(s)" -f $label, $ev.Count)
    foreach ($e in ($ev | Select-Object -First $max)) {
        $msg = [string]$e.Message
        $detail = $null
        foreach ($rx in @(
                "(?i)attempted to load (.+?) that did not meet",   # CodeIntegrity 3033/3076/3077: the blocked file
                "(?i)^(.+?\.(exe|dll|pyd)) was (prevented|allowed)",   # AppLocker 8003/8004
                "(?i)Faulting application path:\s*(.+)$",            # Application 1000
                "(?i)Path:\s*(.+\.(exe|dll|pyd))"                     # Defender
            )) {
            $m = [regex]::Match($msg, $rx, "Multiline")
            if ($m.Success) { $detail = $m.Groups[1].Value.Trim(); break }
        }
        $first = (($msg -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -First 1)
        if ($first.Length -gt 160) { $first = $first.Substring(0, 160) + "..." }
        Out-Line ("  {0}  id={1}  {2}" -f $e.TimeCreated.ToString("yyyy-MM-dd HH:mm"), $e.Id, (Redact $first))
        if ($detail) { Out-Line ("        file: {0}" -f (Redact $detail)) }
    }
}
$pattern = [regex]::Escape($fileName) + "|_MEI\d+|CRFS_IQ_Recorder_Setup|is-[A-Z0-9]+\.tmp"
Show-Events "Smart App Control / WDAC blocks (CodeIntegrity 3077, 3033)" @{LogName = "Microsoft-Windows-CodeIntegrity/Operational"; Id = 3077, 3033; StartTime = $since } $pattern

# Event 3118 is written only by Smart App Control and records the decision inputs:
# whether Defender's cloud (Intelligent Security Graph) was asked about the file,
# whether it named a threat, and the file hash. No threat + block = the file was
# simply unknown to Microsoft and unsigned.
$sac = Try-Get { @(Get-WinEvent -FilterHashtable @{LogName = "Microsoft-Windows-CodeIntegrity/Operational"; Id = 3118; StartTime = $since } -ErrorAction Stop -MaxEvents 400) } @()
$sac = @($sac | Where-Object {
        $x = [xml]$_.ToXml(); $name = ($x.Event.EventData.Data | Where-Object { $_.Name -eq "FileNameBuffer" }).'#text'
        $name -match $pattern })
Out-Line ("Smart App Control block details (CodeIntegrity 3118): {0} event(s)" -f $sac.Count)
foreach ($e in ($sac | Select-Object -First 6)) {
    $d = @{}
    foreach ($row in ([xml]$e.ToXml()).Event.EventData.Data) { $d[$row.Name] = $row.'#text' }
    Out-Line ("  {0}  file: {1}" -f $e.TimeCreated.ToString("yyyy-MM-dd HH:mm"), (Redact $d["FileNameBuffer"]))
    $threat = if ($d["DefenderThreatName"]) { $d["DefenderThreatName"] } else { "none" }
    Out-Line ("        SHA-256 {0}" -f $d["SHA256FlatHash"])
    Out-Line ("        Defender cloud reputation asked: {0}   threat named: {1}   -> unknown to Microsoft and not signed by a trusted CA" -f $d["DefenderMadeCloudCall"], $threat)
}
Show-Events "WDAC audit-only events (CodeIntegrity 3076)" @{LogName = "Microsoft-Windows-CodeIntegrity/Operational"; Id = 3076; StartTime = $since } $pattern 5
Show-Events "AppLocker blocks (8004) and audits (8003), EXE and DLL" @{LogName = "Microsoft-Windows-AppLocker/EXE and DLL"; Id = 8003, 8004; StartTime = $since } $pattern
Show-Events "Defender detections (1116/1117) and ASR (1121/1122)" @{LogName = "Microsoft-Windows-Windows Defender/Operational"; Id = 1116, 1117, 1121, 1122; StartTime = $since } $pattern
Show-Events "Application crashes (event 1000) of the app" @{LogName = "Application"; Id = 1000; StartTime = $since } ("(?i)Faulting application name:\s*" + [regex]::Escape($fileName) + "\.exe") 6

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
Out-Section "9. Summary"
$verdict = New-Object System.Collections.Generic.List[string]
if ($sig -and $sig.Status -eq "NotSigned") {
    $verdict.Add("The file is unsigned. Expect the SmartScreen 'Unknown publisher' warning on first run after download.")
} elseif ($sig -and $sig.Status -ne "Valid" -and $sig.SignerCertificate) {
    $verdict.Add(("The file is signed but the signature does not validate here ({0}): the certificate chain is not trusted on this PC. Windows treats this like an unsigned file." -f $sig.Status))
} elseif ($sig -and $sig.Status -eq "Valid") {
    $verdict.Add(("The file carries a valid signature from '{0}'. Smart App Control accepts it; SmartScreen may still warn until the publisher has download history." -f $sig.SignerCertificate.Subject))
}
if ($sacState -eq 1) {
    $verdict.Add("Smart App Control is ON: unsigned builds cannot run on this PC at all. Fix = a release signed with a trusted certificate (Sensorz side). Turning SAC off is not the recommended fix.")
}
if ($dg -and $dg.UsermodeCodeIntegrityPolicyEnforcementStatus -eq 2 -and $sacState -ne 1) {
    $verdict.Add("A company App Control (WDAC) policy is enforced: IT must allow the publisher certificate (after signing) or this app's installer path.")
}
if ($alXml -and $alXml.AppLockerPolicy.RuleCollection) {
    $verdict.Add("AppLocker rules are active: the portable EXE unpacks into %TEMP%, which AppLocker usually forbids. Use the installer (Program Files) and ask IT for a publisher or path rule.")
}
if ($pol -and $pol.ShellSmartScreenLevel -eq "Block") {
    $verdict.Add("SmartScreen policy 'warn and prevent bypass' is set: only a signed, reputable file or an IT allow-list gets past it.")
}
if ($hits.Count -gt 0) {
    $verdict.Add("Defender reported a detection on this file: submit it at https://www.microsoft.com/wdsi/filesubmission as a false positive (IT) and share the detection name with the app owner.")
}
if ($cfaEvents.Count -gt 0) {
    $verdict.Add(("Controlled folder access blocked the app, its installer or its uninstaller {0} time(s): it was installed or saving into Documents/Desktop/Pictures. Delete a leftover install folder in File Explorer, reinstall to the default folder (Program Files or %LOCALAPPDATA%\Programs), and keep the recordings folder outside protected folders." -f $cfaEvents.Count))
} elseif ($cfa -eq 1) {
    $verdict.Add("Controlled folder access is ON: do not install into or download recordings to Documents, Desktop, Pictures, Videos or Music; an unknown app is blocked from writing there.")
}
$crashes = Try-Get { @(Get-WinEvent -FilterHashtable @{LogName = "Application"; Id = 1000; StartTime = $since } -ErrorAction Stop -MaxEvents 400 |
        Where-Object { $_.Message -match ("(?i)Faulting application name:\s*" + [regex]::Escape($fileName) + "\.exe") }).Count } 0
if ($crashes -gt 0) {
    $verdict.Add(("{0} crash report(s) for the app in the last {1} days. Versions before 1.8.6 crashed on every exit (exception 0xC0000409 in Qt6Core.dll); update to 1.8.6 or later." -f $crashes, $Days))
}
if ($verdict.Count -eq 0) {
    $verdict.Add("No blocking mechanism found in settings. Check the event evidence above; if the app opened and closed by itself, see the crash events.")
}
foreach ($v in $verdict) { Out-Line ("- " + $v) }

try {
    $lines | Set-Content -LiteralPath $Report -Encoding UTF8
    Out-Line ""
    Out-Line ("Report written to: {0}" -f $Report)
} catch {
    Out-Line ("Could not write the report file: {0}" -f $_.Exception.Message)
}
