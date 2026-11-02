"""Results written back into the protocol workbook: one sheet per round, protocol sheet untouched.

Synthetic workbooks only; the runner tests use the scripted host from test_cellular_runner.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

from crfs_iq_recorder.cellular_capabilities import capability_for
from crfs_iq_recorder.cellular_plan import build_plan
from crfs_iq_recorder.cellular_protocol import import_protocol
from crfs_iq_recorder.cellular_protocol_results import (
    DATETIME_FORMAT,
    ResultRow,
    local_naive,
    result_rows,
    round_sheet_name,
    sensor_column,
    sensor_text,
    sync_round_sheet,
    task_status_text,
    window_text,
)
from crfs_iq_recorder.excel_writer import ExcelBusyError

MHZ = 1_000_000
HEADERS = ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)", "Sensor", "IQ Recording", "IQ Duration (s)", "Recording Date & Time"]
ROWS = [
    ["700 MHz (B68 / B28)", "DL", "FDD", 753, 803, "CRFS 100-18", None, None, None],
    ["700 MHz (B68 / B28)", "UL", "FDD", 698, 748, "CRFS 100-18", None, None, None],
    ["1800 MHz (B3)", "DL", "FDD", 1805, 1880, "CRFS 100-18", None, None, None],
    ["3500 MHz (n78)", "UL/DL", "TDD", 3500, 3550, "CRFS 100-18", None, None, None],
]


def _protocol_book(path: Path) -> Path:
    """A workbook shaped like the collection protocol: styled header, table, validations, a second sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Cellular Protocol"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in ROWS:
        ws.append(row)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["G"].width = 40
    ws.freeze_panes = "A2"
    table = Table(displayName="CellularProtocol", ref=f"A1:I{len(ROWS) + 1}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(table)
    dv = DataValidation(type="list", formula1='"DL,UL,UL/DL"')
    dv.add(f"B2:B{len(ROWS) + 1}")
    ws.add_data_validation(dv)
    ref = wb.create_sheet("Reference")
    ref.append(["Rules"])
    ref.append(["Save each round separately; do not overwrite previous results."])
    wb.save(path)
    return path


def _sheet_values(path: Path, title: str) -> list[list]:
    wb = load_workbook(path)
    try:
        return [list(row) for row in wb[title].iter_rows(values_only=True)]
    finally:
        wb.close()


def test_round_sheet_names_follow_the_scheduled_time_and_never_collide():
    when = datetime(2026, 11, 2, 19, 0)
    assert round_sheet_name(when) == "Round 2026-11-02 19-00"
    assert round_sheet_name(when, ["Round 2026-11-02 19-00"]) == "Round 2026-11-02 19-00 (2)"
    assert round_sheet_name(when, ["Round 2026-11-02 19-00", "round 2026-11-02 19-00 (2)"]) == "Round 2026-11-02 19-00 (3)"
    assert len(round_sheet_name(when, [])) <= 31


def test_sensor_column_and_time_helpers():
    from crfs_iq_recorder.cellular_protocol import suggest_mapping

    mapping = suggest_mapping(HEADERS)
    assert sensor_column(HEADERS, mapping) == "Sensor"
    assert sensor_column(["Band", "Start (MHz)", "Stop (MHz)"]) is None
    assert sensor_text({"model": "R40-8", "serial": "rfeye300540"}) == "R40-8 (rfeye300540)"
    assert sensor_text({"model": "R40-8"}) == "R40-8" and sensor_text({}) == ""
    aware = datetime(2026, 11, 2, 17, 5, 9, 500, tzinfo=timezone.utc)
    local = local_naive(aware)
    assert local.tzinfo is None and local.microsecond == 0
    assert local == aware.astimezone().replace(tzinfo=None, microsecond=0)
    assert local_naive("2026-11-02T19:05:09") == datetime(2026, 11, 2, 19, 5, 9)
    assert local_naive("") is None and local_naive("not a date") is None


def test_status_and_window_texts_say_exactly_what_happened():
    assert task_status_text({"status": "queued"}, round_finished=False) == "queued"
    assert task_status_text({"status": "queued"}, round_finished=True) == "not recorded"
    assert task_status_text({"status": "skipped"}, round_finished=True) == "skipped by the operator"
    assert task_status_text({"status": "unknown"}, round_finished=True).startswith("uncertain")
    files = [{"name": "a_0001.wav", "download_status": "verified", "sha256": "ab"}, {"name": "a_0002.wav", "download_status": "verified", "sha256": "cd"}]
    text = task_status_text({"status": "confirmed", "files": files, "quality": {"level": "ok"}}, round_finished=True)
    assert text == "confirmed; downloaded, SHA-256 verified; files checked OK"
    text = task_status_text({"status": "confirmed", "short": True, "files": [{"name": "a.wav", "download_status": "failed"}]}, round_finished=True)
    assert text.startswith("SHORT capture confirmed; download failed for a.wav")
    text = task_status_text({"status": "confirmed", "files": [{"name": "a.wav", "download_status": "not_requested"}]}, round_finished=True)
    assert "download not requested" in text
    task = {"window_start_hz": 1825 * MHZ, "window_stop_hz": 1845 * MHZ, "requested_start_hz": 1805 * MHZ, "requested_stop_hz": 1880 * MHZ,
            "window_index": 2, "window_count": 4, "overlap_prev_hz": 0}
    assert window_text(task) == "1825–1845 MHz: window 2 of 4 of the allocation 1805–1880 MHz (sequential coverage, partial capture)"
    task.update(window_index=4, window_start_hz=1860 * MHZ, window_stop_hz=1880 * MHZ, overlap_prev_hz=5 * MHZ)
    assert window_text(task).endswith("overlaps the previous window by 5 MHz")
    assert window_text({"window_start_hz": 753 * MHZ, "window_stop_hz": 773 * MHZ, "window_count": 1}) == "753–773 MHz (whole allocation)"


def test_round_sheet_is_created_from_the_protocol_and_the_protocol_sheet_is_untouched(tmp_path):
    path = _protocol_book(tmp_path / "protocol.xlsx")
    before = _sheet_values(path, "Cellular Protocol")
    imported = import_protocol(path, "Cellular Protocol")
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=2.0)
    assert plan.ready and len(plan.tasks) == 3 + 3 + 4 + 3  # 50, 50, 75 and 50 MHz in 20 MHz windows
    tasks = [t.to_dict() for t in plan.tasks]
    first = tasks[0]
    states = {
        first["task_key"]: {
            "status": "confirmed",
            "short": False,
            "summary": {"held_s": 2.0, "started_at": "2026-11-02T19:00:05"},
            "files": [
                {"name": "rec_0002.wav", "download_status": "verified", "sha256": "ff"},
                {"name": "rec_0001.wav", "download_status": "verified", "sha256": "ee"},
            ],
            "quality": {"level": "ok"},
            "notes": [],
        },
        tasks[1]["task_key"]: {"status": "unknown", "summary": {}, "files": [], "notes": ["Uncertain: no file settled"], "started_utc": "2026-11-02T17:00:30+00:00"},
    }
    rows = result_rows(tasks, states, round_id="Camp_20261102_1900", round_finished=False, sensor_text="R40-8 (rfeye300540)")
    assert len(rows) == len(tasks)
    assert rows[0].files == ("rec_0002.wav", "rec_0001.wav") and rows[0].duration_s == 2.0 and rows[0].sensor == "R40-8 (rfeye300540)"
    assert rows[0].recorded_at == datetime(2026, 11, 2, 19, 0, 5)
    assert rows[1].files == () and rows[1].duration_s is None and rows[1].sensor == "" and rows[1].recorded_at is not None
    assert rows[2].status == "queued" and rows[2].recorded_at is None

    result = sync_round_sheet(
        path,
        protocol_sheet="Cellular Protocol",
        header_row=imported.header_row,
        mapping=imported.mapping,
        sheet_name=None,
        rows=rows,
        scheduled_local=datetime(2026, 11, 2, 19, 0),
        make_backup=True,
    )
    assert result.sheet == "Round 2026-11-02 19-00" and result.created and result.rows == len(tasks)
    assert Path(result.backup).is_file() and ".crfs-backup-" in Path(result.backup).name
    assert result.warnings == []

    # Protocol sheet: byte-for-byte the same values; table, validation and the second sheet survive.
    assert _sheet_values(path, "Cellular Protocol") == before
    wb = load_workbook(path)
    try:
        assert wb.sheetnames == ["Cellular Protocol", "Reference", "Round 2026-11-02 19-00"]
        assert list(wb["Cellular Protocol"].tables.keys()) == ["CellularProtocol"]
        assert len(wb["Cellular Protocol"].data_validations.dataValidation) == 1
        ws = wb["Round 2026-11-02 19-00"]
        header = [c.value for c in ws[1]]
        assert header == HEADERS + ["Window", "Status", "Notes", "Round"]
        assert ws["A1"].font.bold and ws["A1"].fill.fgColor.rgb.endswith("1F4E78")
        assert ws.column_dimensions["A"].width == 26 and ws.freeze_panes == "A2"
        # Row 2: first window of 700 DL, recorded.
        assert ws["A2"].value == "700 MHz (B68 / B28)" and ws["B2"].value == "DL" and ws["C2"].value == "FDD"
        assert (ws["D2"].value, ws["E2"].value) == (753, 773), "the window actually recorded, not the whole allocation"
        assert ws["F2"].value == "R40-8 (rfeye300540)", "the sensor that recorded replaces the planned one"
        assert ws["G2"].value == "rec_0001.wav\nrec_0002.wav" and ws["G2"].alignment.wrap_text
        assert ws["H2"].value == 2.0
        assert ws["I2"].value == datetime(2026, 11, 2, 19, 0, 5) and ws["I2"].number_format == DATETIME_FORMAT
        assert ws["J2"].value.startswith("753–773 MHz: window 1 of 3 of the allocation 753–803 MHz")
        assert ws["K2"].value == "confirmed; downloaded, SHA-256 verified; files checked OK"
        assert ws["M2"].value == "Camp_20261102_1900"
        # Row 3: uncertain — nothing invented, the planned sensor text stays, cells empty.
        assert (ws["D3"].value, ws["E3"].value) == (773, 793) and ws["F3"].value == "CRFS 100-18"
        assert ws["G3"].value is None and ws["H3"].value is None
        assert ws["K3"].value.startswith("uncertain") and "no file settled" in ws["L3"].value
        # Row 4: queued.
        assert ws["G4"].value is None and ws["I4"].value is None and ws["K4"].value == "queued"
        # The last window of the 50 MHz 700 DL allocation ends on the allocation edge.
        assert (ws["D4"].value, ws["E4"].value) == (783, 803)
        # One table per round sheet, covering header + rows.
        assert len(ws.tables) == 1
        table = next(iter(ws.tables.values()))
        assert table.ref == f"A1:M{len(tasks) + 1}" and table.displayName == "Round_2026_11_02_19_00"
    finally:
        wb.close()


def test_rewrite_is_idempotent_and_leaves_earlier_round_sheets_alone(tmp_path):
    path = _protocol_book(tmp_path / "protocol.xlsx")
    imported = import_protocol(path, "Cellular Protocol")
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=2.0)
    tasks = [t.to_dict() for t in plan.tasks]
    common = dict(protocol_sheet="Cellular Protocol", header_row=imported.header_row, mapping=imported.mapping)
    # Round 1: finished, every window recorded.
    states1 = {t["task_key"]: {"status": "confirmed", "summary": {"held_s": 2.0, "started_at": f"2026-11-02T08:00:{i:02d}"},
                               "files": [{"name": f"r1_{i:04d}.wav", "download_status": "verified", "sha256": "aa"}], "notes": []} for i, t in enumerate(tasks)}
    rows1 = result_rows(tasks, states1, round_id="R1", round_finished=True, sensor_text="R40-8 (S1)")
    first = sync_round_sheet(path, sheet_name=None, rows=rows1, scheduled_local=datetime(2026, 11, 2, 8, 0), **common)
    round1 = _sheet_values(path, first.sheet)
    # Round 2: written while running, then rewritten with progress; a (2) name is not needed because the time differs.
    states2 = {tasks[0]["task_key"]: {"status": "confirmed", "summary": {"held_s": 1.9, "started_at": "2026-11-02T12:00:03"},
                                      "files": [{"name": "r2_0001.wav", "download_status": "pending"}], "notes": []}}
    rows2 = result_rows(tasks, states2, round_id="R2", round_finished=False, sensor_text="R40-8 (S1)")
    second = sync_round_sheet(path, sheet_name=None, rows=rows2, scheduled_local=datetime(2026, 11, 2, 12, 0), **common)
    assert second.sheet == "Round 2026-11-02 12-00" and second.created
    states2[tasks[0]["task_key"]]["files"][0].update(download_status="verified", sha256="bb")
    states2[tasks[1]["task_key"]] = {"status": "skipped", "summary": {}, "files": [], "notes": ["Skipped by the operator."]}
    rows2b = result_rows(tasks, states2, round_id="R2", round_finished=True, sensor_text="R40-8 (S1)")
    again = sync_round_sheet(path, sheet_name=second.sheet, rows=rows2b, scheduled_local=datetime(2026, 11, 2, 12, 0), **common)
    assert again.sheet == second.sheet and not again.created
    twice = sync_round_sheet(path, sheet_name=second.sheet, rows=rows2b, scheduled_local=datetime(2026, 11, 2, 12, 0), **common)
    assert not twice.created
    wb = load_workbook(path)
    try:
        assert wb.sheetnames == ["Cellular Protocol", "Reference", first.sheet, second.sheet]
        ws = wb[second.sheet]
        assert ws["K2"].value == "confirmed; downloaded, SHA-256 verified"
        assert ws["K3"].value == "skipped by the operator" and ws["G3"].value is None
        assert ws["K4"].value == "not recorded"
        assert len(wb[second.sheet].tables) == 1 and len(wb[first.sheet].tables) == 1
        names = {t.displayName for sheet in wb.worksheets for t in sheet.tables.values()}
        assert len(names) == 3, "table names stay unique across the workbook"
    finally:
        wb.close()
    assert _sheet_values(path, first.sheet) == round1, "an earlier round's sheet is never rewritten"
    # A same-minute round in a later session gets its own sheet instead of overwriting.
    third = sync_round_sheet(path, sheet_name=None, rows=rows2b, scheduled_local=datetime(2026, 11, 2, 12, 0), **common)
    assert third.sheet == "Round 2026-11-02 12-00 (2)" and third.created
    assert _sheet_values(path, second.sheet) == _sheet_values(path, third.sheet)


def test_locked_workbook_defers_instead_of_failing_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = _protocol_book(tmp_path / "protocol.xlsx")
    imported = import_protocol(path, "Cellular Protocol")
    rows = [ResultRow(protocol_row=2, direction="DL", start_hz=753 * MHZ, stop_hz=773 * MHZ, window="w", status="queued", round_id="R")]
    common = dict(protocol_sheet="Cellular Protocol", header_row=1, mapping=imported.mapping, scheduled_local=datetime(2026, 11, 2, 9, 0))
    # Excel holds the workbook: refused before anything is read.
    monkeypatch.setattr("crfs_iq_recorder.cellular_protocol_results.excel_has_workbook", lambda p: True)
    with pytest.raises(ExcelBusyError, match="open in Excel"):
        sync_round_sheet(path, sheet_name=None, rows=rows, **common)
    monkeypatch.setattr("crfs_iq_recorder.cellular_protocol_results.excel_has_workbook", lambda p: False)
    # The file itself is locked for replacement (another process has it open).
    real_replace = os.replace

    def locked(src, dst):
        raise PermissionError(13, "The process cannot access the file", str(dst))

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(ExcelBusyError, match="Could not save"):
        sync_round_sheet(path, sheet_name=None, rows=rows, **common)
    monkeypatch.setattr(os, "replace", real_replace)
    assert [p.name for p in tmp_path.iterdir() if "crfs-tmp" in p.name] == []
    assert load_workbook(path).sheetnames == ["Cellular Protocol", "Reference"], "a failed save leaves the workbook as it was"
    # Missing protocol sheet is a clear error, not a silent write.
    with pytest.raises(ValueError, match="not in protocol.xlsx"):
        sync_round_sheet(path, sheet_name=None, rows=rows, **{**common, "protocol_sheet": "Nope"})


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _pump(qapp, seconds=0.05):
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def test_runner_fills_the_protocol_workbook_round_by_round_and_defers_while_excel_holds_it(qapp, tmp_path, monkeypatch):
    from test_cellular_runner import FakeHost

    from crfs_iq_recorder.cellular_runner import S_DONE, CampaignSettings, CellularRunner, load_session
    from crfs_iq_recorder.cellular_scheduler import MODE_NOW, Schedule

    path = _protocol_book(tmp_path / "Cellular_IQ_Collection_Protocol.xlsx")
    before = _sheet_values(path, "Cellular Protocol")
    imported = import_protocol(path, "Cellular Protocol")
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=2.0)
    host = FakeHost()
    cfg = tmp_path / "config"
    cfg.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    runner = CellularRunner(host, config_dir=cfg, download_dir=lambda: downloads, clock=lambda: datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc).astimezone())
    log: list[str] = []
    runner.log.connect(lambda text, _level: log.append(text))
    settings = CampaignSettings(
        campaign="Camp", site="Site", wait_s=0.0, auto_download=False, verify_checksums=False,
        protocol_path=str(path), protocol_sheet="Cellular Protocol", protocol_results=True,
        protocol_header_row=imported.header_row, protocol_mapping=imported.mapping.to_dict(),
    )
    # Excel "holds" the workbook when the round starts: the first write is deferred, not lost.
    monkeypatch.setattr("crfs_iq_recorder.cellular_protocol_results.excel_has_workbook", lambda p: True)
    assert runner.start_session(plan, Schedule(mode=MODE_NOW), settings) is None
    record = runner.session.rounds[0]
    assert record.protocol_status == "deferred" and record.protocol_sheet == ""
    assert runner.status()["protocol_status"] == "deferred" and runner.status()["protocol_results"] is True
    assert sum(1 for line in log if "Results sheet deferred" in line) == 1
    assert load_workbook(path).sheetnames == ["Cellular Protocol", "Reference"]
    # Excel closes the workbook; the retry (15 s back-off skipped here) writes the sheet.
    monkeypatch.setattr("crfs_iq_recorder.cellular_protocol_results.excel_has_workbook", lambda p: False)
    runner._protocol_not_before = 0.0
    runner._sync_protocol_results()
    assert record.protocol_status == "saved" and record.protocol_sheet == f"Round {record.scheduled_local:%Y-%m-%d %H-%M}"
    sheet = record.protocol_sheet
    values = _sheet_values(path, sheet)
    assert len(values) == len(plan.tasks) + 1 and values[1][10] == "recording…", values[1]
    backups = [p for p in tmp_path.iterdir() if ".crfs-backup-" in p.name]
    assert len(backups) == 1, "one backup before the first write"
    # Captures finish: confirmed (2 parts), then a short one, then the rest; each rewrite lands on the same sheet.
    host.finish(host.requests[0], parts=2, names=["cap1_0001.wav", "cap1_0002.wav"])
    _pump(qapp)
    runner._protocol_not_before = 0.0
    runner._sync_protocol_results()
    values = _sheet_values(path, sheet)
    assert values[1][6] == "cap1_0001.wav\ncap1_0002.wav" and values[1][7] == 2.0 and values[1][5] == "R40-8 (SN1)"
    assert isinstance(values[1][8], datetime) and values[1][10] == "confirmed; on the sensor (download not requested)"
    assert values[2][10] == "recording…" and values[2][6] is None
    host.finish(host.requests[1], held=0.4)
    _pump(qapp)
    while len(host.requests) < len(plan.tasks):
        host.finish(host.requests[-1])
        _pump(qapp)
    host.finish(host.requests[-1])
    _pump(qapp)
    runner._tick()
    assert runner.state == S_DONE
    runner._protocol_not_before = 0.0
    runner._sync_protocol_results()
    assert runner._protocol_dirty == {} and runner.background_jobs() == 0
    values = _sheet_values(path, sheet)
    assert values[2][7] == 0.4 and values[2][10].startswith("SHORT capture confirmed")
    assert all(row[10].startswith(("confirmed", "SHORT")) for row in values[1:])
    assert load_workbook(path).sheetnames == ["Cellular Protocol", "Reference", sheet]
    assert _sheet_values(path, "Cellular Protocol") == before, "the protocol sheet is never written"
    assert len([p for p in tmp_path.iterdir() if ".crfs-backup-" in p.name]) == 1
    saved = load_session(cfg)
    assert saved.rounds[0].protocol_sheet == sheet and saved.settings.protocol_results is True
    assert saved.settings.protocol_mapping["result"] == "IQ Recording"


def test_paired_layout_writes_the_recorded_direction_only(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Paired"
    ws.append(["Band", "DL start (MHz)", "DL stop (MHz)", "UL start (MHz)", "UL stop (MHz)", "IQ Recording", "Recording Date & Time"])
    ws.append(["B8", 925, 945, 880, 900, None, None])
    path = tmp_path / "paired.xlsx"
    wb.save(path)
    imported = import_protocol(path, "Paired")
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=1.0)
    assert plan.ready and [t.direction for t in plan.tasks] == ["DL", "UL"]
    tasks = [t.to_dict() for t in plan.tasks]
    rows = result_rows(tasks, {}, round_id="R", round_finished=True, sensor_text="")
    result = sync_round_sheet(path, protocol_sheet="Paired", header_row=1, mapping=imported.mapping, sheet_name="Round X", rows=rows)
    assert result.warnings == []
    sheet = _sheet_values(path, "Round X")
    assert sheet[0][:7] == ["Band", "DL start (MHz)", "DL stop (MHz)", "UL start (MHz)", "UL stop (MHz)", "IQ Recording", "Recording Date & Time"]
    assert sheet[1][:5] == ["B8", 925, 945, None, None], "the DL row shows only the DL window"
    assert sheet[2][:5] == ["B8", None, None, 880, 900], "the UL row shows only the UL window"
    assert sheet[1][8] == "not recorded" and sheet[2][8] == "not recorded"
