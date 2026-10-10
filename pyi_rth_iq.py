# Runtime setup for the frozen Windows exe (matplotlib cache location).
#
# matplotlib builds a font cache on first import. Inside a one-file EXE the
# default location is the per-launch extraction folder in %TEMP%, so the cache
# is thrown away every time and rebuilt on the next launch (several seconds).
# Point it at a stable per-user folder instead.
import os
import sys
import tempfile
from pathlib import Path

if getattr(sys, "frozen", False):
    os.environ.setdefault("MPLBACKEND", "Agg")
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "CRFS IQ Spectrogram Converter"
    mpl = root / "cache" / "matplotlib"
    try:
        mpl.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(mpl))
    except OSError:
        pass
