# -*- mode: python ; coding: utf-8 -*-
r"""One-file Windows build for CRFS IQ Recorder. Double-click dist/CRFS_IQ_Recorder.exe.

Startup of a one-file EXE is dominated by unpacking the bundle into %TEMP% on
every launch, so this spec bundles only what the app imports:

- PySide6 QtCore / QtGui / QtWidgets and the plugins they need (no WebEngine,
  QML/Quick, 3D, Multimedia, Designer, or software OpenGL).
- matplotlib with the Agg backend only (used for the short waterfall preview).
- numpy, paramiko + cryptography (SFTP), requests, openpyxl, pywin32.

It also embeds a Windows version resource (product name, company, version) so
Explorer, Task Manager, and SmartScreen show a real name instead of blanks.

Signing: when signing credentials are configured (see sign_exe.ps1), every DLL,
PYD and EXE that goes into the bundle is copied to build\signed_binaries and
signed there before packaging. Smart App Control and App Control (WDAC) check
each file the app loads, not just the outer EXE, so the ~40 unsigned
third-party extensions (numpy, cryptography, Pillow, matplotlib, pywin32, ...)
must carry a signature too. Files that already have a valid vendor signature
(Python, Qt, Microsoft) are left as they are. The outer EXE is signed by
build_exe.bat after PyInstaller finishes. No certificate lives in this repo.

Layout: one-file by default (dist\CRFS_IQ_Recorder.exe, portable). Set
CRFS_BUILD_ONEDIR=1 for a folder build (dist\...\CRFS_IQ_Recorder\) that the
Inno Setup installer in packaging\ installs to Program Files - that layout does
not unpack into %TEMP% at every launch, which AppLocker and WDAC path rules on
managed PCs commonly forbid.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

block_cipher = None
root = Path(SPECPATH)

APP_NAME = "CRFS IQ Recorder"
COMPANY = "Sensorz"
EXE_NAME = "CRFS_IQ_Recorder"
ONEDIR = os.environ.get("CRFS_BUILD_ONEDIR", "").strip().lower() in {"1", "true", "yes"}
SIGNING_ENV = ("CRFS_SIGN_ARTIFACT_METADATA", "CRFS_SIGN_THUMBPRINT", "CRFS_SIGN_PFX")
PE_SUFFIXES = (".dll", ".pyd", ".exe")


def signing_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in SIGNING_ENV)


def sign_bundled_binaries(binaries):
    """Sign copies of every PE file in the TOC; return the TOC pointing at the copies."""
    if not signing_configured():
        return binaries
    staging = root / "build" / "signed_binaries"
    shutil.rmtree(staging, ignore_errors=True)
    rewritten = []
    copies = []
    for dest, src, typecode in binaries:
        if typecode in ("BINARY", "EXTENSION") and dest.lower().endswith(PE_SUFFIXES):
            target = staging / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copies.append(target)
            rewritten.append((dest, str(target), typecode))
        else:
            rewritten.append((dest, src, typecode))
    listfile = staging / "files.txt"
    listfile.write_text("\n".join(str(path) for path in copies), encoding="utf-8")
    print(f"Signing {len(copies)} bundled binaries that lack a valid signature ...")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "sign_exe.ps1"),
            "-FileList",
            str(listfile),
            "-SkipSigned",
            "-Description",
            APP_NAME,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Signing bundled binaries failed (exit {result.returncode}).")
    return rewritten


def read_version() -> str:
    text = (root / "crfs_iq_recorder" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


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

# Icon: the shared Sensorz mark (16-256 px). Bundled for the window/taskbar at
# runtime and embedded into the EXE for Explorer, shortcuts, and pins.
icon = root / "assets" / "sensorz_icon.ico"
if not icon.is_file():
    icon = root.parent / "assets" / "sensorz_icon.ico"

datas = []
for name in ("sensorz_icon.ico", "sensorz_icon.png"):
    bundled = root / "assets" / name
    if not bundled.is_file():
        bundled = root.parent / "assets" / name
    if bundled.is_file():
        datas.append((str(bundled), "assets"))
try:
    datas += collect_data_files("certifi")
except Exception:
    pass

hiddenimports = collect_submodules("crfs_iq_recorder")
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "requests",
    "paramiko",
    "numpy",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "matplotlib.pyplot",
    "openpyxl",
    "et_xmlfile",
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "win32crypt",
    "certifi",
    "charset_normalizer",
    "idna",
    "urllib3",
    "cryptography",
    "nacl",
]

# Qt modules PyInstaller may otherwise pull in through package scanning.
QT_EXCLUDES = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtAsyncio",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization",
    "PySide6.QtDBus",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtPrintSupport",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.QtXml",
]

excludes = [
    "pytest",
    "tkinter",
    "IPython",
    "jupyter",
    "scipy",
    "pandas",
    "PyQt5",
    "PyQt6",
    "PIL.ImageQt",
    "PIL.ImageTk",
    "PIL.AvifImagePlugin",
    "PIL._avif",
    "PIL.WebPImagePlugin",
    "PIL._webp",
    "matplotlib.tests",
    "numpy.tests",
    "numpy.f2py",
    "numpy.distutils",
    "paramiko.win_openssh",
] + QT_EXCLUDES

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[str(root / "pyi_rth_crfs.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)


# Qt runtime DLLs this app links: QtCore/QtGui/QtWidgets and the plugins kept
# below need nothing else. Any other Qt6*.dll arrives only as a dependency of a
# plugin we do not ship (qpdf -> Qt6Pdf/Qt6Network, virtual keyboard ->
# Qt6Quick/Qt6Qml/Qt6OpenGL, qsvg -> Qt6Svg).
QT_DLLS_KEEP = {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll"}

# Plugins a plain QtWidgets desktop app on Windows actually loads.
QT_PLUGINS_KEEP = {
    "plugins/platforms/qwindows.dll",
    "plugins/platforms/qminimal.dll",
    "plugins/platforms/qoffscreen.dll",
    "plugins/styles/qmodernwindowsstyle.dll",
    "plugins/imageformats/qico.dll",  # Sensorz .ico for window/taskbar
    "plugins/imageformats/qjpeg.dll",
}


def _keep(entry) -> bool:
    """Drop Qt pieces a QtWidgets desktop app never loads."""
    dest = str(entry[0]).replace("\\", "/")
    lower = dest.lower()
    # Software OpenGL fallback for QtQuick/OpenGL scenes (~20 MB). Widgets paint with the raster engine.
    if lower.endswith("opengl32sw.dll"):
        return False
    if lower.startswith("pyside6/") and "/plugins/" in lower:
        rel = lower.split("pyside6/", 1)[1]
        return rel in QT_PLUGINS_KEEP
    if lower.startswith("pyside6/") and lower.count("/") == 1 and lower.endswith(".dll"):
        name = lower.rsplit("/", 1)[1]
        if name.startswith("qt6"):
            return name in QT_DLLS_KEEP
    # Qt translations (app is English only) and QML.
    if "/translations/" in lower or lower.startswith("pyside6/translations"):
        return False
    if "/qml/" in lower or lower.startswith("pyside6/qml"):
        return False
    # Qt plugins we do not use (no QtNetwork, QtSql, QtMultimedia, etc.).
    unused_plugins = (
        "plugins/tls/",
        "plugins/networkinformation/",
        "plugins/sqldrivers/",
        "plugins/multimedia/",
        "plugins/qmltooling/",
        "plugins/qmllint/",
        "plugins/scxmldatamodel/",
        "plugins/sensors/",
        "plugins/position/",
        "plugins/geoservices/",
        "plugins/texttospeech/",
        "plugins/canbus/",
        "plugins/webview/",
        "plugins/renderers/",
        "plugins/renderplugins/",
        "plugins/sceneparsers/",
        "plugins/geometryloaders/",
        "plugins/assetimporters/",
        "plugins/designer/",
    )
    if any(part in lower for part in unused_plugins):
        return False
    # PySide6 helper executables and Designer tooling.
    if lower.startswith("pyside6/") and lower.endswith(".exe"):
        return False
    return True


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
a.binaries = sign_bundled_binaries(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=version_info,
)
if icon.is_file():
    exe_kwargs["icon"] = str(icon)

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **exe_kwargs,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=EXE_NAME,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **exe_kwargs,
    )
