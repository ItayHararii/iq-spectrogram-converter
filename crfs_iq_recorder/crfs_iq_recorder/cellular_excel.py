"""Excel rows for cellular captures through the existing, persistent Excel queue.

Rows use the collection workbook's existing columns (Collection Event,
Class, Freq Start/Stop, Sensor, IQ Recording, Playback Start) exactly as
manual recordings do, so headers, layout, formulas and VBA are preserved
by the same writer. The worksheet is the one mapped to the task's target
class in the workbook; when no such worksheet exists, a clearly named
results sheet is created once (after the workbook backup). The protocol
worksheet itself is never written to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .collection_catalog import (
    HEADER_CLASS,
    HEADER_COLLECTION,
    HEADER_IMAGE,
    HEADER_IQ,
    HEADER_PLAY_END,
    HEADER_PLAY_START,
    HEADER_PLAY_TIME,
    HEADER_RBW,
    HEADER_SCAN,
    HEADER_SENSOR,
    HEADER_SNAPSHOT,
    HEADER_START,
    HEADER_STOP,
)
from .download_paths import existing_copy, local_destination
from .excel_log import ExcelLogStore, pending_from_row
from .excel_writer import ExcelBusyError, ExcelRow
from .frequency import FreqTextStyle
from .recording_history import join_iq_filenames, recording_group_key, remote_directory, sort_iq_filenames

RESULTS_SHEET = "Cellular Results"
RESULTS_HEADERS = (
    HEADER_COLLECTION,
    HEADER_CLASS,
    HEADER_START,
    HEADER_STOP,
    HEADER_RBW,
    HEADER_SCAN,
    HEADER_SENSOR,
    HEADER_SNAPSHOT,
    HEADER_IQ,
    HEADER_IMAGE,
    HEADER_PLAY_START,
    HEADER_PLAY_END,
    HEADER_PLAY_TIME,
)
_MHZ_STYLE = FreqTextStyle(unit="MHz", space_before_unit=True)


def collection_event_text(campaign: str, site: str, round_id: str) -> str:
    parts = [p.strip() for p in (campaign, site) if p and p.strip()]
    text = " / ".join(parts) if parts else "Cellular collection"
    return f"{text} / {round_id}" if round_id else text


def class_text(task: dict[str, Any]) -> str:
    """The workbook's class when it has one; otherwise a label naming band and direction."""
    explicit = str(task.get("target_class") or "").strip()
    if explicit:
        return explicit
    band = str(task.get("band_label") or task.get("band") or "band").strip()
    direction = str(task.get("direction") or "").strip()
    return f"Cellular {band} {direction}".strip()


def worksheet_for_task(task: dict[str, Any], catalog: Any) -> str:
    """Worksheet mapped to the task's class in the collection workbook, else the results sheet."""
    wanted = class_text(task).casefold()
    mappings = getattr(catalog, "mappings", None) or []
    for mapping in mappings:
        if str(getattr(mapping, "target_class", "")).casefold() == wanted:
            return str(getattr(mapping, "worksheet", "")) or RESULTS_SHEET
    return RESULTS_SHEET


def ensure_results_sheet(store: ExcelLogStore, workbook: Path, sheet: str = RESULTS_SHEET) -> bool:
    """Create the results worksheet with the standard headers if it is missing.

    Returns True when the sheet exists afterwards. Backs the workbook up
    first (once per workbook, through the store). Raises ExcelBusyError when
    the file is locked by Excel so the caller can try again later.
    """
    from openpyxl import load_workbook

    workbook = Path(workbook)
    if not workbook.is_file():
        return False
    try:
        wb = load_workbook(workbook, read_only=True, keep_vba=False)
    except PermissionError as exc:
        raise ExcelBusyError(f"Could not open {workbook.name}. It may be open in Excel.") from exc
    try:
        exists = sheet in wb.sheetnames
    finally:
        wb.close()
    if exists:
        return True
    from .excel_com import BUSY, add_worksheet_com, excel_workbook_state

    state = excel_workbook_state(workbook)
    if state == BUSY:
        raise ExcelBusyError(f"Excel is busy; the results sheet in {workbook.name} is created when it is free.")
    if state == "open":
        # Open in Excel: the sheet is added through the open window (the file
        # on disk is Excel's to write), and saved there.
        store.ensure_backup(workbook)
        if add_worksheet_com(workbook, sheet, list(RESULTS_HEADERS)):
            return True
        raise ExcelBusyError(f"{workbook.name} is open in Excel; the results sheet is created when Excel is free.")
    store.ensure_backup(workbook)
    from .openpyxl_links import load_workbook_keeping_links

    try:
        wb = load_workbook_keeping_links(workbook, keep_vba=workbook.suffix.casefold() == ".xlsm")
    except PermissionError as exc:
        raise ExcelBusyError(f"Could not open {workbook.name}. It may be open in Excel.") from exc
    try:
        ws = wb.create_sheet(sheet)
        for column, header in enumerate(RESULTS_HEADERS, start=1):
            ws.cell(1, column).value = header
        try:
            wb.save(workbook)
        except PermissionError as exc:
            raise ExcelBusyError(f"Could not save {workbook.name}. Excel may have it open.") from exc
    finally:
        vba = getattr(wb, "vba_archive", None)
        if vba is not None:
            try:
                vba.close()
            except Exception:
                pass
            wb.vba_archive = None
        wb.close()
    return True


def queue_capture_rows(
    store: ExcelLogStore,
    *,
    workbook: Path,
    worksheet: str,
    task: dict[str, Any],
    collection_event: str,
    sensor_name: str,
    sensor_id: str,
    record_id: str,
    files: list[dict[str, Any]],
    duration_s: float | None,
    local_dir: Path,
) -> int:
    """Queue one row per split part (the writer merges parts of one capture into one row).

    Sample files only: XDAT header/marker sidecars stay beside them in the
    download folder and are not recordings in their own right.
    """
    from .xdat_header import is_xdat_sidecar

    queued = 0
    names = sort_iq_filenames([str(f.get("name") or "") for f in files if f.get("name") and not is_xdat_sidecar(str(f.get("name")))])
    by_name = {str(f.get("name")): f for f in files}
    for name in names:
        entry = by_name[name]
        remote_path = str(entry.get("remote_path") or "")
        local = Path(entry["local_path"]) if entry.get("local_path") else None
        if local is None or not local.is_file():
            # Not downloaded by the runner (or moved): look where the sensor's dated folder puts it, then the root.
            found = existing_copy(local_dir, remote_path or name, int(entry.get("size") or 0))
            local = found if found is not None else local_destination(local_dir, remote_path or name)
        row = ExcelRow(
            worksheet=worksheet,
            collection_event=collection_event,
            target_class=class_text(task),
            start_hz=int(task.get("window_start_hz") or 0),
            end_hz=int(task.get("window_stop_hz") or 0),
            start_style=_MHZ_STYLE,
            stop_style=_MHZ_STYLE,
            sensor=sensor_name,
            filename=name,
            local_path=str(local) if local.is_file() else "",
            remote_path=remote_path,
            sensor_id=sensor_id,
            duration_s=duration_s,
            record_id=record_id,
            group_key=recording_group_key(
                sensor_id=sensor_id,
                directory=remote_directory(remote_path),
                stem=_stem(name),
                record_id=record_id,
            ),
        )
        if store.enqueue(pending_from_row(workbook=workbook, row=row)):
            queued += 1
    return queued


def _stem(name: str) -> str:
    from .recording_history import iq_group_key

    return iq_group_key(name)


def joined_names(files: list[dict[str, Any]]) -> str:
    return join_iq_filenames(sort_iq_filenames([str(f.get("name") or "") for f in files if f.get("name")]))
