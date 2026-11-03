"""Open a file or folder with the desktop's own application (Explorer on Windows)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_path(path: str | Path) -> bool:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))


def reveal(path: str | Path) -> bool:
    """Show a file selected in its folder; open the folder itself when that is not possible."""
    target = Path(path)
    if sys.platform == "win32" and target.is_file():
        try:
            # Explorer parses its own command line: the path is quoted after "/select,".
            subprocess.Popen(f'explorer /select,"{os.path.normpath(str(target))}"')
            return True
        except OSError:
            pass
    folder = target if target.is_dir() else target.parent
    return open_path(folder)
