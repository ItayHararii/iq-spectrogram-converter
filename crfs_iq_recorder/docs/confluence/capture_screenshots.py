"""Capture Confluence screenshots from demo widgets. No live sensor, IP, or password.

Writes PNGs next to this file:
  01-main-window.png          main window after a short demo session
  02-sensor-files.png         Sensor Files window with a grouped split recording
  03-waterfall-preview.png    waterfall preview of the selected file
  04-settings.png             Settings dialog (example values only)
  05-excel-section.png        "Log recordings to Excel" part of the main window
  06-workbook-before.png      collection workbook before any recording (Excel)
  07-workbook-after.png       the same workbook after the app logged two recordings
  08-workbook-folder.png      workbook folder with the one-time .crfs-backup copy

The workbook images need Microsoft Excel on this PC. Without it they are skipped.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QApplication, QFileIconProvider, QTreeWidget, QTreeWidgetItem

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from crfs_iq_recorder.connection_dialog import ConnectionDialog
from crfs_iq_recorder.connection_state import ConnectionState
from crfs_iq_recorder.excel_log import ExcelLogStore, flush_excel_queue, pending_from_row
from crfs_iq_recorder.excel_writer import ExcelRow
from crfs_iq_recorder.frequency import FreqTextStyle
from crfs_iq_recorder.gui import MainWindow, configure_appearance
from crfs_iq_recorder.recording_history import add_recording, new_recording, recording_group_key
from crfs_iq_recorder.sensor_info import SensorInfo
from crfs_iq_recorder.sftp_window import SftpWindow

EXAMPLE_HOST = "example"
EXAMPLE_DOWNLOAD = r"C:\CRFS IQ Recorder\Recordings"
EXAMPLE_WORKBOOK = "Collection workbook.xlsm"
SHOWN_WORKBOOK = r"C:\Collection\Collection workbook.xlsm"

HEADERS = [
    "Collection Event",
    "Class",
    "Freq Start",
    "Freq Stop",
    "RBW",
    "Scan Rate",
    "Sensor",
    "Snapshot ID",
    "IQ Recording",
    "IQ Image",
    "Playback Start",
    "Playback End",
    "Playback Time (UTC-3)",
]
WIDTHS = [17, 14, 13, 13, 9, 11, 14, 13, 30, 11, 15, 14, 22]

# Planned rows the analyst prepared in advance (no IQ yet).
PLANNED = {
    "SATCOM": [
        ("Haifa Port", "Iridium", "1616 MHz", "1626 MHz", "10 kHz", "50 GHz/s", "CRFS 100-18"),
        ("Haifa Port", "Inmarsat-C", "1525 MHz", "1559 MHz", "10 kHz", "50 GHz/s", "CRFS 100-18"),
        ("Haifa Port", "GPS L1", "1574 MHz", "1577 MHz", "10 kHz", "50 GHz/s", "CRFS 100-18"),
    ],
    "Cellular": [
        ("Haifa Port", "GSM-900 DL", "935 MHz", "960 MHz", "30 kHz", "50 GHz/s", "CRFS 100-18"),
    ],
}


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


def _sample_workbook(path: Path) -> Path:
    """A small collection workbook that looks like the real one (styled header, planned rows)."""
    wb = Workbook()
    first = True
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F4E78")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for sheet, rows in PLANNED.items():
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = sheet
        for col, header in enumerate(HEADERS, start=1):
            cell = ws.cell(1, col, header)
            cell.font = head_font
            cell.fill = head_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[cell.column_letter].width = WIDTHS[col - 1]
        ws.row_dimensions[1].height = 30
        for r, values in enumerate(rows, start=2):
            for col in range(1, len(HEADERS) + 1):
                cell = ws.cell(r, col)
                cell.border = border
                cell.alignment = Alignment(vertical="center")
            for col, value in enumerate(values, start=1):
                ws.cell(r, col, value)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{ws.cell(1, len(HEADERS)).column_letter}{len(rows) + 1}"
    xlsx = path.with_suffix(".xlsx")
    wb.save(xlsx)
    return xlsx


def _excel():
    try:
        import win32com.client as win32
    except ImportError:
        return None
    try:
        return win32.DispatchEx("Excel.Application")
    except Exception:
        return None


def _to_xlsm(xlsx: Path, xlsm: Path) -> bool:
    """Save the sample as a real macro-enabled workbook so the app treats it like the field one."""
    xl = _excel()
    if xl is None:
        return False
    try:
        xl.Visible = False
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(str(xlsx))
        if xlsm.exists():
            xlsm.unlink()
        wb.SaveAs(str(xlsm), FileFormat=52)  # xlOpenXMLWorkbookMacroEnabled
        wb.Close(SaveChanges=False)
        return True
    finally:
        xl.Quit()


def _grab_hwnd(hwnd: int, path: Path, *, crop_top: int = 0) -> bool:
    """Render a top-level window to PNG even if another window covers it."""
    import ctypes

    import win32gui
    import win32ui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return False
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bmp)
    ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    try:
        if not ok:
            return False
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        from PIL import Image

        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), data, "raw", "BGRX", 0, 1)
        # Trim the invisible resize border Windows adds around top-level windows.
        img = img.crop((8, crop_top, img.width - 8, img.height - 8))
        img.save(path, "PNG")
        print(f"Wrote {path} ({path.stat().st_size} bytes)")
        return True
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _shoot_workbook(xlsm: Path, out: Path, *, sheet: str = "SATCOM", select: str | None = None) -> bool:
    xl = _excel()
    if xl is None:
        return False
    try:
        xl.Visible = True
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(str(xlsm), ReadOnly=True)
        ws = wb.Worksheets(sheet)
        ws.Activate()
        xl.WindowState = -4143  # xlNormal
        xl.Left, xl.Top, xl.Width, xl.Height = 20, 20, 1180, 520
        xl.ActiveWindow.Zoom = 100
        xl.ActiveWindow.ScrollColumn = 1
        xl.ActiveWindow.ScrollRow = 1
        ws.Range(select or "A1").Select()
        time.sleep(1.2)
        # Keep the formula bar, column letters, grid, and sheet tabs; drop the title bar and ribbon.
        ok = _grab_hwnd(int(xl.Hwnd), out, crop_top=190)
        wb.Close(SaveChanges=False)
        return ok
    finally:
        xl.Quit()


def _log_recordings(xlsm: Path, store_path: Path, download_dir: Path) -> None:
    """Run the app's real Excel pipeline for two finished recordings."""
    download_dir.mkdir(parents=True, exist_ok=True)
    local_single = download_dir / "iq_20261011_101532.wav"
    local_single.write_bytes(b"RIFF")  # downloaded file exists -> cell gets a hyperlink
    store = ExcelLogStore(store_path)
    mhz = FreqTextStyle("MHz", True)
    sensor = "CRFS 100-18"
    remote_dir = "/mnt/1/remdata/20261011"

    def row(cls, start, end, name, duration, record_id, local=""):
        return ExcelRow(
            worksheet="SATCOM",
            collection_event="Haifa Port",
            target_class=cls,
            start_hz=start,
            end_hz=end,
            start_style=mhz,
            stop_style=mhz,
            sensor=sensor,
            filename=name,
            local_path=local,
            remote_path=f"{remote_dir}/{name}",
            sensor_id="rfeyeDEMO",
            duration_s=duration,
            record_id=record_id,
            group_key=recording_group_key(
                sensor_id="rfeyeDEMO",
                directory=remote_dir,
                stem=name.rsplit("_0", 1)[0] if "_000" in name else name,
                record_id=record_id,
            ),
        )

    items = [
        row("Iridium", 1_616_000_000, 1_626_000_000, "iq_20261011_101532.wav", 2, "rec-1", str(local_single)),
        row("Inmarsat-C", 1_525_000_000, 1_559_000_000, "iq_20261011_102210_0001.wav", 5, "rec-2"),
        row("Inmarsat-C", 1_525_000_000, 1_559_000_000, "iq_20261011_102210_0002.wav", 5, "rec-2"),
    ]
    for item in items:
        store.enqueue(pending_from_row(workbook=xlsm, row=item))
    saved, pending = flush_excel_queue(store)
    print(f"Excel pipeline: saved={saved} pending={pending}")


def _folder_listing(folder: Path, out: Path, app: QApplication) -> None:
    """A small Explorer-like listing of the workbook folder (shows the backup copy)."""
    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "Type", "Size"])
    tree.setRootIsDecorated(False)
    tree.setAlternatingRowColors(True)
    tree.setIconSize(QSize(20, 20))
    provider = QFileIconProvider()
    from PySide6.QtCore import QFileInfo

    for file in sorted(folder.iterdir()):
        if file.suffix.lower() not in {".xlsm", ".xlsx"} or file.name.endswith(".xlsx"):
            continue
        kind = "Microsoft Excel Macro-Enabled Worksheet"
        size = f"{max(1, round(file.stat().st_size / 1024))} KB"
        item = QTreeWidgetItem([file.name, kind, size])
        item.setIcon(0, provider.icon(QFileInfo(str(file))))
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tree.addTopLevelItem(item)
    tree.setColumnWidth(0, 420)
    tree.setColumnWidth(1, 300)
    tree.resize(840, 110)
    tree.show()
    app.processEvents()
    time.sleep(0.2)
    _save(tree, out)
    tree.close()


def main() -> int:
    out = Path(__file__).resolve().parent
    tmp = Path(tempfile.mkdtemp(prefix="crfs-docs-shot-"))
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    from crfs_iq_recorder import paths as app_paths
    from crfs_iq_recorder import recording_history

    app_paths.settings_path = lambda: tmp / "settings.json"  # type: ignore[method-assign]
    app_paths.excel_log_path = lambda: tmp / "excel_log.json"  # type: ignore[method-assign]
    recording_history.recordings_path = lambda: tmp / "recordings.json"  # type: ignore[method-assign]
    add_recording(
        new_recording(
            host=EXAMPLE_HOST,
            start_hz=1_616_000_000,
            end_hz=1_626_000_000,
            center_hz=1_621_000_000,
            bandwidth_hz=10_000_000,
            duration_s=2,
            started_at=datetime(2026, 10, 11, 10, 15, 32),
            demo=True,
            iq_stem="iq_20261011_101532",
        )
    )

    # Workbook: build the sample, make it a real .xlsm, photograph it before and after.
    book_dir = tmp / "Collection"
    book_dir.mkdir()
    xlsx = _sample_workbook(book_dir / EXAMPLE_WORKBOOK)
    book = book_dir / EXAMPLE_WORKBOOK
    have_excel = _to_xlsm(xlsx, book)
    if not have_excel:
        print("Excel not available: workbook screenshots skipped; using .xlsx for the UI")
        book = xlsx
    else:
        _shoot_workbook(book, out / "06-workbook-before.png")
        _log_recordings(book, tmp / "excel_log.json", tmp / "Recordings")
        _shoot_workbook(book, out / "07-workbook-after.png", select="I5")

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)

    if have_excel:
        _folder_listing(book_dir, out / "08-workbook-folder.png", app)

    window = MainWindow(demo=True)
    window._conn.host = EXAMPLE_HOST
    window._conn.download_dir = EXAMPLE_DOWNLOAD
    window.conn_label.setText(EXAMPLE_HOST)
    window._apply_sensor_info(
        SensorInfo(
            host=EXAMPLE_HOST,
            model="R100-18",
            firmware="2.25-325",
            serial="rfeyeDEMO",
            status="Connected",
            connected=True,
        )
    )
    window._refresh_storage()
    window.mode_start.setChecked(True)
    window.field_a.setText("1616")
    window.field_b.setText("1626")
    window.time_edit.setText("2")
    window.excel_check.setChecked(True)
    window._load_workbook(str(book), quiet=True)
    window.workbook_edit.setText(SHOWN_WORKBOOK)
    window.event_combo.setCurrentText("Haifa Port")
    window.class_combo.setCurrentText("Iridium")
    window._refresh_class_suggestion()
    window._refresh_size()
    window._recordings_completed = 2
    window._files_found = 3
    window._update_collection_status()
    window.collection_status.setText("Recordings completed: 2  ·  Files found: 3  ·  Excel: Saved")
    for line in (
        "Connected to example (R100-18, firmware 2.25-325).",
        "Workbook loaded: 2 worksheets, 4 classes, event 'Haifa Port'.",
        "Recording 1616-1626 MHz for 2 s (Iridium).",
        "Capture finished with 1 file(s).",
        "Excel: saved 1 row(s).",
        "Recording 1525-1559 MHz for 5 s (Inmarsat-C).",
        "Capture finished with 2 file(s).",
        "Excel: saved 1 row(s).",
    ):
        window.log(line)
    window.resize(1100, 1180)
    window.show()
    window.raise_()
    window.activateWindow()
    app.processEvents()
    time.sleep(0.4)
    app.processEvents()
    _save(window, out / "01-main-window.png")
    _save(window.excel_section, out / "05-excel-section.png")

    settings = ConnectionDialog(
        ConnectionState(
            demo=True,
            host="sensor-01.example.local",
            username="admin",
            http_password="",
            sftp_username="root",
            sftp_password="",
            download_dir=EXAMPLE_DOWNLOAD,
        ),
        window,
    )
    settings.show()
    settings.setMinimumHeight(820)
    settings.resize(560, 820)
    app.processEvents()
    time.sleep(0.3)
    _save(settings, out / "04-settings.png")
    settings.close()

    sftp = SftpWindow(
        ConnectionState(
            demo=True,
            host=EXAMPLE_HOST,
            download_dir=EXAMPLE_DOWNLOAD,
        )
    )
    sftp.resize(1240, 720)
    sftp.show()
    sftp.raise_()
    app.processEvents()
    time.sleep(0.3)
    _wait(app, lambda: sftp.table.topLevelItemCount() >= 2)
    group = next(
        (
            sftp.table.topLevelItem(i)
            for i in range(sftp.table.topLevelItemCount())
            if sftp.table.topLevelItem(i) and sftp.table.topLevelItem(i).childCount()
        ),
        sftp.table.topLevelItem(0),
    )
    if group is not None:
        sftp.table.clearSelection()
        group.setExpanded(True)
        group.setSelected(True)
        sftp.table.setCurrentItem(group)
        sftp._on_selection_changed()
    _wait(
        app,
        lambda: sftp.preview_image.pixmap() is not None and not sftp.preview_image.pixmap().isNull(),
        10,
    )
    app.processEvents()
    _save(sftp, out / "02-sensor-files.png")
    _save(sftp.preview_image, out / "03-waterfall-preview.png")

    sftp.close()
    window.close()
    app.processEvents()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
