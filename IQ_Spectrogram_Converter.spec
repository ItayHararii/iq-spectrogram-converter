# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [('assets', 'assets')]
_recorder_icon = Path('crfs_iq_recorder/assets/sensorz_icon.ico')
_local_icon = Path('assets/sensorz_icon.ico')
_exe_icon = _recorder_icon if _recorder_icon.is_file() else _local_icon
binaries = []
hiddenimports = ['iq_data', 'iq_theme', 'PIL', 'PIL.ImageTk', 'numpy', 'matplotlib', 'seaborn', 'scipy', 'soundfile', 'tkinterdnd2']
tmp_ret = collect_all('matplotlib')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('seaborn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['iq_gui.py'],
    pathex=[],
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='IQ_Spectrogram_Converter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(_exe_icon).replace('\\', '/')],
)
