# Runtime setup for the frozen Windows exe (matplotlib cache, Qt API).
import os
import sys
import tempfile
from pathlib import Path

if getattr(sys, "frozen", False):
    os.environ.setdefault("QT_API", "pyside6")
    os.environ.setdefault("MPLBACKEND", "Agg")
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "CRFS IQ Recorder"
    mpl = root / "cache" / "matplotlib"
    try:
        mpl.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(mpl))
    except OSError:
        pass
