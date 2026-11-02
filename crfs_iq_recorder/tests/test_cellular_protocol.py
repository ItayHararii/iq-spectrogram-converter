"""Protocol import from synthetic workbooks. No real collection workbook is used."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from crfs_iq_recorder.cellular_protocol import (
    DIR_DL,
    DIR_SHARED,
    DIR_UL,
    DUPLEX_FDD,
    DUPLEX_TDD,
    DUPLICATES_EACH,
    LEVEL_ERROR,
    ColumnMapping,
    ProtocolError,
    import_protocol,
    list_worksheets,
    load_snapshot,
    parse_duration_cell,
    parse_frequency_cell,
    read_headers,
    save_snapshot,
    suggest_mapping,
)

MHZ = 1_000_000


def _book(path: Path, sheets: dict[str, list[list]], *, hidden: tuple[str, ...] = ()) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
        if name in hidden:
            ws.sheet_state = "hidden"
    wb.save(path)
    return path


# Values below describe a synthetic test plan in the layout of the reference
# chart attached to the collection task; they are test data, not a protocol.
ROWS_LAYOUT = [
    ["Band", "Nominal frequency", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)", "Network", "Technology", "Duration (s)", "Notes"],
    ["700", "700 MHz", "FDD", "DL", 758, 768, "Op A", "LTE", 20, ""],
    ["700", "700 MHz", "FDD", "UL", 703, 713, "Op A", "LTE", 20, ""],
    ["1800", "1800 MHz", "FDD", "DL", 1805, 1825, "Op B", "LTE", None, "no duration here"],
    ["1800", "1800 MHz", "FDD", "UL", 1710, 1730, "Op B", "LTE", None, ""],
    ["3500", "3.5 GHz", "TDD", "TDD", 3600, 3700, "Op A", "5G NR", 10, "shared"],
]


def test_suggest_mapping_reads_roles_and_header_units():
    mapping = suggest_mapping(ROWS_LAYOUT[0])
    assert mapping.band == "Band"
    assert mapping.nominal == "Nominal frequency"
    assert mapping.duplex == "Duplex"
    assert mapping.direction == "Direction"
    assert mapping.start == "Start (MHz)"
    assert mapping.stop == "Stop (MHz)"
    assert mapping.header_units == {"start": "MHz", "stop": "MHz"}
    assert mapping.default_unit == "MHz"
    assert mapping.duration == "Duration (s)"
    assert mapping.network == "Network"
    assert mapping.technology == "Technology"
    assert mapping.notes == "Notes"
    assert mapping.layout() == "rows"
    assert mapping.missing() == []


def test_rows_layout_imports_every_allocation_exactly(tmp_path):
    path = _book(tmp_path / "plan.xlsx", {"Plan": ROWS_LAYOUT})
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    entries = imported.valid_entries()
    assert [(e.band, e.direction, e.start_hz, e.stop_hz) for e in entries] == [
        ("700", DIR_DL, 758 * MHZ, 768 * MHZ),
        ("700", DIR_UL, 703 * MHZ, 713 * MHZ),
        ("1800", DIR_DL, 1805 * MHZ, 1825 * MHZ),
        ("1800", DIR_UL, 1710 * MHZ, 1730 * MHZ),
        ("3500", DIR_SHARED, 3600 * MHZ, 3700 * MHZ),
    ]
    assert entries[0].nominal_hz == 700 * MHZ
    assert entries[4].nominal_hz == 3_500 * MHZ, "3.5 GHz band text is 3500 MHz"
    assert entries[4].duplex == DUPLEX_TDD
    assert entries[0].duplex == DUPLEX_FDD
    assert entries[0].duration_s == 20.0
    assert entries[2].duration_s is None, "a missing duration stays missing (campaign fallback applies later)"
    assert entries[0].network == "Op A" and entries[0].technology == "LTE"
    assert entries[0].label() == "700 MHz DL 758–768 MHz"
    assert entries[4].label() == "3500 MHz TDD / shared UL-DL 3600–3700 MHz"
    assert imported.ready
    assert len(imported.protocol_version()) == 12


def _strip_dimension_tags(path: Path) -> None:
    """Rewrite the workbook without the sheets' <dimension> elements, as some writers do."""
    import re
    import shutil
    import zipfile

    tmp = path.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                data = re.sub(rb"<dimension[^>]*/>", b"", data)
                assert b"<dimension" not in data
            dst.writestr(item, data)
    shutil.move(tmp, path)


PROTOCOL_LAYOUT = [
    ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)", "Sensor", "IQ Recording", "IQ Duration (s)", "Recording Date & Time"],
    ["700 MHz (B68 / B28)", "DL", "FDD", 753, 803, "CRFS 100-18", None, None, None],
    ["700 MHz (B68 / B28)", "UL", "FDD", 698, 748, "CRFS 100-18", None, None, None],
    ["1800 MHz (B3)", "DL", "FDD", 1805, 1880, "CRFS 100-18", None, None, None],
    ["1800 MHz (B3)", "UL", "FDD", 1710, 1785, "CRFS 100-18", None, None, None],
    ["3500 MHz (n78)", "UL/DL", "TDD", 3300, 3800, "CRFS 100-18", None, None, None],
]


def test_workbook_without_dimension_tags_imports_every_row_and_band_labels_carry_their_nominal(tmp_path):
    """A protocol written by a tool that omits <dimension> made openpyxl report no rows;
    every allocation was silently dropped. The sheet is now read to its end."""
    path = _book(tmp_path / "protocol.xlsx", {"Cellular Protocol": PROTOCOL_LAYOUT, "Reference": [["Notes"], ["x"]]})
    _strip_dimension_tags(path)
    headers, header_row = read_headers(path, "Cellular Protocol")
    assert header_row == 1 and headers[:5] == ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)"]
    imported = import_protocol(path, "Cellular Protocol")
    assert imported.errors == [] and imported.rows_scanned == 5
    entries = imported.valid_entries()
    assert [(e.direction, e.start_hz // MHZ, e.stop_hz // MHZ) for e in entries] == [
        (DIR_DL, 753, 803), (DIR_UL, 698, 748), (DIR_DL, 1805, 1880), (DIR_UL, 1710, 1785), (DIR_SHARED, 3300, 3800),
    ]
    # The band text keeps its label, and the leading frequency is its nominal (for ordering).
    assert entries[0].band == "700 MHz (B68 / B28)" and entries[0].nominal_hz == 700 * MHZ
    assert entries[2].nominal_hz == 1800 * MHZ and entries[4].nominal_hz == 3500 * MHZ
    assert entries[0].label() == "700 MHz (B68 / B28) DL 753–803 MHz"
    # The result columns are recognised, and empty ones leave the row an instruction. The empty
    # duration column means "campaign duration"; the collection fills it with the held seconds.
    mapping = imported.mapping
    assert mapping.result == "IQ Recording" and mapping.result_time == "Recording Date & Time"
    assert mapping.duration == "IQ Duration (s)" and mapping.extra == ["Sensor"]
    assert mapping.has_result_columns()
    assert mapping.result_columns() == {"result": "IQ Recording", "result_time": "Recording Date & Time", "duration": "IQ Duration (s)"}
    assert all(e.duration_s is None for e in entries)
    assert imported.ready
    # A protocol without result columns says so.
    assert not suggest_mapping(ROWS_LAYOUT[0]).has_result_columns()


def test_units_in_cells_header_and_default_are_all_understood():
    assert parse_frequency_cell("713 MHz", unit_hint=None, field_name="f") == (713 * MHZ, None)
    assert parse_frequency_cell("0.713GHz", unit_hint=None, field_name="f") == (713 * MHZ, None)
    assert parse_frequency_cell("713,000 kHz", unit_hint=None, field_name="f") == (713 * MHZ, None)
    assert parse_frequency_cell("1 805.5 MHz", unit_hint=None, field_name="f") == (1_805_500_000, None)
    assert parse_frequency_cell("902,2", unit_hint="MHz", field_name="f") == (902_200_000, None)
    assert parse_frequency_cell(713, unit_hint="MHz", field_name="f") == (713 * MHZ, None)
    assert parse_frequency_cell(713_000_000, unit_hint="Hz", field_name="f") == (713 * MHZ, None)
    hz, issue = parse_frequency_cell(713, unit_hint=None, field_name="Start")
    assert hz is None and issue.code == "no_unit"
    hz, issue = parse_frequency_cell("713.0000001 MHz", unit_hint=None, field_name="Start")
    assert hz is None and issue.code == "fraction_hz"
    hz, issue = parse_frequency_cell("abc", unit_hint="MHz", field_name="Start")
    assert hz is None and issue.code == "invalid"
    hz, issue = parse_frequency_cell(None, unit_hint="MHz", field_name="Start")
    assert hz is None and issue.code == "missing"


def test_duration_cells():
    assert parse_duration_cell(2) == (2.0, None)
    assert parse_duration_cell("2 s") == (2.0, None)
    assert parse_duration_cell("30 sec") == (30.0, None)
    assert parse_duration_cell("1.5 min") == (90.0, None)
    assert parse_duration_cell("500 ms") == (0.5, None)
    assert parse_duration_cell("0.5 sec") == (0.5, None)
    assert parse_duration_cell(None) == (None, None)
    value, issue = parse_duration_cell("fast")
    assert value is None and issue.code == "invalid_duration"
    value, issue = parse_duration_cell(0)
    assert value is None and issue.code == "invalid_duration"


def test_paired_dl_ul_columns_make_two_entries_dl_first(tmp_path):
    rows = [
        ["Band (MHz)", "Operator", "DL start", "DL stop", "UL start", "UL stop", "Unit"],
        [2100, "Op A", 2150, 2160, 1960, 1970, "MHz"],
        [800, "Op B", 791, 801, 832, 842, "MHz"],
    ]
    path = _book(tmp_path / "paired.xlsx", {"Paired": rows})
    imported = import_protocol(path, "Paired")
    assert imported.errors == []
    entries = imported.valid_entries()
    assert [(e.nominal_hz // MHZ, e.direction, e.start_hz // MHZ, e.stop_hz // MHZ) for e in entries] == [
        (2100, DIR_DL, 2150, 2160),
        (2100, DIR_UL, 1960, 1970),
        (800, DIR_DL, 791, 801),
        (800, DIR_UL, 832, 842),
    ]
    assert all(e.duplex == DUPLEX_FDD for e in entries)
    assert all(any(i.code == "duplex_inferred" for i in e.issues) for e in entries)
    assert entries[0].network == "Op A"
    # "Band (MHz)" is the nominal frequency, so the band label shows it.
    assert entries[0].band_label() == "2100 MHz"


def test_paired_layout_tdd_row_with_equal_ranges_is_one_shared_entry(tmp_path):
    rows = [
        ["Band", "Duplex", "DL start (MHz)", "DL stop (MHz)", "UL start (MHz)", "UL stop (MHz)"],
        ["3500", "TDD", 3500, 3550, 3500, 3550],
        ["3500", "TDD", 3550, 3600, None, None],
        ["3500", "TDD", 3600, 3700, 3650, 3700],
    ]
    path = _book(tmp_path / "tdd.xlsx", {"TDD": rows})
    imported = import_protocol(path, "TDD")
    good = imported.valid_entries()
    assert [(e.direction, e.start_hz // MHZ, e.stop_hz // MHZ) for e in good] == [
        (DIR_SHARED, 3500, 3550),
        (DIR_SHARED, 3550, 3600),
    ]
    bad = imported.invalid_entries()
    assert len(bad) == 1 and any(i.code == "tdd_two_ranges" for i in bad[0].issues)
    assert not imported.ready, "an invalid row blocks execution until it is fixed or removed"


def test_validation_flags_reversed_missing_contradictory_and_unknown_rows(tmp_path):
    rows = [
        ["Band", "Duplex", "Direction", "Start", "Stop", "Unit"],
        ["700", "FDD", "DL", 768, 758, "MHz"],  # reversed
        ["700", "FDD", "UL", None, 713, "MHz"],  # missing start
        ["3500", "TDD", "DL", 3600, 3700, "MHz"],  # TDD marked DL
        ["900", "FDD", "shared", 947.2, 955.2, "MHz"],  # FDD marked shared
        ["900", "FDD", "", 902.2, 910.2, "MHz"],  # no direction
        ["900", "", "", 902.2, 910.2, "MHz"],  # nothing
        ["900", "FDD", "sideways", 902.2, 910.2, "MHz"],  # unknown direction
        ["900", "HDX", "DL", 902.2, 910.2, "MHz"],  # unknown duplex -> warning, read as FDD-by-direction
        ["", "", "DL", 1805, 1825, "MHz"],  # no band
    ]
    path = _book(tmp_path / "bad.xlsx", {"Bad": rows})
    imported = import_protocol(path, "Bad")
    codes = {e.row: {i.code for i in e.issues if i.level == LEVEL_ERROR} for e in imported.entries}
    assert "reversed" in codes[2]
    assert "missing" in codes[3]
    assert "tdd_direction" in codes[4]
    assert "fdd_shared" in codes[5]
    assert "direction_missing" in codes[6]
    assert "direction_missing" in codes[7]
    assert "direction_unknown" in codes[8]
    assert codes[9] == set(), "unknown duplex is a warning; the DL direction decides"
    assert any(i.code == "duplex_unknown" for i in imported.entries[7].issues)
    assert "band_missing" in codes[10]
    assert len(imported.valid_entries()) == 1
    assert len(imported.invalid_entries()) == 8
    assert not imported.ready


def test_duplicates_are_flagged_and_collapsed_unless_asked_otherwise(tmp_path):
    rows = [
        ["Band", "Direction", "Start (MHz)", "Stop (MHz)", "Repeat"],
        ["700", "DL", 758, 768, None],
        ["700", "DL", 758, 768, None],  # duplicated template row
        ["700", "DL", 760, 770, None],  # overlaps row 2
        ["700", "UL", 703, 713, 3],  # intentional repetition
        ["700", "DL", 768, 778, None],  # touches row 2 (no overlap), overlaps row 4 by 2 MHz
    ]
    path = _book(tmp_path / "dups.xlsx", {"P": rows})
    imported = import_protocol(path, "P")
    assert imported.errors == []
    by_row = {e.row: e for e in imported.entries}
    assert by_row[3].duplicate_of == 2
    assert any(i.code == "duplicate" for i in by_row[3].issues)
    assert any(i.code == "overlap" and "row 2" in i.message for i in by_row[4].issues)
    overlaps_6 = [i.message for i in by_row[6].issues if i.code == "overlap"]
    assert len(overlaps_6) == 1 and "row 4" in overlaps_6[0] and "row 2" not in overlaps_6[0]
    assert by_row[5].repeat == 3
    assert [e.row for e in imported.valid_entries()] == [2, 4, 5, 6]
    each = import_protocol(path, "P", duplicate_policy=DUPLICATES_EACH)
    assert [e.row for e in each.valid_entries()] == [2, 3, 4, 5, 6]
    assert imported.protocol_version() != each.protocol_version()


def test_rows_with_results_are_listed_but_never_imported_as_instructions(tmp_path):
    rows = [
        ["Band", "Frequency", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)", "IQ Recording", "Status"],
        ["8", "900 MHz", "FDD", "DL", 947.2, 955.2, "file_114955_0001.wav", "Collected"],
        ["8", "900 MHz", "FDD", "DL", 947.2, 955.2, None, "Collected"],
        ["8", "900 MHz", "FDD", "UL", 902.2, 910.2, None, "Planned"],
        ["8", "900 MHz", "FDD", "UL", 910.2, 912.6, None, None],
    ]
    path = _book(tmp_path / "results.xlsx", {"Plan": rows})
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    assert [e.row for e in imported.result_rows()] == [2, 3]
    assert [e.row for e in imported.valid_entries()] == [4, 5]
    assert imported.valid_entries()[0].band_label() == "Band 8 (900 MHz)"
    assert imported.valid_entries()[0].nominal_hz == 900 * MHZ


def test_master_sheet_without_allocation_edges_is_refused_with_a_clear_reason(tmp_path):
    # Layout of a collection log: band number and nominal frequency only.
    rows = [
        ["Record ID", "RAN", "Technology", "Band", "Frequency", "Duplex", "Direction", "Sensor", "IQ Recording", "IQ Duration", "Status"],
        ["CELL-0001", "PHI", "GSM", 8, "900 MHz", "FDD", "DL", "CRFS", None, "0.5 sec", "Planned"],
    ]
    path = _book(tmp_path / "master.xlsx", {"Cellular Master": rows})
    headers, header_row = read_headers(path, "Cellular Master")
    assert header_row == 1 and headers[3] == "Band"
    mapping = suggest_mapping(headers)
    assert mapping.layout() == "incomplete"
    assert mapping.nominal == "Frequency" and mapping.band == "Band"
    assert mapping.network == "RAN" and mapping.duration == "IQ Duration" and mapping.status == "Status"
    imported = import_protocol(path, "Cellular Master")
    assert imported.entries == []
    messages = " ".join(i.message for i in imported.errors)
    assert "start/stop frequency columns" in messages
    assert "never used as a centre frequency" in messages
    assert not imported.ready


def test_header_row_is_found_below_a_title_and_blank_rows_are_counted(tmp_path):
    rows = [
        ["Cellular collection plan - site X"],
        [],
        ["Band", "Direction", "Start", "Stop"],
        ["700 MHz", "DL", "758 MHz", "768 MHz"],
        [],
        ["700 MHz", "UL", "703 MHz", "713 MHz"],
    ]
    path = _book(tmp_path / "title.xlsx", {"Plan": rows}, hidden=())
    headers, header_row = read_headers(path, "Plan")
    assert header_row == 3 and headers == ["Band", "Direction", "Start", "Stop"]
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    assert [e.row for e in imported.valid_entries()] == [4, 6]
    assert imported.blank_rows == 1
    assert imported.valid_entries()[0].nominal_hz == 700 * MHZ


def test_hidden_sheets_are_listed_and_import_warns(tmp_path):
    path = _book(tmp_path / "hidden.xlsx", {"Plan": ROWS_LAYOUT, "Old": ROWS_LAYOUT}, hidden=("Old",))
    assert list_worksheets(path) == [("Plan", True), ("Old", False)]
    imported = import_protocol(path, "Old")
    assert any(i.code == "hidden_sheet" for i in imported.warnings)
    assert imported.ready


def test_explicit_mapping_and_default_unit(tmp_path):
    rows = [
        ["Band", "Side", "Low", "High"],
        ["1800", "Downlink", 1805, 1825],
        ["1800", "Uplink", 1710, 1730],
    ]
    path = _book(tmp_path / "custom.xlsx", {"S": rows})
    auto = import_protocol(path, "S")
    assert auto.mapping.layout() == "rows"
    assert any(i.code == "no_unit" for i in auto.errors), "bare numbers need a unit"
    mapping = ColumnMapping(band="Band", direction="Side", start="Low", stop="High", default_unit="MHz")
    imported = import_protocol(path, "S", mapping)
    assert imported.errors == []
    assert [(e.direction, e.start_hz // MHZ) for e in imported.valid_entries()] == [(DIR_DL, 1805), (DIR_UL, 1710)]
    broken = ColumnMapping(band="Band", direction="Side", start="Low", stop="Nope", default_unit="MHz")
    missing = import_protocol(path, "S", broken)
    assert any(i.code == "column_missing" for i in missing.errors)


def test_snapshot_round_trip_and_version(tmp_path):
    path = _book(tmp_path / "plan.xlsx", {"Plan": ROWS_LAYOUT})
    imported = import_protocol(path, "Plan")
    saved = save_snapshot(imported, tmp_path / "config")
    assert saved.parent == tmp_path / "config" / "cellular" / "protocols"
    loaded = load_snapshot(saved)
    assert loaded.protocol_version() == imported.protocol_version()
    assert [e.to_dict() for e in loaded.entries] == [e.to_dict() for e in imported.entries]
    assert loaded.mapping.to_dict() == imported.mapping.to_dict()
    assert loaded.workbook_sha256 == imported.workbook_sha256
    # Editing the workbook changes the version; the saved snapshot does not change.
    wb_rows = [list(r) for r in ROWS_LAYOUT]
    wb_rows[1][4] = 757
    _book(path, {"Plan": wb_rows})
    again = import_protocol(path, "Plan")
    assert again.protocol_version() != imported.protocol_version()
    assert load_snapshot(saved).protocol_version() == imported.protocol_version()


def test_missing_sheet_and_file_raise_protocol_error(tmp_path):
    path = _book(tmp_path / "plan.xlsx", {"Plan": ROWS_LAYOUT})
    with pytest.raises(ProtocolError):
        import_protocol(path, "Nope")
    with pytest.raises(ProtocolError):
        list_worksheets(tmp_path / "absent.xlsx")
