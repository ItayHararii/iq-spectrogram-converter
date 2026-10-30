r"""Report the Authenticode status of every executable file in a build.

Smart App Control and App Control (WDAC) evaluate each DLL/PYD the app loads,
not only the outer EXE, so a release is only "signed" when the bundle is.

Usage (from crfs_iq_recorder\, inside the build venv):
    python packaging\verify_bundle_signatures.py dist\CRFS_IQ_Recorder.exe
    python packaging\verify_bundle_signatures.py dist\onedir\CRFS_IQ_Recorder
    python packaging\verify_bundle_signatures.py <path> --report-only   (never fails)

Exit codes: 0 everything signed, 1 something unsigned, 2 usage/tool error.
One-file EXEs are unpacked to a temporary folder with PyInstaller's own reader.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

PE_SUFFIXES = (".exe", ".dll", ".pyd")


def _pe_files_in_dir(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in PE_SUFFIXES)


def _extract_onefile(exe: Path, out: Path) -> list[Path]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError:
        print("PyInstaller is not installed in this interpreter; cannot open a one-file EXE.", file=sys.stderr)
        raise SystemExit(2)
    reader = CArchiveReader(str(exe))
    written: list[Path] = []
    for name, entry in reader.toc.items():
        typecode = entry[4]
        if typecode not in ("b", "x", "z", "m", "s", "o"):
            continue
        if not name.lower().endswith(PE_SUFFIXES):
            continue
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(reader.extract(name))
        written.append(target)
    return written


def _signature_rows(paths: list[Path]) -> list[dict]:
    if not paths:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(str(p) for p in paths))
        listfile = fh.name
    script = (
        "$files = Get-Content -LiteralPath '%s'; "
        "$rows = foreach ($f in $files) { $s = Get-AuthenticodeSignature -LiteralPath $f; "
        "[pscustomobject]@{ file = [string]$f; status = [string]$s.Status; "
        "signer = [string]$(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }) } }; "
        "ConvertTo-Json -InputObject @($rows) -Depth 3"
    ) % listfile.replace("'", "''")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    Path(listfile).unlink(missing_ok=True)
    if result.returncode != 0 or not result.stdout.strip():
        print(result.stderr.strip() or "Get-AuthenticodeSignature failed.", file=sys.stderr)
        raise SystemExit(2)
    rows = json.loads(result.stdout)
    return rows if isinstance(rows, list) else [rows]


def _cn(subject: str) -> str:
    for part in subject.split(","):
        part = part.strip()
        if part.startswith("CN="):
            return part[3:]
    return subject or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", help="one-file EXE, onedir folder, or installer EXE")
    parser.add_argument("--report-only", action="store_true", help="print the report but always exit 0")
    args = parser.parse_args()
    target = Path(args.target)
    if not target.exists():
        print(f"Not found: {target}", file=sys.stderr)
        return 2

    files: list[Path]
    base: Path
    tmpdir: tempfile.TemporaryDirectory | None = None
    if target.is_dir():
        base = target
        files = _pe_files_in_dir(target)
    else:
        tmpdir = tempfile.TemporaryDirectory(prefix="crfs-bundle-")
        base = Path(tmpdir.name)
        try:
            inner = _extract_onefile(target, base)
        except Exception:  # not a PyInstaller archive (for example the installer)
            inner = []
        files = [target, *inner]

    rows = _signature_rows(files)
    if tmpdir is not None:
        tmpdir.cleanup()

    by_key = Counter()
    unsigned = []
    for row in rows:
        path = Path(row["file"])
        try:
            label = str(path.relative_to(base))
        except ValueError:
            label = path.name
        key = f"{row['status']}: {_cn(row['signer'])}"
        by_key[key] += 1
        if row["status"] != "Valid":
            unsigned.append((label, row["status"]))

    print(f"Target: {target}")
    print(f"Executable files checked: {len(rows)}")
    for key, count in by_key.most_common():
        print(f"  {count:4d}  {key}")
    if unsigned:
        print(f"\nNOT validly signed ({len(unsigned)}):")
        for label, status in unsigned:
            print(f"  {status:12s} {label}")
        print(
            "\nSmart App Control and WDAC publisher rules will reject these files. "
            "Configure signing (sign_exe.ps1) and rebuild."
        )
        return 0 if args.report_only else 1
    print("\nAll executable files carry a valid Authenticode signature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
