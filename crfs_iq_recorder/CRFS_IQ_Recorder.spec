# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build for CRFS IQ Recorder. Double-click dist/CRFS_IQ_Recorder.exe."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, collect_submodules

block_cipher = None
root = Path(SPECPATH)

datas = [(str(root / "docs"), "docs")]
binaries = []
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

icon = root / "assets" / "sensorz_icon.ico"
if not icon.is_file():
    icon = root.parent / "assets" / "sensorz_icon.ico"
for name in ("sensorz_icon.ico", "sensorz_icon.png"):
    bundled = root / "assets" / name
    if not bundled.is_file():
        bundled = root.parent / "assets" / name
    if bundled.is_file():
        datas.append((str(bundled), "assets"))

for pkg in ("PySide6", "matplotlib", "paramiko", "cryptography"):
    collected = collect_all(pkg)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

try:
    datas += collect_data_files("certifi")
except Exception:
    pass
try:
    binaries += collect_dynamic_libs("numpy")
except Exception:
    pass

exe_icon = str(icon) if icon.is_file() else None

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "pyi_rth_crfs.py")],
    excludes=["pytest", "tkinter", "IPython", "jupyter", "scipy", "pandas"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    name="CRFS_IQ_Recorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
if exe_icon:
    exe_kwargs["icon"] = exe_icon

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    **exe_kwargs,
)
