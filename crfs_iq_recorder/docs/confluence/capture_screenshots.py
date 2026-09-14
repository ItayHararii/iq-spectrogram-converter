"""Capture Confluence screenshots from the live CRFS IQ Recorder widgets."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from crfs_iq_recorder.connection_state import ConnectionState
from crfs_iq_recorder.gui import MainWindow, configure_appearance
from crfs_iq_recorder.paths import default_download_dir
from crfs_iq_recorder.recording_history import add_recording, new_recording
from crfs_iq_recorder.sensor_info import SensorInfo
from crfs_iq_recorder.sftp_window import SftpWindow


def _wait(app: QApplication, pred, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


def _save(widget, path: Path) -> None:
    pix = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pix.save(str(path), "PNG"):
        raise SystemExit(f"Could not save {path}")
    print(f"Wrote {path} ({path.stat().st_size} bytes)")


def main() -> int:
    out = Path(__file__).resolve().parent
    recordings = Path(tempfile.gettempdir()) / "crfs_iq_recorder_shot_recordings.json"
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    from crfs_iq_recorder import recording_history

    recording_history.recordings_path = lambda: recordings  # type: ignore[method-assign]
    add_recording(
        new_recording(
            host="10.1.0.11",
            start_hz=793_750_000,
            end_hz=806_250_000,
            center_hz=800_000_000,
            bandwidth_hz=12_500_000,
            duration_s=0.1,
            started_at=datetime(2026, 9, 10, 16, 16, 29),
            demo=True,
            iq_stem="iq_20260910_161629",
        )
    )

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)

    window = MainWindow(demo=True)
    window._conn.host = "10.1.0.11"
    window._conn.download_dir = str(default_download_dir())
    window.conn_label.setText("10.1.0.11")
    window._apply_sensor_info(
        SensorInfo(
            host="10.1.0.11",
            model="R40-8",
            firmware="2.25-325",
            serial="rfeye300540",
            status="Connected",
            connected=True,
        )
    )
    window.mode_center.setChecked(True)
    window.field_a.setText("800")
    window.field_b.setText("12.5")
    window.time_edit.setText("0.1")
    window._refresh_size()
    window.resize(1100, 720)
    window.show()
    window.raise_()
    window.activateWindow()
    app.processEvents()
    time.sleep(0.4)
    app.processEvents()
    _save(window, out / "01-main-window.png")
    refresh = ROOT / "docs" / "ui_refresh"
    _save(window, refresh / "after-main.png")

    from crfs_iq_recorder.connection_dialog import ConnectionDialog

    settings = ConnectionDialog(window._conn, window)
    settings.show()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    _save(settings, refresh / "after-settings.png")
    settings.close()

    sftp = SftpWindow(
        ConnectionState(
            demo=True,
            host="10.1.0.11",
            download_dir=str(default_download_dir()),
        )
    )
    sftp.resize(1240, 720)
    sftp.show()
    sftp.raise_()
    app.processEvents()
    time.sleep(0.3)
    _wait(app, lambda: sftp.table.rowCount() >= 2)
    files = [entry for entry in sftp._entries if not entry.is_dir]
    target = next((i for i, e in enumerate(sftp._entries) if "0001" in e.name), 0)
    if files:
        sftp.table.selectRow(target)
        sftp.table.setCurrentCell(target, 1)
        sftp._on_selection_changed()
    _wait(app, lambda: not sftp.preview_image.pixmap() is None and not sftp.preview_image.pixmap().isNull(), 10)
    app.processEvents()
    _save(sftp, out / "02-sensor-files.png")
    _save(sftp.preview_image, out / "03-waterfall-preview.png")
    _save(sftp, refresh / "after-sftp.png")

    sftp.close()
    window.close()
    app.processEvents()
    try:
        recordings.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
