"""Quality checks, manifests/handover, Excel results rows and coverage summaries."""

from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from crfs_iq_recorder import cellular_quality as cq
from crfs_iq_recorder.cellular_excel import (
    RESULTS_SHEET,
    class_text,
    collection_event_text,
    ensure_results_sheet,
    queue_capture_rows,
    worksheet_for_task,
)
from crfs_iq_recorder.cellular_manifest import (
    build_capture_manifest,
    build_handover,
    handover_markdown,
    manifest_path,
    read_manifest,
    write_manifest,
)
from crfs_iq_recorder.cellular_plan import CellularPlan
from crfs_iq_recorder.collection_catalog import ClassMapping, load_workbook_catalog
from crfs_iq_recorder.excel_log import ExcelLogStore, flush_excel_queue
from crfs_iq_recorder.excel_writer import ExcelBusyError
from crfs_iq_recorder.frequency import freq_text_style
from crfs_iq_recorder.recording_history import split_iq_filenames
from crfs_iq_recorder.sensor_storage import StorageSnapshot

from test_iq_merge import FS, T0, make_recording, write_part

MHZ = 1_000_000


# ----------------------------------------------------------------------------- file validation


def test_validate_capture_clean_parts_is_ok_and_measures_duration(tmp_path):
    paths, _ = make_recording(tmp_path, (400, 400, 200))
    report = cq.validate_capture(paths, expected_duration_s=1.0, expected_center_hz=511_000_000, expected_bandwidth_hz=22_000_000, expected_sample_rate_hz=FS)
    # The synthetic payload is a biased ramp, so only the DC-offset warning may appear.
    assert report.errors == [] and set(report.flags) <= {"dc_offset"}, (report.errors, report.warnings)
    assert report.level in {cq.LEVEL_OK, cq.LEVEL_WARNING}
    assert report.actual_duration_s == pytest.approx(1.0)
    assert [f.part for f in report.files] == [1, 2, 3]
    assert all(f.readable and f.blocks for f in report.files)
    assert report.files[0].blocks[0].position == "whole"
    data = report.to_dict()
    assert data["level"] == report.level and data["files"][0]["sample_rate_hz"] == FS


def test_validate_capture_flags_truncation_gap_missing_part_and_tuning(tmp_path):
    paths, _ = make_recording(tmp_path, (300, 300, 300), gap_ms=50.0)
    write_part(tmp_path / "file_063929_0003.wav", frames=300, start=T0 + timedelta(seconds=0.65), seed=3, truncate_by=40)
    paths.append(tmp_path / "file_063929_0005.wav")
    write_part(paths[-1], frames=300, start=T0 + timedelta(seconds=1.3), seed=5, rf=5.3e8)
    report = cq.validate_capture(paths, expected_duration_s=5.0, expected_center_hz=511_000_000, expected_parts=["file_063929_0001.wav", "file_063929_0006.wav"])
    assert report.level == cq.LEVEL_ERROR
    assert "truncated" in report.flags
    assert "missing_part" in report.flags and any("0004" in e for e in report.errors)
    assert any("file_063929_0006.wav" in e for e in report.errors)
    assert "time_gap" in report.flags and any("50.0 ms gap" in w for w in report.warnings)
    assert "tuning_mismatch" in report.flags and any("530.000000 MHz" in e for e in report.errors)
    assert "short_duration" in report.flags


def test_validate_file_detects_empty_corrupt_clipping_zero_and_level_change(tmp_path):
    empty = tmp_path / "file_1_0001.wav"
    empty.write_bytes(b"")
    assert cq.validate_file(empty).flags == ["empty"]
    junk = tmp_path / "file_2_0001.wav"
    junk.write_bytes(b"not a wave file at all, just text" * 4)
    assert cq.validate_file(junk).flags == ["corrupt"]

    # Hand-built samples: clipped start, silent middle, quiet end.
    frames = 3 * cq.BLOCK_FRAMES // 8
    block = cq.BLOCK_FRAMES // 8
    values = []
    for n in range(frames):
        if n < block:
            i = q = 32767 if n % 2 else -32768
        elif n < 2 * block:
            i = q = 0
        else:
            i, q = (n % 7) - 3, (n % 5) - 2
        values.extend((i, q))
    data = struct.pack(f"<{len(values)}h", *values)
    fmt = struct.pack("<HHIIHH", 1, 2, 1000, 4000, 4, 16)
    blob = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path = tmp_path / "file_3_0001.wav"
    path.write_bytes(blob)
    result = cq.validate_file(path, block_frames=block)
    positions = [b.position for b in result.blocks]
    assert positions == ["begin", "middle", "end"]
    assert "clipping:begin" in result.flags
    assert "zero_samples:middle" in result.flags
    assert "level_change" in result.flags
    assert result.blocks[0].clip_ratio > 0.99 and result.blocks[1].zero_ratio == 1.0


def test_non_wave_recordings_are_not_called_corrupt_and_are_reported_as_not_checked(tmp_path):
    # The sensor writes XDAT when the recorder's Format box says so (seen on hardware):
    # a .xdat payload plus .xhdr/.xmrk sidecars. Without the .xhdr (the sensor
    # writes it last) the samples cannot be read, so nothing is claimed about them.
    xdat = tmp_path / "file_135548_0001.xdat"
    xdat.write_bytes(b"\x01\x02" * 4096)
    xmrk = tmp_path / "file_135548_0001.xmrk"
    xmrk.write_bytes(b"<xmrk/>" * 16)
    report = cq.validate_capture([xdat, xmrk], expected_duration_s=2.0, expected_center_hz=763_000_000, expected_parts=[xdat.name, xmrk.name])
    assert report.level == cq.LEVEL_NOT_CHECKED
    assert report.errors == [] and report.warnings == []
    assert report.flags == ["not_wave"]
    assert any("XDAT" in n and "WAVE and XDAT (with .xhdr) recordings only" in n for n in report.notes)
    assert any("Kept with the recording" in n and xmrk.name in n for n in report.notes), "the marker file is listed, not checked"
    assert [f.container for f in report.files] == ["XDAT"] and not report.files[0].readable
    assert any(".xhdr" in p for p in report.files[0].problems)
    assert report.actual_duration_s == 0.0, "no duration is claimed for a format that was not parsed"
    assert report.to_dict()["notes"] == report.notes
    # An unknown format (no reader at all) is reported the same way.
    ncp = tmp_path / "file_135548_0001.ncp"
    ncp.write_bytes(b"\x01\x02" * 64)
    unknown = cq.validate_capture([ncp], expected_duration_s=2.0)
    assert unknown.level == cq.LEVEL_NOT_CHECKED and unknown.files[0].container == "NCP"
    # A WAVE file keeps the full checks.
    paths, _ = make_recording(tmp_path, (400,))
    assert cq.validate_capture(paths).level in {cq.LEVEL_OK, cq.LEVEL_WARNING}
    # Mixed: the WAVE part is checked, the sidecar is only listed.
    mixed = cq.validate_capture([paths[0], xmrk])
    assert mixed.level in {cq.LEVEL_OK, cq.LEVEL_WARNING} and "not_wave" not in mixed.flags and len(mixed.files) == 1


def test_preview_samples_begin_middle_end(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    paths, _ = make_recording(tmp_path, (3000, 3000, 3000))
    out = cq.write_preview(paths, cq.preview_path_for(paths[0]), center_hz=511_000_000, label="700 MHz DL", block_frames=1024)
    assert out.name == "file_063929.preview.png" and out.stat().st_size > 1000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------------------- pre-run


class _Info:
    def __init__(self, connected=True, model="R40-8", serial="rfeye000123", firmware="2.25"):
        self.connected = connected
        self.model = model
        self.serial = serial
        self.firmware = firmware
        self.status = "Connected" if connected else "Offline"


def _plan(ready=True, estimated=5 * 1024 ** 3):
    from crfs_iq_recorder.cellular_plan import PlannedTask

    task = PlannedTask.from_dict({**_task_dict(), "estimated_bytes": estimated, "duration_s": 30.0})
    plan = CellularPlan(protocol_version="abc", tasks=[task], issues=[], fallback_duration_s=10.0, capability={}, band_order=["700"])
    if not ready:
        from crfs_iq_recorder.cellular_protocol import LEVEL_ERROR, Issue

        plan.issues.append(Issue(LEVEL_ERROR, "x", "bad", None))
    return plan


def test_pre_run_checks_report_identity_clocks_storage_and_plan(tmp_path):
    sensor = StorageSnapshot(path="/mnt/1", total_bytes=200 * 1024 ** 3, free_bytes=6 * 1024 ** 3, ok=True)
    checks = cq.pre_run_checks(
        sensor_info=_Info(),
        sensor_storage=sensor,
        local_dir=tmp_path,
        plan=_plan(),
        local_reserve_bytes=1024,
        previous_identity={"serial": "rfeye999999", "model": "R40-8"},
        now=datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc),
    )
    by_name = {}
    for c in checks:
        by_name.setdefault(c.name, []).append(c)
    assert by_name["identity"][0].level == cq.LEVEL_OK and "rfeye000123" in by_name["identity"][0].message
    assert any("different sensor" in c.message for c in by_name["identity"])
    assert by_name["sensor_clock"][0].level == cq.LEVEL_INFO and "unknown" in by_name["sensor_clock"][0].message
    assert by_name["gnss"][0].level == cq.LEVEL_INFO and "unknown" in by_name["gnss"][0].message
    assert by_name["sensor_storage"][0].level == cq.LEVEL_WARNING, by_name["sensor_storage"][0].message
    assert by_name["local_storage"][0].level in {cq.LEVEL_OK, cq.LEVEL_ERROR}
    assert by_name["plan"][0].level == cq.LEVEL_OK
    assert by_name["destination"][0].level == cq.LEVEL_OK
    assert "test_capture" not in by_name  # nothing is recorded by the check itself
    assert cq.worst_level(checks) in {cq.LEVEL_WARNING, cq.LEVEL_ERROR}


def test_pre_run_checks_compare_identity_clock_offset_and_workbook_state(tmp_path):
    sensor = StorageSnapshot(path="/mnt/1", total_bytes=200 * 1024 ** 3, free_bytes=60 * 1024 ** 3, ok=True)
    common = dict(sensor_info=_Info(), sensor_storage=sensor, local_dir=tmp_path, plan=_plan(), local_reserve_bytes=0)
    # The sensor the plan was made for is the one that answered: ok. A different serial blocks the pass.
    same = {c.name: c for c in cq.pre_run_checks(**common, expected_identity={"serial": "rfeye000123", "model": "R40-8"})}
    assert same["identity_match"].level == cq.LEVEL_OK
    other = {c.name: c for c in cq.pre_run_checks(**common, expected_identity={"serial": "rfeye999999", "model": "R40-8"})}
    assert other["identity_match"].level == cq.LEVEL_ERROR and "rfeye999999" in other["identity_match"].message
    assert [c.name for c in cq.blocking_checks(list(other.values()))] == ["identity_match"]
    # Clock drift is reported with its source; a small offset is fine, a large one is an error.
    small = {c.name: c for c in cq.pre_run_checks(**common, clock_offset_s=0.4, clock_source="status.json system time")}
    assert small["sensor_clock"].level == cq.LEVEL_OK and "0.4" in small["sensor_clock"].message
    drift = {c.name: c for c in cq.pre_run_checks(**common, clock_offset_s=12.0, clock_source="HTTP Date header")}
    assert drift["sensor_clock"].level == cq.LEVEL_WARNING and "ahead" in drift["sensor_clock"].message
    far = {c.name: c for c in cq.pre_run_checks(**common, clock_offset_s=-400.0, clock_source="status.json system time")}
    assert far["sensor_clock"].level == cq.LEVEL_ERROR and "behind" in far["sensor_clock"].message
    assert "sensor_clock" not in [c.name for c in cq.blocking_checks(list(far.values()))]  # reported, never stops the pass
    unknown = {c.name: c for c in cq.pre_run_checks(**common)}
    assert "unknown" in unknown["sensor_clock"].message
    # Workbook: busy is a warning (results queue), missing with a loaded protocol is a warning, missing without is an error.
    book = tmp_path / "book.xlsm"
    book.write_bytes(b"x")
    busy = {c.name: c for c in cq.pre_run_checks(**common, workbook_path=book, workbook_sheet="Cellular Protocol", workbook_state="busy", protocol_loaded=True)}
    assert busy["workbook"].level == cq.LEVEL_WARNING and "queued" in busy["workbook"].message.casefold()
    is_open = {c.name: c for c in cq.pre_run_checks(**common, workbook_path=book, workbook_sheet="Cellular Protocol", workbook_state="open", protocol_loaded=True)}
    assert is_open["workbook"].level == cq.LEVEL_OK and "open in Excel" in is_open["workbook"].message
    missing = {c.name: c for c in cq.pre_run_checks(**common, workbook_path=tmp_path / "gone.xlsm", workbook_sheet="S", workbook_state="closed", protocol_loaded=True)}
    assert missing["workbook"].level == cq.LEVEL_WARNING
    missing_no_plan = {c.name: c for c in cq.pre_run_checks(**common, workbook_path=tmp_path / "gone.xlsm", workbook_sheet="S", workbook_state="closed", protocol_loaded=False)}
    assert missing_no_plan["workbook"].level == cq.LEVEL_ERROR
    assert "workbook" not in cq.BLOCKING_CHECKS
    # A destination that cannot be written blocks; the check never deletes anything.
    (tmp_path / "file.txt").write_text("not a folder")
    unwritable = {c.name: c for c in cq.pre_run_checks(**{**common, "local_dir": tmp_path / "file.txt"}, check_writable=True)}
    assert unwritable["destination"].level == cq.LEVEL_ERROR
    assert (tmp_path / "file.txt").read_text() == "not a folder"
    # Local storage: the estimate plus the reserve must fit; a tiny free space blocks and nothing is deleted.
    low = {c.name: c for c in cq.pre_run_checks(**{**common, "local_reserve_bytes": 10 ** 18})}
    assert low["local_storage"].level == cq.LEVEL_ERROR and "local_storage" in [c.name for c in cq.blocking_checks(list(low.values()))]


def test_pre_run_checks_flag_disconnected_bad_clock_and_full_sensor(tmp_path):
    sensor = StorageSnapshot(path="/mnt/1", total_bytes=200 * 1024 ** 3, free_bytes=1 * 1024 ** 3, ok=True)
    checks = cq.pre_run_checks(
        sensor_info=_Info(connected=False),
        sensor_storage=sensor,
        local_dir=tmp_path,
        plan=_plan(ready=False),
        local_reserve_bytes=0,
        sensor_time=datetime(2001, 1, 1, tzinfo=timezone.utc),
    )
    levels = {c.name: c.level for c in checks}
    assert levels["connection"] == cq.LEVEL_ERROR
    assert levels["sensor_clock"] == cq.LEVEL_ERROR
    assert levels["sensor_storage"] == cq.LEVEL_ERROR
    assert levels["plan"] == cq.LEVEL_ERROR
    assert cq.worst_level(checks) == cq.LEVEL_ERROR
    assert cq.plausible_time(datetime(2030, 1, 1, tzinfo=timezone.utc), now=datetime(2026, 1, 1, tzinfo=timezone.utc)).endswith("in the future")
    assert cq.plausible_time(None) is None


# ----------------------------------------------------------------------------- manifests


def _task_dict():
    return {
        "task_key": "r2#w1of1#r1", "entry_id": "r2", "row": 2, "band": "700", "band_label": "700 MHz", "nominal_hz": 700 * MHZ,
        "duplex": "FDD", "direction": "DL", "network": "Cellcom", "technology": "LTE", "target_class": "",
        "requested_start_hz": 768 * MHZ, "requested_stop_hz": 778 * MHZ, "window_start_hz": 768 * MHZ, "window_stop_hz": 778 * MHZ,
        "window_index": 1, "window_count": 1, "overlap_prev_hz": 0, "repetition": 1, "repetitions": 1, "duration_s": 10.0,
        "duration_source": "workbook", "center_hz": 773 * MHZ, "bandwidth_hz": 10 * MHZ, "sample_rate_hz": 15_625_000,
        "estimated_bytes": 625_000_000, "estimated_parts": 2, "adjustments": [], "extra": {}, "notes": "",
    }


def _round_dict():
    return {
        "round_id": "camp_20261103-0800_0600Z", "scheduled_local": "2026-11-03T08:00:00+02:00", "scheduled_utc": "2026-11-03T06:00:00+00:00",
        "status": "done", "actual_start_utc": "2026-11-03T06:00:03+00:00", "finished_utc": "2026-11-03T06:04:00+00:00",
    }


def test_capture_manifest_keeps_requested_and_actual_windows_apart_and_says_unknown(tmp_path):
    state = {
        "task_key": "r2#w1of1#r1", "status": "confirmed", "record_id": "rec1", "started_utc": "2026-11-03T06:00:05+00:00",
        "finished_utc": "2026-11-03T06:00:16+00:00", "summary": {"held_s": 9.98, "requested_s": 10.0, "parts": 2, "total_bytes": 1000, "stem": "file_080005"},
        "files": [{"name": "file_080005_0001.wav", "remote_path": "/mnt/1/remdata/20261103/file_080005_0001.wav", "size": 500, "download_status": "verified", "sha256": "ab" * 32}],
        "quality": {"level": "ok", "flags": [], "preview_path": str(tmp_path / "file_080005.preview.png")}, "excel_status": "queued", "notes": ["n"], "merged_output": "",
    }
    data = build_capture_manifest(
        campaign={"campaign": "Camp", "campaign_id": "C1", "site": "Site", "site_id": "S1", "visit_id": "V1", "scenario": {"environment": "indoor"}},
        round_record=_round_dict(), task=_task_dict(), task_state=state,
        sensor={"model": "R40-8", "serial": "rfeye000123"}, protocol_version="abc123", timezone_label="Computer local time (UTC+02:00)",
    )
    assert data["identifiers"]["protocol_version"] == "abc123"
    assert data["allocation"]["requested_start_hz"] == 768 * MHZ and data["allocation"]["requested_stop_hz"] == 778 * MHZ
    assert data["allocation"]["request_rule"].startswith("one request per protocol row")
    assert data["allocation"]["recorded_range_hz_from_metadata"] == "unknown"
    assert data["recording"]["measured_duration_s"] == 9.98 and data["recording"]["requested_duration_s"] == 10.0
    # Nothing measured from the files: the sample rate and the request reply are unknown, not modelled.
    assert data["recording"]["sample_rate_hz"] == "unknown" and data["recording"]["sample_count"] == "unknown"
    assert data["request"]["payload_sent"] == "unknown" and data["request"]["earlier_attempts"] == []
    assert "repeat interval" in data["recording"]["sample_rate_note"]
    assert data["sensor"]["firmware"] == "unknown" and data["sensor"]["gnss_status"] == "unknown"
    assert data["scenario"] == {"environment": "indoor"}
    assert data["files"][0]["sha256"] == "ab" * 32
    assert data["derived_images"] == [str(tmp_path / "file_080005.preview.png")]
    # The format that was sent is recorded; nothing is assumed when it is missing.
    assert data["recording"]["recording_format"] == "unknown" and data["recording"]["encoding"] == "unknown"
    state["summary"]["recording_format"] = "XDAT"
    again = build_capture_manifest(
        campaign={}, round_record=_round_dict(), task=_task_dict(), task_state=state, sensor={}, protocol_version="abc123",
        timezone_label="",
    )
    assert again["recording"]["recording_format"] == "XDAT" and again["recording"]["encoding"].startswith("XDAT (sensor format")
    state["summary"]["recording_format"] = "WAVE"
    wave = build_capture_manifest(
        campaign={}, round_record=_round_dict(), task=_task_dict(), task_state=state, sensor={}, protocol_version="abc123",
        timezone_label="",
    )
    assert wave["recording"]["encoding"].startswith("WAVE, 16-bit PCM")
    path = manifest_path(tmp_path, "file_080005", fallback_name="x")
    write_manifest(path, data)
    assert path.name == "file_080005.cellular.json" and read_manifest(path)["identifiers"]["protocol_version"] == "abc123"
    # What the sensor's files say is kept next to the request; an earlier attempt stays listed.
    state["summary"].update(
        measurement={"centre_hz": 773_000_000, "bandwidth_hz": 10_000_000, "sample_rate_hz": 15_625_000,
                     "parts": [{"name": "file_080005_0001.wav", "duration_s": 9.98, "sample_rate_hz": 15_625_000}]},
        payload={"center_frequency": 773_000_000, "bandwidth": 10_000_000, "duration": 10.0},
        sensor_reply={"bandwidth": 10_000_000},
    )
    state.update(attempts=2, result="completed", history=[{"attempt": 1, "status": "unknown", "result": "unconfirmed", "files": []}])
    measured = build_capture_manifest(
        campaign={}, round_record=_round_dict(), task=_task_dict(), task_state=state, sensor={}, protocol_version="abc123",
        timezone_label="",
    )
    assert measured["allocation"]["recorded_range_hz_from_metadata"] == [768_000_000.0, 778_000_000.0]
    assert measured["recording"]["sample_rate_hz"] == 15_625_000 and measured["recording"]["sample_count"] == 155_937_500
    assert measured["recording"]["result"] == "completed"
    assert measured["request"]["attempt"] == 2 and measured["request"]["payload_sent"]["center_frequency"] == 773_000_000
    assert measured["request"]["earlier_attempts"][0]["result"] == "unconfirmed"


def test_handover_counts_logical_captures_files_images_and_merged_separately():
    rounds = [_round_dict()]
    confirmed = {
        "task_key": "r2#w1of1#r1", "status": "confirmed", "summary": {"held_s": 10.0, "requested_s": 10.0, "stem": "file_1"},
        "files": [{"name": "a_0001.wav", "size": 10, "download_status": "verified", "sha256": "x"}, {"name": "a_0002.wav", "size": 10, "download_status": "pending"}],
        "quality": {"level": "warning", "flags": ["clipping"], "preview_path": "a.preview.png"}, "merged_output": "a_merged.wav", "short": False,
    }
    uncertain = {"task_key": "r3#w1of1#r1", "status": "unknown", "summary": {}, "files": [], "quality": {}}
    failed = {"task_key": "r4#w1of1#r1", "status": "not_sent", "summary": {}, "files": [], "quality": {}}
    skipped = {"task_key": "r5#w1of1#r1", "status": "skipped", "summary": {}, "files": [], "quality": {}}
    plan_tasks = {k: {**_task_dict(), "task_key": k} for k in ("r2#w1of1#r1", "r3#w1of1#r1", "r4#w1of1#r1", "r5#w1of1#r1")}
    data = build_handover(
        campaign={"campaign": "Camp", "site": "Site"}, rounds=rounds, tasks_by_round={rounds[0]["round_id"]: [confirmed, uncertain, failed, skipped]},
        plan_tasks=plan_tasks, sensor={"model": "R40-8"}, protocol_version="abc", timezone_label="UTC",
    )
    counts = data["counts"]
    assert counts["logical_captures_confirmed"] == 1 and counts["files"] == 2 and counts["files_verified_locally"] == 1
    assert counts["derived_images"] == 1 and counts["merged_outputs"] == 1
    assert counts["uncertain"] == 1 and counts["failed"] == 1 and counts["not_recorded"] == 1
    assert data["quality_warnings"] == ["700 MHz DL in camp_20261103-0800_0600Z: clipping"]
    assert data["content_not_inspected"] == [] and counts["content_not_inspected"] == 0
    text = handover_markdown(data)
    assert "Derived images (previews): 1" in text and "Uncertain: 1" in text
    assert "nothing is uploaded or deleted automatically" in text
    assert "Content not inspected" not in text
    # A non-WAVE capture is not a quality warning: it is listed as not inspected.
    xdat = {
        **confirmed, "merged_output": "",
        "files": [{"name": "a_0001.xdat", "size": 10, "download_status": "verified", "sha256": "x"}, {"name": "a_0001.xhdr", "size": 1, "download_status": "verified", "sha256": "y"}],
        "quality": {"level": "not_checked", "flags": ["not_wave"], "files": [{"container": "XDAT"}, {"container": "XHDR"}]},
    }
    data = build_handover(
        campaign={"campaign": "Camp", "site": "Site", "recording_format": "XDAT"}, rounds=rounds, tasks_by_round={rounds[0]["round_id"]: [xdat]},
        plan_tasks=plan_tasks, sensor={"model": "R40-8"}, protocol_version="abc", timezone_label="UTC",
    )
    assert data["quality_warnings"] == [] and data["counts"]["content_not_inspected"] == 1
    assert data["content_not_inspected"][0].startswith("700 MHz DL in camp_20261103-0800_0600Z: XDAT, XHDR — sizes and SHA-256 verified")
    text = handover_markdown(data)
    assert "## Quality warnings (0)" in text and "## Content not inspected (1)" in text


# ----------------------------------------------------------------------------- Excel


HEADERS = ["Collection Event", "Class", "Freq Start", "Freq Stop", "RBW", "Scan Rate", "Sensor", "Snapshot ID", "IQ Recording", "IQ Image", "Playback Start", "Playback End", "Playback Time (UTC-3)"]


def _workbook(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Cellular"
    ws.append(HEADERS)
    ws.append(["Haifa", "Cellular 700 MHz DL", "768 MHz", "778 MHz", "1 kHz", "10 Hz", "CRFS 40-8", None, None, None, None, None, None])
    wb.save(path)
    return path


def test_results_sheet_is_created_once_with_headers_and_backup(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_has_workbook", lambda _p: False)
    path = _workbook(tmp_path / "book.xlsx")
    store = ExcelLogStore(tmp_path / "queue.json")
    assert ensure_results_sheet(store, path) is True
    assert ensure_results_sheet(store, path) is True
    wb = load_workbook(path)
    assert RESULTS_SHEET in wb.sheetnames
    assert [c.value for c in wb[RESULTS_SHEET][1]] == HEADERS
    assert [c.value for c in wb["Cellular"][1]] == HEADERS, "existing sheets untouched"
    wb.close()
    backups = list(tmp_path.glob("book*.crfs-backup-*"))
    assert len(backups) == 1
    # Open in Excel: the sheet is added through Excel (1.15); when Excel cannot take the
    # call the work stays queued and the file is not rewritten underneath Excel.
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_workbook_state", lambda _p: "open")
    monkeypatch.setattr("crfs_iq_recorder.excel_com.add_worksheet_com", lambda *_a: False)
    other = _workbook(tmp_path / "open.xlsx")
    with pytest.raises(ExcelBusyError):
        ensure_results_sheet(store, other)
    assert RESULTS_SHEET not in load_workbook(other, read_only=True).sheetnames


def test_worksheet_choice_and_rows_through_the_queue_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.excel_log.excel_has_workbook", lambda _p: False)
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_has_workbook", lambda _p: False)
    path = _workbook(tmp_path / "book.xlsx")
    catalog = load_workbook_catalog(path)
    task = _task_dict()
    assert class_text(task) == "Cellular 700 MHz DL"
    assert worksheet_for_task(task, catalog) == "Cellular"
    assert worksheet_for_task({**task, "direction": "UL"}, catalog) == RESULTS_SHEET
    assert collection_event_text("Camp", "Site", "camp_20261103-0800_0600Z") == "Camp / Site / camp_20261103-0800_0600Z"
    store = ExcelLogStore(tmp_path / "queue.json")
    files = [
        {"name": "file_080005_0002.wav", "remote_path": "/mnt/1/remdata/20261103/file_080005_0002.wav", "size": 5},
        {"name": "file_080005_0001.wav", "remote_path": "/mnt/1/remdata/20261103/file_080005_0001.wav", "size": 5},
    ]
    kwargs = dict(workbook=path, worksheet="Cellular", task=task, collection_event="Camp / Site / R1", sensor_name="CRFS 40-8",
                  sensor_id="rfeye000123", record_id="rec1", files=files, duration_s=10.0, local_dir=tmp_path)
    assert queue_capture_rows(store, **kwargs) == 2
    assert queue_capture_rows(store, **kwargs) == 0, "same sensor + remote path is never queued twice"
    saved, pending = flush_excel_queue(store)
    assert (saved, pending) == (2, 0)
    wb = load_workbook(path)
    ws = wb["Cellular"]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    assert rows[0][1] == "Cellular 700 MHz DL" and rows[0][8] in (None, ""), "the protocol/template row is untouched"
    written = [r for r in rows if r[8]]
    assert len(written) == 1, "split parts of one capture share one row"
    assert split_iq_filenames(written[0][8]) == ["file_080005_0001.wav", "file_080005_0002.wav"]
    assert written[0][0] == "Camp / Site / R1" and written[0][2] == "768 MHz" and written[0][3] == "778 MHz"
    assert written[0][10] == "10 seconds"
    wb.close()
    assert store.is_logged("rfeye000123", "/mnt/1/remdata/20261103/file_080005_0001.wav")


# ----------------------------------------------------------------------------- coverage


def test_coverage_rows_summary_and_filter(tmp_path):
    from crfs_iq_recorder.cellular_plan import PlannedTask
    from crfs_iq_recorder.cellular_runner import CampaignSettings, SessionState, TaskState
    from crfs_iq_recorder.cellular_scheduler import RoundRecord

    tasks = [PlannedTask.from_dict({**_task_dict(), "task_key": "a", "direction": "DL"}), PlannedTask.from_dict({**_task_dict(), "task_key": "b", "direction": "UL", "window_start_hz": 713 * MHZ, "window_stop_hz": 723 * MHZ})]
    plan = CellularPlan(protocol_version="v", tasks=tasks, issues=[], fallback_duration_s=None, capability={}, band_order=["700"])
    rounds = [
        RoundRecord(round_id="R1", scheduled_local=datetime(2026, 11, 3, 8, 0, tzinfo=timezone(timedelta(hours=2))), scheduled_utc=datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc), status="done"),
        RoundRecord(round_id="R2", scheduled_local=datetime(2026, 11, 3, 12, 0, tzinfo=timezone(timedelta(hours=2))), scheduled_utc=datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc), status="partial"),
    ]
    session = SessionState(settings=CampaignSettings(site="Site A"), plan=plan, schedule=None, rounds=rounds, tasks={
        "R1": [TaskState(task_key="a", round_id="R1", status="confirmed", files=[{"download_status": "verified"}, {"download_status": "verified"}], quality={"level": "ok", "preview_path": "p.png"}, summary={"held_s": 10.0}),
               TaskState(task_key="b", round_id="R1", status="confirmed", files=[{"download_status": "pending"}], quality={"level": "warning", "flags": ["clipping"]}, summary={"held_s": 10.0})],
        "R2": [TaskState(task_key="a", round_id="R2", status="unknown"), TaskState(task_key="b", round_id="R2", status="not_sent")],
    })
    rows = cq.coverage_rows(session)
    assert len(rows) == 4 and rows[0]["band"] == "700 MHz" and rows[0]["direction"] == "DL" and rows[0]["site"] == "Site A"
    by_band_dir = cq.coverage_summary(rows, group_by=("band", "direction"))
    assert [(g["band"], g["direction"], g["planned"], g["confirmed"], g["files"], g["images"], g["uncertain"], g["failed"]) for g in by_band_dir] == [
        ("700 MHz", "DL", 2, 1, 2, 1, 1, 0),
        ("700 MHz", "UL", 2, 1, 1, 0, 0, 1),
    ]
    assert by_band_dir[1]["quality_warnings"] == 1
    only_r2 = cq.filter_rows(rows, round_id="R2")
    assert len(only_r2) == 2 and all(not r["confirmed"] for r in only_r2)
    text = cq.coverage_text(cq.coverage_summary(rows))
    assert "R1 · Site A · 700 MHz · DL: 1/1 confirmed, 2 file(s), 1 image(s)" in text
