"""Cell-level results in the protocol worksheet: first pass in place, later passes appended."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table, TableStyleInfo

from crfs_iq_recorder.cellular_protocol import import_protocol
from crfs_iq_recorder.cellular_workbook import (
    BackupError,
    BlockRef,
    DATETIME_FORMAT,
    DURATION_FORMAT,
    RowResult,
    WorkbookQueue,
    build_row_result,
    capture_start_from_stem,
    ensure_backup,
    flush_queue,
    layout_from_import,
    plan_rows_from_import,
    write_results_openpyxl,
)
from crfs_iq_recorder.excel_writer import ExcelBusyError

HEADERS = [
    "Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)",
    "Sensor", "IQ Recording", "IQ Duration (s)", "Recording Date & Time",
]
ROWS = [
    ("700 MHz (B28)", "DL", "FDD", 753, 803),
    ("700 MHz (B28)", "UL", "FDD", 698, 748),
    ("1800 MHz (B3)", "DL", "FDD", 1805, 1880),
    ("3500 MHz (n78)", "TDD / shared UL-DL", "TDD", 3300, 3800),
]


def make_workbook(path: Path, *, sensor: str = "CRFS 100-18", table: bool = True, rows=None) -> Path:
    rows = list(rows) if rows is not None else ROWS
    wb = Workbook()
    ws = wb.active
    ws.title = "Cellular Protocol"
    ws.append(HEADERS)
    for band, direction, duplex, start, stop in rows:
        ws.append([band, direction, duplex, start, stop, sensor, None, None, None])
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=5):
        for cell in row:
            cell.number_format = '0" MHz"'
    if table:
        t = Table(displayName="CellularProtocol", ref=f"A1:I{len(rows) + 1}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)
    notes = wb.create_sheet("Notes")
    notes["A1"] = "manual note that must survive"
    wb.save(path)
    return path


def _import(path: Path):
    imported = import_protocol(path, "Cellular Protocol")
    assert not imported.errors, [i.message for i in imported.issues]
    return imported


def _result(round_id: str, index: int, files: list[str], duration: float, when: datetime, *, sensor: str = "CRFS 100-18") -> RowResult:
    return RowResult(round_id=round_id, plan_index=index, files=files, duration_s=duration, recorded_at=when, sensor=sensor)


def _values(path: Path, sheet: str = "Cellular Protocol") -> list[list]:
    wb = load_workbook(path)
    try:
        ws = wb[sheet]
        return [[c.value for c in row] for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=9)]
    finally:
        wb.close()


# ----------------------------------------------------------------------------- layout and plan
def test_layout_and_plan_follow_the_nine_columns(tmp_path: Path):
    imported = _import(make_workbook(tmp_path / "p.xlsx"))
    layout = layout_from_import(imported)
    assert (layout.band, layout.direction, layout.duplex, layout.start, layout.stop) == (1, 2, 3, 4, 5)
    assert (layout.sensor, layout.result, layout.duration, layout.time) == (6, 7, 8, 9)
    assert layout.last_column == 9 and layout.header_row == 1
    plan = plan_rows_from_import(imported)
    assert [r.sheet_row for r in plan] == [2, 3, 4, 5]
    assert plan[2].key.label() == "1800 MHz (B3) DL 1805–1880 MHz"
    assert plan[3].key.start_hz == 3_300_000_000


# ----------------------------------------------------------------------------- first pass, later passes
def test_first_pass_fills_the_plan_rows_in_place(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    when = datetime(2026, 11, 2, 18, 44, 7)
    results = [
        _result("r1", 0, ["iq_20261102_164407_0000.wav"], 2.011, when),
        _result("r1", 2, ["iq_20261102_164430_0000.wav", "iq_20261102_164430_0001.wav"], 5.0, when.replace(second=30)),
    ]
    report = write_results_openpyxl(path, layout=layout, plan=plan, results=results, blocks={})
    assert [(r[1], r[2]) for r in report.written] == [(0, 2), (2, 4)]
    assert report.blocks["r1"].ordinal == 0
    wb = load_workbook(path)
    ws = wb["Cellular Protocol"]
    assert ws["G2"].value == "iq_20261102_164407_0000.wav"
    assert ws["H2"].value == pytest.approx(2.011) and ws["H2"].number_format == DURATION_FORMAT
    assert ws["I2"].value == when and ws["I2"].number_format == DATETIME_FORMAT
    assert ws["G4"].value == "iq_20261102_164430_0000.wav\niq_20261102_164430_0001.wav"
    assert ws["G4"].alignment.wrap_text is True
    assert ws["D2"].value == 753 and ws["D2"].number_format == '0" MHz"'
    # Untouched rows and other sheets stay as they were.
    assert ws["G3"].value is None and ws["G5"].value is None
    assert ws.max_row == 5
    assert wb["Notes"]["A1"].value == "manual note that must survive"
    assert dict(ws.tables.items()) == {"CellularProtocol": "A1:I5"}
    wb.close()


def test_second_pass_appends_a_block_and_keeps_the_first(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    t1 = datetime(2026, 11, 2, 9, 0, 0)
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", i, [f"a{i}.wav"], 2.0, t1) for i in range(4)], blocks={})
    # A manual note typed into the first block after it was written.
    wb = load_workbook(path)
    wb["Cellular Protocol"]["G3"].value = "a1.wav"  # unchanged
    wb["Cellular Protocol"]["J3"].value = "manual remark"
    wb.save(path)
    wb.close()
    t2 = datetime(2026, 11, 2, 15, 0, 0)
    second = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r2", 1, ["b1.wav"], 2.011, t2)], blocks=first.blocks)
    block = second.blocks["r2"]
    assert block.ordinal == 1 and block.first_row == 6
    rows = _values(path)
    assert rows[5][:5] == ["700 MHz (B28)", "DL", "FDD", 753, 803]  # row 6 copies plan row 2
    assert rows[6][:5] == ["700 MHz (B28)", "UL", "FDD", 698, 748]
    assert rows[6][6:] == ["b1.wav", pytest.approx(2.011), t2]
    assert rows[5][6:] == [None, None, None]  # not recorded yet in round 2
    assert rows[5][5] == "CRFS 100-18"  # sensor copied from the plan row
    assert rows[1][6:] == ["a0.wav", 2.0, t1]  # first block intact
    assert len(rows) == 9
    wb = load_workbook(path)
    ws = wb["Cellular Protocol"]
    assert ws["J3"].value == "manual remark"
    assert ws["D6"].number_format == '0" MHz"'
    assert dict(ws.tables.items()) == {"CellularProtocol": "A1:I9"}
    wb.close()
    # Writing the rest of round 2 later lands in the same block (located again, not re-allocated).
    third = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r2", 3, ["b3.wav"], 10.5, t2)], blocks=second.blocks)
    assert third.blocks["r2"].first_row == 6 and third.written == [("r2", 3, 9)]
    assert _values(path)[8][6] == "b3.wav"


def test_plan_rows_that_already_hold_results_get_a_new_block(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    wb = load_workbook(path)
    wb["Cellular Protocol"]["G2"].value = "old.wav"  # left over from an earlier day
    wb.save(path)
    wb.close()
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    report = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", 0, ["new.wav"], 1.0, datetime(2026, 1, 1, 8))], blocks={})
    assert report.blocks["r1"].ordinal == 1 and report.blocks["r1"].first_row == 6
    rows = _values(path)
    assert rows[1][6] == "old.wav" and rows[5][6] == "new.wav"


def test_rows_inserted_by_hand_above_a_block_are_found_again(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    t = datetime(2026, 11, 2, 9)
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", i, [f"a{i}.wav"], 2.0, t) for i in range(4)], blocks={})
    second = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r2", 0, ["b0.wav"], 2.0, t) for _ in [0]], blocks=first.blocks)
    assert second.blocks["r2"].first_row == 6
    # Someone inserts two rows between the blocks.
    wb = load_workbook(path)
    wb["Cellular Protocol"].insert_rows(6, amount=2)
    wb["Cellular Protocol"]["A6"].value = "note between passes"
    wb.save(path)
    wb.close()
    third = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r2", 1, ["b1.wav"], 2.0, t)], blocks=second.blocks)
    assert third.blocks["r2"].first_row == 8 and third.written == [("r2", 1, 9)]
    rows = _values(path)
    assert rows[8][:2] == ["700 MHz (B28)", "UL"] and rows[8][6] == "b1.wav"
    assert rows[5][0] == "note between passes"


def test_a_row_that_lists_other_files_is_left_alone(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    t = datetime(2026, 11, 2, 9)
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", 0, ["a0.wav"], 2.0, t)], blocks={})
    wb = load_workbook(path)
    wb["Cellular Protocol"]["G2"].value = "renamed_by_hand.wav"
    wb.save(path)
    wb.close()
    again = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", 0, ["a0.wav"], 2.0, t)], blocks=first.blocks)
    assert again.written == [] and any("left unchanged" in s for s in again.skipped)
    assert _values(path)[1][6] == "renamed_by_hand.wav"


def test_a_hand_typed_duration_is_a_conflict_reported_once_never_retried_or_appended(tmp_path: Path):
    """Retry rule: a row the application did not fill (a duration or time typed in, no file
    listed) is left unchanged, reported, and not queued again; no row is added for it."""
    path = make_workbook(tmp_path / "p.xlsx")
    wb = load_workbook(path)
    wb["Cellular Protocol"]["H3"].value = 5
    wb.save(path)
    wb.close()
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "state" / "workbook_queue.json")
    t = datetime(2026, 11, 3, 9)
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, t))
    queue.enqueue(path, layout, plan, _result("r1", 1, ["a1.wav"], 2.0, t))
    assert queue.row_state("r1", 0) == "pending" and queue.row_state("r1", 1) == "pending"
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.written == 1 and report.pending == 0
    assert any("entered by hand" in note for note in report.notes)
    assert queue.row_state("r1", 0) == "written"
    assert queue.row_state("r1", 1) == "conflict"
    values = _values(path)
    assert values[2][6] is None and values[2][7] == 5, "the typed duration stays; no file is added"
    assert len(values) == 5, "no row appended"
    # The same result queued again is reported again and still not written or appended.
    queue.enqueue(path, layout, plan, _result("r1", 1, ["a1.wav"], 2.0, t))
    again = flush_queue(queue, excel_open=lambda p: False)
    assert again.written == 0 and again.pending == 0 and queue.row_state("r1", 1) == "conflict"
    assert len(_values(path)) == 5


def test_row_state_reports_a_failed_write_as_retrying_until_it_lands(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "state" / "workbook_queue.json")
    queue.enqueue(path, layout, plan, _result("r1", 2, ["b.wav"], 1.0, datetime(2026, 11, 3, 9)))
    moved = path.with_name("away.xlsx")
    path.rename(moved)
    report = flush_queue(queue, excel_open=lambda p: False)
    assert "not found" in report.error and queue.row_state("r1", 2) == "retrying"
    moved.rename(path)
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.ok and report.written == 1 and queue.row_state("r1", 2) == "written"
    assert queue.row_state("r1", 3) == "" and queue.row_state("r2", 2) == ""


def test_rewriting_the_same_result_changes_nothing(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    result = _result("r1", 1, ["x.wav"], 3.333, datetime(2026, 11, 2, 9, 30, 15))
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[result], blocks={})
    stamp = path.stat().st_mtime_ns
    again = write_results_openpyxl(path, layout=layout, plan=plan, results=[result], blocks=first.blocks)
    assert again.written == [] and again.unchanged == 1
    assert path.stat().st_mtime_ns == stamp  # not even saved


def test_split_files_of_several_windows_are_grouped_in_one_cell():
    tasks = [
        {"task_key": "k1", "status": "confirmed", "files": [{"name": "iq_20261102_164407_0001.wav"}, {"name": "iq_20261102_164407_0000.wav"}],
         "summary": {"held_s": 5.004, "stem": "iq_20261102_164407"}},
        {"task_key": "k2", "status": "confirmed", "files": [{"name": "iq_20261102_164420_0000.wav"}],
         "summary": {"held_s": 5.0, "stem": "iq_20261102_164420"}},
    ]
    result = build_row_result("r1", 4, tasks, sensor="CRFS 100-18")
    assert result.files == ["iq_20261102_164407_0000.wav", "iq_20261102_164407_0001.wav", "iq_20261102_164420_0000.wav"]
    assert result.duration_s == pytest.approx(10.004)
    assert result.recorded_at == capture_start_from_stem("iq_20261102_164407")
    assert result.complete is True and result.task_keys == ["k1", "k2"]
    partial = build_row_result("r1", 4, tasks + [{"task_key": "k3", "status": "failed"}], sensor="CRFS 100-18")
    assert partial.complete is False and partial.duration_s == pytest.approx(10.004)


def test_row_result_writes_only_what_the_files_say():
    """Measured duration or nothing; the start from the files' metadata or name, never the request time."""
    unmeasured = [
        {"task_key": "k1", "status": "confirmed", "files": [{"name": "iq_20261103_171321_0001.wav"}],
         "summary": {"held_s": None, "requested_s": 2.0, "stem": "iq_20261103_171321", "started_at": "2026-11-03T19:13:00"}},
    ]
    result = build_row_result("r1", 0, unmeasured, sensor="CRFS 100-18")
    assert result.duration_s is None, "the requested 2 s is never written as the duration"
    assert result.recorded_at == capture_start_from_stem("iq_20261103_171321")
    # One of two recordings unmeasured: the row's total is unknown, not the measured half.
    mixed = unmeasured + [{"task_key": "k2", "status": "confirmed", "files": [{"name": "iq_20261103_171405_0001.wav"}],
                           "summary": {"held_s": 1.98, "stem": "iq_20261103_171405"}}]
    assert build_row_result("r1", 0, mixed, sensor="CRFS 100-18").duration_s is None
    # No time in the files and none in the name: unknown, not the moment the request was sent.
    nameless = [{"task_key": "k1", "status": "confirmed", "files": [{"name": "capture.wav"}],
                 "summary": {"held_s": 1.5, "stem": "capture", "started_at": "2026-11-03T19:13:00"},
                 "started_utc": "2026-11-03T17:13:00+00:00"}]
    blind = build_row_result("r1", 0, nameless, sensor="CRFS 100-18")
    assert blind.recorded_at is None and blind.duration_s == pytest.approx(1.5)
    # The CRFS metadata time wins over the file name (both UTC, shown as local time).
    nameless[0]["files"][0]["signal_time"] = "2026-11-03T17:13:21.250000+00:00"
    stamped = build_row_result("r1", 0, nameless, sensor="CRFS 100-18")
    expected = datetime(2026, 11, 3, 17, 13, 21, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert stamped.recorded_at == expected


def test_capture_start_is_local_time_from_the_utc_stem():
    local = capture_start_from_stem("iq_20261102_172917")
    assert local is not None and local.tzinfo is None
    utc = datetime(2026, 11, 2, 17, 29, 17)
    offset = datetime.now().astimezone().utcoffset()
    assert abs((local - utc) - (datetime(2026, 11, 2, 17, 29, 17).astimezone().utcoffset() or offset)).total_seconds() <= 3600 * 2
    assert capture_start_from_stem("nothing") is None


# ----------------------------------------------------------------------------- queue, backup, flush
def test_queue_flushes_after_a_restart_without_duplicates(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue_file = tmp_path / "state" / "workbook_queue.json"
    queue = WorkbookQueue(queue_file)
    t = datetime(2026, 11, 2, 10, 0, 0)
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, t))
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav", "a0b.wav"], 4.0, t))  # newer result replaces
    assert queue.pending_count() == 1
    # Restart: a new queue object reads the same file.
    restarted = WorkbookQueue(queue_file)
    assert restarted.pending_count() == 1
    report = flush_queue(restarted, excel_open=lambda p: False)
    assert report.ok and report.written == 1 and report.pending == 0
    assert len(report.backups) == 1 and Path(report.backups[0]).is_file()
    assert ".crfs-backup-" in Path(report.backups[0]).name
    assert _values(path)[1][6] == "a0.wav\na0b.wav"
    # Flushing the same result again (e.g. re-queued after a crash) writes nothing new.
    restarted.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav", "a0b.wav"], 4.0, t))
    second = flush_queue(restarted, excel_open=lambda p: False)
    assert second.written == 0 and second.unchanged == 1 and second.pending == 0
    assert len(list(tmp_path.glob("*.crfs-backup-*"))) == 1  # one backup per collection
    assert restarted.block_for_round(path, "r1") == BlockRef("r1", 0, 2)
    # The next collection (same second is possible in tests and in quick restarts) gets its own copy.
    restarted.forget_backup(path)
    second_backup = ensure_backup(restarted, path)
    assert second_backup is not None and second_backup.is_file() and second_backup != Path(report.backups[0])
    assert len(list(tmp_path.glob("*.crfs-backup-*"))) == 2


def test_queue_remembers_the_workbook_that_received_the_rows(tmp_path: Path):
    """'Open written Excel file' opens the file the rows went into, even after another workbook is chosen."""
    first = make_workbook(tmp_path / "first.xlsx")
    second = make_workbook(tmp_path / "second.xlsx")
    imported = _import(first)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue_file = tmp_path / "state" / "workbook_queue.json"
    queue = WorkbookQueue(queue_file)
    assert queue.written_workbook() == "", "nothing written yet"
    queue.enqueue(first, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 11, 2, 10, 0, 0)))
    assert queue.written_workbook() == "", "queued is not written"
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.written == 1 and report.workbooks == [str(first)]
    assert queue.written_workbook() == str(first)
    # The operator picks another protocol workbook; results of a later collection go there.
    queue.enqueue(second, layout_from_import(_import(second)), plan, _result("r2", 0, ["b0.wav"], 2.0, datetime(2026, 11, 3, 10, 0, 0)))
    flush_queue(queue, excel_open=lambda p: False)
    assert queue.written_workbook() == str(second) and queue.written_workbooks() == [str(second), str(first)]
    # Survives a restart (read from the persistent queue).
    assert WorkbookQueue(queue_file).written_workbook() == str(second)


def test_failed_backup_keeps_the_results_queued(tmp_path: Path, monkeypatch):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))

    def broken(src, dest):
        raise OSError("disk full")

    monkeypatch.setattr("crfs_iq_recorder.cellular_workbook.backup_workbook", broken)
    report = flush_queue(queue, excel_open=lambda p: False)
    assert not report.ok and "Backup" in report.error and report.pending == 1
    assert _values(path)[1][6] is None
    with pytest.raises(BackupError):
        ensure_backup(queue, path)
    assert queue.pending_items()[0].attempts == 1


def test_locked_workbook_stays_pending_as_busy(tmp_path: Path, monkeypatch):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))

    def locked(*args, **kwargs):
        raise ExcelBusyError("Could not save p.xlsx: in use")

    monkeypatch.setattr("crfs_iq_recorder.cellular_workbook.write_results_openpyxl", locked)
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.ok and report.busy and report.pending == 1 and report.written == 0


@pytest.mark.skipif(os.name != "nt", reason="byte-range locks: Windows")
def test_a_file_lock_held_by_another_program_is_busy_not_a_backup_failure(tmp_path: Path):
    """Hardware session 3 (100-18): another process held a lock on the workbook, the backup copy
    failed with WinError 33 and the queue stopped as 'error' — nothing was retried until the
    app was restarted. A locked file is a busy state: retried, written once the lock goes."""
    import msvcrt

    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))
    handle = open(path, "r+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        report = flush_queue(queue, excel_open=lambda p: False)
        assert report.ok and report.busy and not report.error and report.pending == 1 and report.written == 0
        assert "backup copy" in report.busy and list(tmp_path.glob("*.crfs-backup-*")) == []
        assert queue.pending_items()[0].attempts == 0, "busy is not a failed attempt"
    finally:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.ok and report.written == 1 and report.pending == 0 and len(report.backups) == 1
    assert _values(path)[1][6] == "a0.wav"


def test_a_share_deny_write_handle_is_busy_and_the_log_text_names_no_temp_file(tmp_path: Path):
    """Hardware (3 Nov, 100-18): another program held the workbook open for reading with write
    sharing denied. The backup copy succeeds, saving fails at os.replace with access denied
    (WinError 5) -> busy, retried, and the log says so in one sentence instead of printing the
    temp-file path. Written once the handle is gone, without duplicates."""
    import win32con
    import win32file

    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))
    handle = win32file.CreateFile(str(path), win32con.GENERIC_READ | win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING, 0, None)
    try:
        report = flush_queue(queue, excel_open=lambda p: False)
        assert report.ok and report.busy and not report.error and report.pending == 1 and report.written == 0
        assert len(report.backups) == 1, "the backup copy only needs read access"
        assert "another program has the file open" in report.busy
        assert "crfs-tmp" not in report.busy and "WinError" not in report.busy and str(tmp_path) not in report.busy
        assert list(tmp_path.glob("*.crfs-tmp-*")) == [], "no temp file left behind"
    finally:
        handle.Close()
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.ok and report.written == 1 and report.pending == 0
    assert _values(path)[1][6] == "a0.wav"
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.written == 0 and _values(path)[1][6] == "a0.wav", "nothing is written twice"


def test_open_workbook_goes_through_excel(tmp_path: Path, monkeypatch):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))
    calls: list[str] = []

    def fake_com(file, **kwargs):
        calls.append("com")
        return write_results_openpyxl(file, **kwargs)

    monkeypatch.setattr("crfs_iq_recorder.cellular_workbook.write_results_com", fake_com)
    report = flush_queue(queue, excel_open=lambda p: True)
    assert calls == ["com"] and report.written == 1 and any("open Excel" in n for n in report.notes)


def test_user_protocol_copy_keeps_its_table_image_and_validations(tmp_path: Path):
    """On a copy of the real protocol workbook (when present on this PC)."""
    import os
    import shutil

    source = Path(os.environ.get("TEMP", "")) / "crfs_probe" / "proto_copy.xlsx"
    if not source.is_file():
        pytest.skip("no local copy of the protocol workbook")
    path = Path(shutil.copy2(source, tmp_path / "copy.xlsx"))
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    assert len(plan) == 15 and layout.last_column == 9
    before = _values(path)
    had_results = any(row[6] not in (None, "") for row in before[1:16])
    t = datetime(2026, 11, 2, 19, 5, 3)
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", i, [f"iq_20261102_1705{i:02d}_0000.wav"], 5.002, t) for i in range(15)], blocks={})
    assert len(first.written) == 15
    # The user's rows already hold the earlier results: they are kept and a block is appended.
    assert first.blocks["r1"].ordinal == (1 if had_results else 0)
    first_row = first.blocks["r1"].first_row
    second = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r2", 0, ["iq_20261102_190000_0000.wav"], 5.0, t)], blocks=first.blocks)
    assert second.blocks["r2"].first_row == (first_row + 15 if had_results else 17)
    after = _values(path)
    assert after[1:16][0][:6] == before[1:16][0][:6]
    if had_results:
        assert [row[6:] for row in after[1:16]] == [row[6:] for row in before[1:16]]
    wb = load_workbook(path)
    ws = wb["Cellular Protocol"]
    last = second.blocks["r2"].first_row + 14
    assert dict(ws.tables.items()) == {"CellularProtocol": f"A1:I{last}"}
    assert len(ws.data_validations.dataValidation) == 3
    assert ws.freeze_panes == "A2"
    assert ws.cell(row=first_row, column=8).value == pytest.approx(5.002) and ws.cell(row=first_row, column=9).value == t
    assert ws.cell(row=first_row + 14, column=7).value == "iq_20261102_170514_0000.wav"  # n78 row written
    assert ws.cell(row=first_row, column=4).value == 753 and ws.cell(row=first_row, column=4).number_format == '0" MHz"'
    assert len(getattr(wb["Reference"], "_images", [])) == 1
    assert "Round 2026-11-02 18-44" in wb.sheetnames  # historical sheet untouched
    wb.close()


def test_missing_workbook_is_reported_and_kept(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))
    path.unlink()
    report = flush_queue(queue, excel_open=lambda p: False)
    assert "not found" in report.error and report.pending == 1
