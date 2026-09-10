# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
root = Path(SPECPATH)

datas = [(str(root / "docs"), "docs")]
binaries = []
hiddenimports = collect_submodules("crfs_iq_recorder")
hiddenimports += ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "requests", "paramiko"]

tmp = collect_all("PySide6")
datas += tmp[0]
binaries += tmp[1]
hiddenimports += tmp[2]

icon = root.parent / "assets" / "sensorz_icon.ico"
icon_arg = str(icon) if icon.is_file() else None

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
if icon_arg:
    exe_kwargs["icon"] = icon_arg

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    **exe_kwargs,
)
