# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build for CRFS IQ Spectrogram Converter.

Double-click dist/IQ_Spectrogram_Converter.exe. Startup of a one-file EXE is
dominated by unpacking the bundle into %TEMP% on every launch (and by the
antivirus scanning what was unpacked), so this spec bundles only what the app
imports:

- numpy, scipy (fft + signal windows), soundfile, Pillow, tkinter + tkinterdnd2
- matplotlib with the Agg backend only (the PNG renderer)

It also embeds a Windows version resource (product name, company, version) so
Explorer, Task Manager, and SmartScreen show a real name instead of blanks.
Authenticode signing is a separate step (see sign_exe.ps1); the certificate
never lives in this repository.

Build:  venv\\Scripts\\pyinstaller.exe --noconfirm --clean IQ_Spectrogram_Converter.spec
"""

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

root = Path(SPECPATH)

APP_NAME = "CRFS IQ Spectrogram Converter"
COMPANY = "Sensorz"
EXE_NAME = "IQ_Spectrogram_Converter"


def read_version() -> str:
    """Single source of truth: __version__ in iq_gui.py."""
    text = (root / "iq_gui.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def version_tuple(version: str) -> tuple:
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


VERSION = read_version()
VERSION_TUPLE = version_tuple(VERSION)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=VERSION_TUPLE,
        prodvers=VERSION_TUPLE,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", COMPANY),
                        StringStruct("FileDescription", APP_NAME),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", EXE_NAME),
                        StringStruct("LegalCopyright", f"Copyright (c) {COMPANY}"),
                        StringStruct("OriginalFilename", f"{EXE_NAME}.exe"),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# Icon: the shared Sensorz mark. Bundled for the window/taskbar at runtime and
# embedded into the EXE for Explorer, shortcuts, and pins.
icon = root / "assets" / "sensorz_icon.ico"

datas = []
for name in ("sensorz_icon.ico", "sensorz_icon.png", "sensorz_logo.png"):
    bundled = root / "assets" / name
    if bundled.is_file():
        datas.append((str(bundled), "assets"))

binaries = []
hiddenimports = [
    "iq_data",
    "iq_theme",
    "PIL",
    "PIL.ImageTk",
    "PIL.PngImagePlugin",
    "numpy",
    "scipy.fft",
    "scipy.signal.windows",
    "soundfile",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "matplotlib.pyplot",
]

# tkinterdnd2 ships the tkdnd Tcl package as plain files; collect them all.
_dnd = collect_all("tkinterdnd2")
datas += _dnd[0]
binaries += _dnd[1]
hiddenimports += _dnd[2]

excludes = [
    "seaborn",
    "pandas",
    "pytest",
    "IPython",
    "jupyter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "tkinter.test",
    "matplotlib.tests",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_webagg",
    "matplotlib.backends.backend_nbagg",
    "numpy.tests",
    # numpy.f2py must stay: scipy's array_api_compat shim touches every numpy
    # attribute at import time and fails if the submodule is missing.
    # scipy.signal imports most other scipy subpackages at import time, so only
    # the parts that are never imported can go.
    "scipy.io",
    "scipy.datasets",
    "scipy.cluster",
    "scipy.odr",
    "PIL.ImageQt",
    "PIL.AvifImagePlugin",
    "PIL._avif",
]

a = Analysis(
    [str(root / "iq_gui.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[str(root / "pyi_rth_iq.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe_kwargs = dict(
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX makes every launch decompress ~100 MB and trips antivirus heuristics
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)
if icon.is_file():
    exe_kwargs["icon"] = [str(icon)]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    **exe_kwargs,
)
