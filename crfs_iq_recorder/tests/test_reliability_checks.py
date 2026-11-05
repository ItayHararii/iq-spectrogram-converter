"""Simulated checks for the 1.15 reliability work (no hardware, no Excel process).

Excel while open (busy COM states, tri-state probe, 'unknown' start time), restart
without duplicates (.part leftovers, pending work), staged download verification
(size / structure / checksum), uncertain outcomes never resent, spectrogram axis
from file metadata only, sensor clock offset, and the pre-collection check.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from crfs_iq_recorder import cellular_quality as cq
from crfs_iq_recorder import excel_com
from crfs_iq_recorder.api_client import EmpClient, RawResponse, SensorClock, parse_http_date, parse_sensor_time
from crfs_iq_recorder.cellular_manifest import (
    START_SOURCE_METADATA,
    START_SOURCE_STEM,
    START_SOURCE_UNKNOWN,
    build_capture_manifest,
    capture_start,
    sensor_clock_block,
)
from crfs_iq_recorder.cellular_preflight import PreflightResult, gather_preflight
from crfs_iq_recorder.cellular_runner import (
    DL_PENDING,
    DL_VERIFIED,
    MODE_NOW,
    S_INTERRUPTED,
    S_PAUSED,
    S_RUNNING,
    S_WAITING_SCHEDULE,
    T_CONFIRMED,
    T_SUBMITTING,
    T_UNKNOWN,
    V_CHECKSUM,
    V_FAILED,
    V_NA,
    V_OK,
    V_SIZE,
    V_STRUCTURE,
    Schedule,
    verification_text,
)
from crfs_iq_recorder.cellular_workbook import WorkbookQueue, flush_queue, layout_from_import, plan_rows_from_import
from crfs_iq_recorder.demo_sftp import reset_shared_demo_browser, shared_demo_browser
from crfs_iq_recorder.download_util import part_path
from crfs_iq_recorder.excel_writer import ExcelBusyError
from crfs_iq_recorder.iq_merge import inspect_part
from crfs_iq_recorder.sensor_storage import StorageSnapshot
from crfs_iq_recorder.spectrogram_preview import build_preview_from_path
from crfs_iq_recorder.validation import ConnectionTarget

from test_cellular_quality import _round_dict, _task_dict
from test_cellular_runner import FakeHost, _plan, _pump, _pump_until, _runner, _settings, qapp  # noqa: F401 - fixture
from test_cellular_workbook import _import, _result, _values, make_workbook
from test_iq_merge import FS, T0, _meta_xml, make_recording, write_part

MHZ = 1_000_000
REMOTE_DIR = "/mnt/1/remdata/20261103"


# ----------------------------------------------------------------------------- 1. Excel while open


class _ComError(Exception):
    """Shape of pywin32's com_error: (hresult, text, excepinfo, argerror)."""

    def __init__(self, hresult: int, text: str = "", inner: int | None = None):
        excepinfo = (0, "Microsoft Excel", text, None, 0, inner) if inner is not None else None
        super().__init__(hresult, text, excepinfo, None)
        self.hresult = hresult
        self.excepinfo = excepinfo


def _signed(code: int) -> int:
    return code - (1 << 32) if code >= 1 << 31 else code


def test_com_busy_states_are_recognised_by_hresult_not_by_text():
    assert excel_com.com_busy_reason(_ComError(_signed(excel_com.RPC_E_CALL_REJECTED), "Call was rejected by callee."))
    assert excel_com.com_busy_reason(_ComError(_signed(excel_com.RPC_E_SERVERCALL_RETRYLATER), ""))
    edit_mode = excel_com.com_busy_reason(_ComError(_signed(0x80020009), "Exception occurred.", inner=_signed(excel_com.VBA_E_IGNORE)))
    assert edit_mode and ("edit" in edit_mode.casefold() or "busy" in edit_mode.casefold())
    assert excel_com.com_busy_reason(_ComError(_signed(excel_com.RPC_E_DISCONNECTED), "The object invoked has disconnected"))
    assert excel_com.com_busy_reason(_ComError(_signed(0x80004005), "Unspecified error")) is None
    assert excel_com.com_busy_reason(ValueError("not COM")) is None


def test_busy_excel_keeps_results_queued_and_never_falls_back_to_the_file(tmp_path: Path, monkeypatch):
    """Excel reports the workbook open but refuses calls (a cell is being edited): nothing is
    written through openpyxl behind Excel's back; the rows stay queued and are written later."""
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 1, 1)))
    calls: list[str] = []
    monkeypatch.setattr("crfs_iq_recorder.cellular_workbook.write_results_openpyxl", lambda *a, **k: calls.append("openpyxl"))
    monkeypatch.setattr("crfs_iq_recorder.cellular_workbook.write_results_com", lambda *a, **k: calls.append("com") or (_ for _ in ()).throw(ExcelBusyError("Excel is busy: a cell is being edited")))
    states = iter(["busy", "open"])
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_workbook_state", lambda _p: next(states))
    report = flush_queue(queue)
    assert report.ok and report.busy and report.pending == 1 and report.written == 0
    assert calls == [], "a busy Excel is asked again later; the file is not rewritten underneath it"
    assert queue.pending_items()[0].attempts == 0, "busy is not a failed attempt"
    report = flush_queue(queue)
    assert report.busy and calls == ["com"] and report.pending == 1
    assert _values(path)[1][6] is None


def test_a_save_that_leaves_the_workbook_unsaved_is_busy_not_done():
    """'Excel updated' only after Workbook.Save() returned and Saved is true."""

    class Book:
        def __init__(self, saved_after: bool):
            self.Saved = False
            self._after = saved_after
            self.saves = 0

        def Save(self):
            self.saves += 1
            self.Saved = self._after

    from crfs_iq_recorder.cellular_workbook import _book_saved

    assert _book_saved(Book(True)) is False
    good = Book(True)
    good.Save()
    assert _book_saved(good) is True
    bad = Book(False)
    bad.Save()
    assert _book_saved(bad) is False


def test_unknown_start_time_is_written_as_unknown_never_the_request_time(tmp_path: Path):
    path = make_workbook(tmp_path / "p.xlsx")
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, None))
    report = flush_queue(queue, excel_open=lambda p: False)
    assert report.written == 1
    row = _values(path)[1]
    assert row[6] == "a0.wav" and row[7] == 2.0 and row[8] == "unknown"
    # A later result with a real sensor timestamp replaces the placeholder.
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a0.wav"], 2.0, datetime(2026, 11, 4, 9, 30, 0)))
    flush_queue(queue, excel_open=lambda p: False)
    assert _values(path)[1][8] == datetime(2026, 11, 4, 9, 30, 0)


# ----------------------------------------------------------------------------- 6. capture start time


def test_manifest_start_time_comes_from_the_sensor_files_or_says_unknown():
    state = {
        "task_key": "r2#w1of1#r1", "status": "confirmed", "record_id": "rec1", "started_utc": "2026-11-03T06:00:05+00:00",
        "finished_utc": "2026-11-03T06:00:16+00:00",
        "summary": {"held_s": 9.98, "requested_s": 10.0, "parts": 2, "total_bytes": 1000, "stem": "nostamp",
                    "started_at": "2026-11-03T08:00:01+02:00", "finished_at": "2026-11-03T08:00:12+02:00"},
        "files": [{"name": "nostamp_0001.wav", "remote_path": f"{REMOTE_DIR}/nostamp_0001.wav", "size": 500, "download_status": "verified"}],
        "quality": {}, "excel_status": "queued", "notes": [], "merged_output": "",
    }
    when, source = capture_start(state)
    assert when is None and source == START_SOURCE_UNKNOWN
    data = build_capture_manifest(campaign={}, round_record=_round_dict(), task=_task_dict(), task_state=state, sensor={}, protocol_version="v", timezone_label="")
    assert data["schedule"]["capture_started_local"] == "unknown" and data["schedule"]["capture_start_source"] == START_SOURCE_UNKNOWN
    assert data["schedule"]["request_submitted_local"] == "2026-11-03T08:00:01+02:00"
    assert data["clock"]["sensor_minus_computer_s"] == "unknown"
    # The UTC time in the sensor's file name.
    state["summary"]["stem"] = "iq_20261103_060007"
    when, source = capture_start(state)
    assert source == START_SOURCE_STEM and when.astimezone(timezone.utc) == datetime(2026, 11, 3, 6, 0, 7, tzinfo=timezone.utc)
    # The CRFS metadata timestamp wins over the name; the earliest part counts.
    state["files"] = [
        {"name": "x_0002.wav", "signal_time": "2026-11-03T06:00:09.500Z"},
        {"name": "x_0001.wav", "signal_time": "2026-11-03T06:00:08.250Z"},
    ]
    when, source = capture_start(state)
    assert source == START_SOURCE_METADATA and when.astimezone(timezone.utc) == datetime(2026, 11, 3, 6, 0, 8, 250000, tzinfo=timezone.utc)
    data = build_capture_manifest(
        campaign={}, round_record=_round_dict(), task=_task_dict(), task_state=state,
        sensor={"clock_offset_s": -0.7, "clock_source": "status page system time (UTC)", "clock_time_source": "NTP", "clock_checked_utc": "2026-11-03T05:59:00+00:00", "clock_status": "sensor minus computer -0.7 s"},
        protocol_version="v", timezone_label="",
    )
    assert data["schedule"]["capture_started_utc"] == "2026-11-03T06:00:08.250000+00:00"
    assert datetime.fromisoformat(data["schedule"]["capture_started_local"]).astimezone(timezone.utc) == datetime(2026, 11, 3, 6, 0, 8, 250000, tzinfo=timezone.utc)
    assert data["clock"]["sensor_minus_computer_s"] == -0.7 and data["clock"]["sensor_time_source"] == "NTP"
    assert sensor_clock_block({})["source"] == "unknown"


class _PathTransport:
    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    def request(self, method, url, **kwargs):
        from urllib.parse import urlparse

        self.calls.append((method, url))
        return self.by_path[urlparse(url).path]


def _target() -> ConnectionTarget:
    return ConnectionTarget(host="192.0.2.10", port=80, use_https=False, username="admin", timeout_s=5.0)


def test_sensor_clock_offset_is_measured_from_the_status_page_and_reports_its_source(monkeypatch):
    assert parse_sensor_time("2026-11-04 17:24:51+0000") == datetime(2026, 11, 4, 17, 24, 51, tzinfo=timezone.utc)
    assert parse_sensor_time("2026-11-04T17:24:51Z") == datetime(2026, 11, 4, 17, 24, 51, tzinfo=timezone.utc)
    assert parse_sensor_time("") is None and parse_sensor_time("yesterday") is None
    assert parse_http_date("Wed, 04 Nov 2026 17:24:51 GMT") == datetime(2026, 11, 4, 17, 24, 51, tzinfo=timezone.utc)
    # Simulated clock offset: the sensor answers a time 42 s ahead of the computer.
    ahead = (datetime.now(timezone.utc) + timedelta(seconds=42)).strftime("%Y-%m-%d %H:%M:%S+0000")
    body = json.dumps({"values": {"02-system-time": ahead, "03-time-source": "NTP", "04-time-info": {"01-time-source": "GPS", "02-time-fix": "\u2718", "03-time-satellites": "0"}}})
    transport = _PathTransport({"/api/values/status.json": RawResponse(200, body, {"Content-Type": "application/json", "Date": "Wed, 04 Nov 2026 17:24:51 GMT"}, 0.01, "http://192.0.2.10/api/values/status.json")})
    client = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    clock = client.fetch_sensor_clock(_target(), "pass")
    assert clock.available and 40.0 < clock.offset_s < 44.0, clock
    assert clock.source.startswith("status page") and clock.time_source == "NTP" and "no time fix" in clock.gnss_status
    assert [m for m, _u in transport.calls] == ["GET"]
    # No system time on the page: the HTTP Date header is the (whole-second) fallback.
    transport = _PathTransport({"/api/values/status.json": RawResponse(200, json.dumps({"values": {}}), {"Content-Type": "application/json", "Date": "Wed, 04 Nov 2026 17:24:51 GMT"}, 0.01, "http://192.0.2.10/api/values/status.json")})
    clock = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok")).fetch_sensor_clock(_target(), "pass")
    assert clock.available and clock.source.startswith("HTTP Date")
    # Unreachable: unknown, never a guess.
    transport = _PathTransport({"/api/values/status.json": RawResponse(404, "", {}, 0.01, "http://192.0.2.10/api/values/status.json")})
    clock = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok")).fetch_sensor_clock(_target(), "pass")
    assert not clock.available and clock.error == "HTTP 404" and clock.to_dict()["offset_s"] is None
    assert SensorClock(available=False).to_dict()["available"] is False


# ----------------------------------------------------------------------------- 4. spectrogram axis


def _tone_part(path: Path, *, fs: int, center_hz: float, tone_offset_hz: float, frames: int, meta: bool = True) -> None:
    """A CRFS-style part whose samples are one complex tone at centre + tone_offset_hz."""
    n = np.arange(frames)
    phase = 2.0 * np.pi * tone_offset_hz * n / fs
    i = np.round(np.cos(phase) * 20000).astype("<i2")
    q = np.round(np.sin(phase) * 20000).astype("<i2")
    data = np.empty(frames * 2, dtype="<i2")
    data[0::2] = i
    data[1::2] = q
    payload = data.tobytes()
    fmt = struct.pack("<HHIIHH", 1, 2, fs, fs * 4, 4, 16)
    trailer = b""
    if meta:
        xml = _meta_xml(T0, rf=center_hz, bw=fs * 0.8, fs=fs)
        trailer = b"meta" + struct.pack("<I", len(xml)) + xml
    riff_size = 4 + 8 + 16 + 8 + len(payload) + len(trailer)
    blob = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    blob += b"data" + struct.pack("<I", len(payload)) + payload + trailer
    path.write_bytes(blob)


def test_synthetic_tone_lands_at_its_rf_frequency_on_an_absolute_axis_with_request_markers(tmp_path: Path):
    from crfs_iq_recorder.spectrogram_preview import PREVIEW_N_FFT, _blackmanharris, _stft_power

    fs, centre, offset = 20_000, 768.0 * MHZ, 2_500.0  # tone at 768.0025 MHz
    path = tmp_path / "tone_0001.wav"
    _tone_part(path, fs=fs, center_hz=centre, tone_offset_hz=offset, frames=16384)
    info = inspect_part(path)
    axis = cq.preview_axis([info], requested_start_hz=centre - 8_000, requested_stop_hz=centre + 8_000)
    assert axis.absolute and axis.center_hz == centre and axis.span_hz == fs
    lo, hi = axis.extent_mhz()
    assert (lo, hi) == pytest.approx(((centre - fs / 2) / 1e6, (centre + fs / 2) / 1e6))
    # Where the tone falls on that axis, computed the way the renderer does it.
    block = cq.read_block(info, 0, 16384)
    iq = block[:, 0].astype(np.complex64) + 1j * block[:, 1].astype(np.complex64)
    window, scale = _blackmanharris(PREVIEW_N_FFT)
    power = np.fft.fftshift(_stft_power(iq, PREVIEW_N_FFT, PREVIEW_N_FFT // 2, window, scale), axes=0)
    peak_bin = int(np.argmax(power.mean(axis=1)))
    peak_mhz = lo + (peak_bin + 0.5) / PREVIEW_N_FFT * (hi - lo)
    assert peak_mhz == pytest.approx((centre + offset) / 1e6, abs=(fs / PREVIEW_N_FFT) / 1e6)
    markers = dict((text.split(" ")[1], x) for x, text in axis.markers_mhz())
    assert markers["start"] == pytest.approx((centre - 8_000) / 1e6) and markers["stop"] == pytest.approx((centre + 8_000) / 1e6)
    legend = "\n".join(axis.legend_lines())
    assert "Requested range: 767.992-768.008 MHz" in legend
    assert "Centre (file metadata): 768 MHz" in legend
    assert "Sample rate: 0.02 MS/s" in legend and "Displayed span: 0.02 MHz (= sample rate" in legend
    assert "Sensor-reported bandwidth (metadata): 0.016 MHz" in legend
    out = cq.write_preview([path], tmp_path / "tone.preview.png", label="tone", requested_start_hz=centre - 8_000, requested_stop_hz=centre + 8_000)
    assert out.is_file() and out.stat().st_size > 1000
    assert cq.describe_preview_axis([path], requested_start_hz=1, requested_stop_hz=2)["center_source"].startswith("file metadata")
    assert path.read_bytes()[:4] == b"RIFF", "the IQ file is never altered"


def test_a_file_without_frequency_metadata_gets_a_relative_axis_and_says_unknown(tmp_path: Path):
    path = tmp_path / "plain_0001.wav"
    write_part(path, frames=4096, start=None, seed=3, fs=8000, meta=False)
    info = inspect_part(path)
    axis = cq.preview_axis([info], requested_start_hz=700 * MHZ, requested_stop_hz=710 * MHZ)
    assert not axis.absolute and axis.center_hz is None
    assert axis.extent_mhz() == pytest.approx((-0.004, 0.004))
    assert axis.markers_mhz() == [], "requested markers are never placed on a guessed axis"
    assert "centre unknown" in axis.xlabel()
    assert axis.to_dict()["center_source"] == "unknown"
    legend = axis.legend_lines()
    assert any(line.startswith("Centre: unknown") for line in legend)
    assert any(line.startswith("Sensor-reported bandwidth: unknown") for line in legend)
    # The planned centre is text only ("not confirmed"); it never positions the axis.
    out = cq.write_preview([path], tmp_path / "plain.preview.png", center_hz=705 * MHZ)
    assert out.is_file()
    result = build_preview_from_path(path, cache_id="plain-test", center_hz=705 * MHZ, bandwidth_hz=10 * MHZ, requested_start_hz=700 * MHZ, requested_stop_hz=710 * MHZ)
    assert "not confirmed" in result.caption and "705" in result.caption
    paths, _ = make_recording(tmp_path, (600, 600))
    good = build_preview_from_path(paths[0], cache_id="meta-test", requested_start_hz=500 * MHZ, requested_stop_hz=522 * MHZ)
    assert "file metadata" in good.caption and "511" in good.caption


# ----------------------------------------------------------------------------- 5. download verification


def test_local_copy_verification_detects_size_mismatch_truncation_and_bad_structure(qapp, tmp_path: Path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    good = tmp_path / "file_000001_0001.wav"
    write_part(good, frames=500, start=T0, seed=1)
    size = good.stat().st_size
    problem, stages = runner._verify_local_copy(None, {"name": good.name, "size": size}, good)
    assert problem == "" and stages == {V_SIZE: V_OK, V_STRUCTURE: V_OK}
    assert verification_text({**stages, V_CHECKSUM: V_OK}) == "size verified, structure verified, checksum verified"
    assert verification_text({**stages, V_CHECKSUM: V_NA}).endswith("checksum not available from the sensor")
    assert verification_text({}) == "not verified"
    # The sensor lists more bytes than arrived: size fails before anything else is read.
    problem, stages = runner._verify_local_copy(None, {"name": good.name, "size": size + 100}, good)
    assert "bytes locally" in problem and stages == {V_SIZE: V_FAILED}
    # A cut-short file with a readable header is truncation, not a good file.
    short = tmp_path / "file_000002_0001.wav"
    write_part(short, frames=500, start=T0, seed=2, truncate_by=300)
    problem, stages = runner._verify_local_copy(None, {"name": short.name, "size": short.stat().st_size}, short)
    assert stages[V_SIZE] == V_OK and stages[V_STRUCTURE] == V_FAILED and "cut short" in problem
    # Not a WAVE file at all.
    junk = tmp_path / "file_000003_0001.wav"
    junk.write_bytes(b"not a riff file at all, just bytes" * 4)
    problem, stages = runner._verify_local_copy(None, {"name": junk.name, "size": junk.stat().st_size}, junk)
    assert stages[V_STRUCTURE] == V_FAILED and problem
    # The sample count must match what was read from the sensor's copy of the same file.
    state = type("S", (), {"summary": {"measurement": {"parts": [{"name": good.name, "duration_s": 0.75, "sample_rate_hz": FS}]}}})()
    problem, stages = runner._verify_local_copy(state, {"name": good.name, "size": size}, good)
    assert stages[V_STRUCTURE] == V_FAILED and "read on the sensor" in problem
    # An XDAT part without its header (and nothing read on the sensor): size only,
    # structure not available. Other formats: structure is not applicable.
    xdat = tmp_path / "file_000004_0001.xdat"
    xdat.write_bytes(b"\x00" * 64)
    problem, stages = runner._verify_local_copy(None, {"name": xdat.name, "size": 64}, xdat)
    assert problem == "" and stages[V_STRUCTURE] == V_NA
    ncp = tmp_path / "file_000005_0001.ncp"
    ncp.write_bytes(b"\x00" * 64)
    problem, stages = runner._verify_local_copy(None, {"name": ncp.name, "size": 64}, ncp)
    assert problem == "" and stages[V_STRUCTURE] == "not_applicable"


def _publish(stem: str, *, seconds: float = 0.2, center_hz: float = 708 * MHZ, bandwidth_hz: float = 10 * MHZ):
    browser = shared_demo_browser()
    return browser, browser.publish_capture(stem, directory=REMOTE_DIR, seconds=seconds, center_hz=center_hz, bandwidth_hz=bandwidth_hz, sample_rate=20_000)


def test_interrupted_part_is_replaced_and_the_download_is_verified_in_three_stages(qapp, tmp_path: Path, monkeypatch):
    """Simulated: a '.part' left by an interrupted transfer, then a real (demo-transport)
    download of the same file: the .part is never counted, the file arrives once, and the
    size, WAVE structure and SHA-256 against the sensor's digest are all verified."""
    import crfs_iq_recorder.cellular_runner as cr

    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.01)
    reset_shared_demo_browser()
    browser, entries = _publish("file_000001")
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, verify_checksums=True, quality_checks=False, previews=False))
    # The download mirrors the sensor's dated folder; the .part of an older version sat in the flat root.
    local = tmp_path / "downloads" / "20261103" / "file_000001_0001.wav"
    legacy = tmp_path / "downloads" / "file_000001_0001.wav"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    leftover = part_path(legacy)
    leftover.write_bytes(b"half a file")
    host.finish(host.requests[0], names=["file_000001_0001.wav"])
    _pump(qapp)
    state = runner.current_tasks()[0]
    assert state.status == T_CONFIRMED
    assert _pump_until(qapp, lambda: state.files[0].get("download_status") == DL_VERIFIED, timeout=15.0), state.files
    item = state.files[0]
    assert not leftover.exists() and local.is_file() and item["local_path"] == str(local)
    assert item["size"] == entries[0].size == local.stat().st_size and item["remote_size_stable"] is True
    assert item["verification"] == {V_SIZE: V_OK, V_STRUCTURE: V_OK, V_CHECKSUM: V_OK}, item
    assert item["verified_text"] == "size verified, structure verified, checksum verified"
    assert item["sha256"] == hashlib.sha256(local.read_bytes()).hexdigest() == item["remote_sha256"]
    assert "unfinished download" in item["note"] and ".part" in item["note"]
    assert list((tmp_path / "downloads").rglob("*.part")) == []
    assert [p.name for p in (tmp_path / "downloads").rglob("file_000001*.wav")] == ["file_000001_0001.wav"], "one copy, no duplicate"
    # The record file of the recording sits beside its files.
    assert Path(state.manifest_path).parent == local.parent, state.manifest_path


def test_download_without_a_sensor_checksum_is_verified_by_size_and_structure_only(qapp, tmp_path: Path, monkeypatch):
    import crfs_iq_recorder.cellular_runner as cr

    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.01)
    reset_shared_demo_browser()
    browser, _entries = _publish("file_000001")
    browser.set_checksum_available(False)
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, verify_checksums=True, quality_checks=False, previews=False))
    host.finish(host.requests[0], names=["file_000001_0001.wav"])
    _pump(qapp)
    state = runner.current_tasks()[0]
    assert _pump_until(qapp, lambda: state.files[0].get("download_status") == DL_VERIFIED, timeout=15.0), state.files
    item = state.files[0]
    assert item["verification"] == {V_SIZE: V_OK, V_STRUCTURE: V_OK, V_CHECKSUM: V_NA}
    assert item["verified_text"] == "size verified, structure verified, checksum not available from the sensor"
    assert item["sha256"] and not item.get("remote_sha256") and "no checksum" in item["sha256_note"]


def test_checksum_mismatch_is_not_a_verified_copy_and_is_downloaded_again(qapp, tmp_path: Path, monkeypatch):
    import crfs_iq_recorder.cellular_runner as cr

    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.01)
    monkeypatch.setattr(cr, "DOWNLOAD_RETRY_DELAY_S", 0.05)
    reset_shared_demo_browser()
    _publish("file_000001")
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, verify_checksums=True, quality_checks=False, previews=False))
    local = tmp_path / "downloads" / "20261103" / "file_000001_0001.wav"
    # Simulated corruption in transit: the sensor's digest differs from the bytes that arrived, once.
    from crfs_iq_recorder.sftp_client import DemoSftpBrowser

    real = DemoSftpBrowser.remote_sha256
    seen = {"n": 0}

    def wrong_once(self, remote_path, **kwargs):
        seen["n"] += 1
        digest, note = real(self, remote_path, **kwargs)
        return ("0" * 64, note) if seen["n"] == 1 else (digest, note)

    monkeypatch.setattr(DemoSftpBrowser, "remote_sha256", wrong_once)
    host.finish(host.requests[0], names=["file_000001_0001.wav"])
    _pump(qapp)
    state = runner.current_tasks()[0]
    item = state.files[0]
    assert _pump_until(qapp, lambda: int(item.get("attempts") or 0) >= 2 or item.get("download_status") == DL_VERIFIED, timeout=15.0), item
    assert item["attempts"] >= 2, "the mismatching copy was downloaded again, not kept"
    assert _pump_until(qapp, lambda: item.get("download_status") == DL_VERIFIED, timeout=15.0), item
    assert item["verification"][V_CHECKSUM] == V_OK and item["sha256"] == hashlib.sha256(local.read_bytes()).hexdigest() == item["remote_sha256"]
    assert [p.name for p in (tmp_path / "downloads").rglob("file_000001*.wav")] == ["file_000001_0001.wav"]
    assert local.with_name(local.name + ".unverified").is_file(), "the mismatching bytes were set aside, not deleted"
    assert item["set_aside"].endswith(".unverified")


def test_a_shorter_file_than_the_sensor_lists_is_not_verified(qapp, tmp_path: Path):
    """Truncation during the transfer: the local copy is shorter than the sensor's stable size."""
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, verify_checksums=False))
    runner._drain_downloads = lambda: None
    host.finish(host.requests[0], names=["file_000001_0001.wav"])
    _pump(qapp)
    state = runner.current_tasks()[0]
    item = state.files[0]
    item["remote_size_stable"] = True
    item["size"] = 5000
    item["download_status"] = "downloading"
    local = tmp_path / "downloads" / "file_000001_0001.wav"
    write_part(local, frames=100, start=T0, seed=1)  # 444 bytes + meta, far short of 5000
    runner._on_download_finished("file_000001_0001.wav", str(local))
    assert item["download_status"] == DL_PENDING and item["verification"][V_SIZE] == V_FAILED
    assert item["verified_text"] == "size FAILED"
    assert item["size"] == 5000, "the sensor's size is kept; the short copy is not accepted as the new truth"


# ----------------------------------------------------------------------------- 2. restart without duplicates


def test_restart_shows_pending_work_and_resumes_downloads_without_duplicates(qapp, tmp_path: Path, monkeypatch):
    import crfs_iq_recorder.cellular_runner as cr

    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.01)
    reset_shared_demo_browser()
    _publish("file_000001")
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, verify_checksums=True, quality_checks=False, previews=False))
    runner._drain_downloads = lambda: None  # the application "closes" before the first download starts
    host.finish(host.requests[0], names=["file_000001_0001.wav"])
    _pump(qapp)
    assert len(host.requests) == 2 and runner.current_tasks()[1].status == T_SUBMITTING
    local = tmp_path / "downloads" / "20261103" / "file_000001_0001.wav"  # the sensor's dated folder
    local.parent.mkdir(parents=True, exist_ok=True)
    part_path(local).write_bytes(b"interrupted")
    runner.shutdown()

    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2)
    assert runner2.state == S_INTERRUPTED and host2.requests == []
    work = runner2.pending_work()
    assert work["downloads"] == 1 and work["leftover_parts"] == ["file_000001_0001.wav"] and work["uncertain"] == 1
    text = runner2.pending_work_text()
    assert text.startswith("Pending:") and ".part" in text and "not confirmed" in text and "never sent again" in text
    assert runner2.status()["pending_work"] == text
    # The uncertain second request is checked first; nothing goes out on its own.
    assert runner2.resume_after_restart() is not None and host2.requests == []
    rid = runner2.session.current_round_id
    runner2.skip_task(rid, runner2.uncertain_tasks()[0][0].task_key)
    assert runner2.resume_after_restart() is None
    first = runner2.session.tasks[rid][0]
    assert _pump_until(qapp, lambda: first.files[0].get("download_status") == DL_VERIFIED, timeout=15.0), first.files
    assert not part_path(local).exists() and local.is_file()
    assert first.files[0]["verification"][V_CHECKSUM] == V_OK
    assert [p.name for p in (tmp_path / "downloads").rglob("file_000001*.wav")] == ["file_000001_0001.wav"]
    assert runner2.leftover_parts() == [] and runner2.pending_work()["downloads"] == 0
    # The third row was sent once after the restart; the first was never recorded again.
    assert len(host2.requests) == 1 and host2.requests[0].label.startswith("1800 MHz DL")


def test_uncertain_outcome_after_restart_is_reconciled_from_the_sensor_not_resent(qapp, tmp_path: Path, monkeypatch):
    """The link dropped while the sensor recorded; the app restarts. The request is looked up
    on the sensor; without a trace it is marked unknown and still never resent on its own."""
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "rec.json")
    reset_shared_demo_browser()
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=False))
    assert runner.current_tasks()[0].status == T_SUBMITTING
    runner.shutdown()
    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2)
    assert runner2.state == S_INTERRUPTED
    uncertain = runner2.uncertain_tasks()
    assert len(uncertain) == 1 and uncertain[0][0].status == T_SUBMITTING
    assert runner2.pending_work()["uncertain"] == 1
    assert runner2.resume_after_restart() is not None, "an unconfirmed request blocks Continue until it is checked"
    assert host2.requests == []
    messages: list[str] = []
    runner2.reconcile(messages.append)
    assert messages == ["Checked with the sensor: 0 recordings found, 1 without a file."]
    task = runner2.session.tasks[runner2.session.current_round_id][0]
    assert task.status == T_UNKNOWN
    assert host2.requests == [], "reconciliation never sends anything"
    # Unknown stays the operator's decision (skip or retry); Continue does not repeat it on its own.
    assert runner2.resume_after_restart() is not None and host2.requests == []
    runner2.skip_task(runner2.session.current_round_id, task.task_key)
    assert runner2.resume_after_restart() is None
    assert len(host2.requests) == 1 and not host2.requests[0].label.startswith("700 MHz UL"), "the next row goes out; the unknown one is not repeated"


# ----------------------------------------------------------------------------- 7. pre-collection check


class _Info:
    def __init__(self, connected=True, model="R40-8", serial="rfeye300540", firmware="2.25-325"):
        self.connected, self.model, self.serial, self.firmware = connected, model, serial, firmware
        self.status = "Connected" if connected else "Offline"


def _plan_for_preflight(tmp_path):
    return _plan(tmp_path)


def test_gather_preflight_blocks_on_identity_and_storage_but_not_on_the_workbook(tmp_path: Path):
    plan = _plan_for_preflight(tmp_path)
    storage = StorageSnapshot(path="/mnt/1", total_bytes=900 * 1024 ** 3, free_bytes=800 * 1024 ** 3, ok=True)
    clock = SensorClock(available=True, sensor_utc=datetime.now(timezone.utc), pc_utc=datetime.now(timezone.utc), offset_s=-0.6, source="status page system time (UTC)", time_source="NTP", gnss_status="GPS: no time fix, 0 satellites")
    common = dict(plan=plan, local_dir=tmp_path / "dl", reserve_bytes=0, workbook_sheet="Cellular Protocol", protocol_loaded=True,
                  fetch_clock=lambda: clock, fetch_storage=lambda: storage, workbook_state=lambda _p: "closed")
    result = gather_preflight(expected_identity={"model": "R40-8", "serial": "rfeye300540"}, workbook_path=None, fetch_info=lambda: _Info(), **common)
    assert result.ok and result.blocking() == [] and result.identity["serial"] == "rfeye300540"
    assert result.clock["offset_s"] == -0.6 and result.storage["free_bytes"] == 800 * 1024 ** 3
    names = {c.name: c for c in result.checks}
    assert names["identity_match"].level == cq.LEVEL_OK and names["sensor_clock"].level == cq.LEVEL_OK and "NTP" in names["sensor_clock"].message
    assert "no time fix" in names["gnss"].message and names["destination"].level == cq.LEVEL_OK
    assert result.summary_line().startswith("Pre-collection check passed")
    assert (tmp_path / "dl").is_dir() and list((tmp_path / "dl").iterdir()) == [], "the write probe leaves nothing behind"
    # Another unit answered: blocked, with the serials in the reason.
    other = gather_preflight(expected_identity={"model": "R40-8", "serial": "rfeye100400"}, workbook_path=None, fetch_info=lambda: _Info(), **common)
    assert not other.ok and other.blocking()[0].name == "identity_match" and "rfeye100400" in other.summary_line()
    # The sensor cannot be read at all: blocked as 'connection'; the clock and storage are not even asked.
    def boom():
        raise OSError("no route to host")

    down = gather_preflight(expected_identity=None, workbook_path=None, fetch_info=boom, **common)
    assert [c.name for c in down.blocking()] == ["connection"] and "no route to host" in down.checks[0].message
    assert down.clock == {} and down.storage == {}
    # Low local storage (huge reserve) blocks; nothing is deleted; the message says so.
    low = gather_preflight(expected_identity=None, workbook_path=None, fetch_info=lambda: _Info(), **{**common, "reserve_bytes": 10 ** 18})
    assert [c.name for c in low.blocking()] == ["local_storage"] and "Nothing is deleted automatically" in low.blocking()[0].message
    # Sensor storage too small for the pass blocks as well.
    tiny = StorageSnapshot(path="/mnt/1", total_bytes=900 * 1024 ** 3, free_bytes=10, ok=True)
    full = gather_preflight(expected_identity=None, workbook_path=None, fetch_info=lambda: _Info(), **{**common, "fetch_storage": lambda: tiny})
    assert [c.name for c in full.blocking()] == ["sensor_storage"]
    # Missing workbook with a loaded protocol: not blocking; the status says Excel updates wait.
    gone = gather_preflight(expected_identity=None, workbook_path=tmp_path / "gone.xlsm", fetch_info=lambda: _Info(), **common)
    assert gone.ok and gone.excel_waiting() and gone.workbook_state == "missing"
    assert "passed with" in gone.summary_line()
    # A busy Excel: not blocking either; the probe raising is treated as busy, never as closed.
    book = tmp_path / "book.xlsm"
    book.write_bytes(b"x")

    def flaky(_p):
        raise RuntimeError("COM call rejected")

    busy = gather_preflight(expected_identity=None, workbook_path=book, fetch_info=lambda: _Info(), **{**common, "workbook_state": flaky})
    assert busy.ok and busy.workbook_state == "busy" and busy.excel_waiting()
    # Unknown clock and storage: reported as unknown, never guessed, never blocking.
    unknown = gather_preflight(expected_identity=None, workbook_path=None, fetch_info=lambda: _Info(), **{**common, "fetch_clock": lambda: SensorClock(available=False, error="HTTP 404"), "fetch_storage": None})
    names = {c.name: c for c in unknown.checks}
    assert unknown.ok and "unknown" in names["sensor_clock"].message and "unknown" in names["sensor_storage"].message
    assert unknown.clock["error"] == "HTTP 404"
    data = unknown.to_dict()
    assert data["ok"] is True and data["checks"] and data["started_utc"] and data["finished_utc"]
    rebuilt = PreflightResult(checks=[cq.Check(**c) for c in data["checks"]])
    assert rebuilt.level == unknown.level


def test_runner_runs_the_check_before_each_pass_and_pauses_with_the_reason(qapp, tmp_path: Path):
    """Simulated: between two passes another unit answers on the address. The second pass does
    not start, the reason names both serials, nothing is sent; once the right unit is back,
    Resume runs the check again and the pass starts."""
    host = FakeHost()
    base = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    logs: list[tuple[str, str]] = []
    runner.log.connect(lambda m, lvl: logs.append((lvl, m)))
    assert runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW, repeat=2), _settings(wait_s=5.0, sensor_model="R40-8", sensor_serial="SN1")) is None
    assert runner.state == S_RUNNING and len(host.requests) == 1
    first_round = runner.session.current_round_id
    assert runner.session.preflight[first_round]["ok"] is True
    for _ in range(3):
        host.finish(host.requests[-1])
        _pump(qapp)
        runner._wait_deadline = 0.0
        runner._tick()
    assert runner.state == S_WAITING_SCHEDULE and len(host.requests) == 3
    host._sensor_info.serial = "SN2"  # a different unit answers now
    clock["now"] = base + timedelta(seconds=6)
    runner._tick()
    assert runner.state == S_PAUSED and len(host.requests) == 3, "the second pass did not start"
    status = runner.status()
    assert "not the sensor the collection was planned for" in status["pause_reason"] and "SN2" in status["pause_reason"] and "SN1" in status["pause_reason"]
    assert status["preflight_level"] == cq.LEVEL_ERROR and status["preflight_pending"] is False
    assert any(m.startswith("Check:") for _l, m in logs)
    preflight = runner.last_preflight()
    assert preflight is not None and [c.name for c in preflight.blocking()] == ["identity_match"]
    second_round = runner.session.current_round_id
    assert second_round != first_round and runner.session.preflight[second_round]["ok"] is False
    # The right unit is back: Resume repeats the check and the pass starts.
    host._sensor_info.serial = "SN1"
    runner.resume()
    assert runner.state == S_RUNNING and len(host.requests) == 4
    assert runner.session.preflight[second_round]["ok"] is True
    assert runner.session.sensor.get("clock_source") == "demo" and runner.session.sensor.get("clock_offset_s") == 0.0
    assert any("Pre-collection check passed" in m for _l, m in logs)


def test_runner_check_passes_on_a_matching_sensor_and_records_identity_and_clock(qapp, tmp_path: Path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(sensor_model="R40-8", sensor_serial="SN1"))
    assert runner.state == S_RUNNING and len(host.requests) == 1
    preflight = runner.last_preflight()
    assert preflight is not None and preflight.ok
    names = {c.name for c in preflight.checks}
    assert {"identity", "identity_match", "sensor_clock", "sensor_storage", "local_storage", "destination", "plan"} <= names
    assert runner.session.sensor["serial"] == "SN1" and "clock_status" in runner.session.sensor
    # A second pass runs the check again (one record per pass).
    host.finish(host.requests[0])
    _pump(qapp)
    assert len(runner.session.preflight) == 1
    saved = json.loads((tmp_path / "config" / "cellular" / "session.json").read_text(encoding="utf-8")) if (tmp_path / "config" / "cellular" / "session.json").is_file() else None
    if saved is not None:
        assert saved["preflight"][runner.session.current_round_id]["ok"] is True


# ----------------------------------------------------------------------------- XDAT (the format in use on the R40-8)

# Header as written by an RFeye Node R40-8 (firmware 2.25-325) for 1.0 s at 10 MHz, sample count
# reduced so the test file stays small (the real one lists 15713248 samples = 62,852,992 bytes).
XHDR_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<xcom_header header_version="1.0" name="{stem}">
<device type="RFeye R40-8" ip="192.168.1.229" mac="00:1e:89:30:05:40"/>
<captures>
<capture name="rfeye300540" channel="1" center_frequency="7.08e+08" acq_scale_factor="2.31435e-13" acquisition_bandwidth="10000000" sample_rate="{rate}" start_capture="2026:308:19:25:18.001002104" stop_capture="2026:308:19:25:19.006649976" timestamp_source="NMEA"/>
</captures>
<data_files>
<data name="{stem}.xdat" channel_count="1" sample_resolution="16" iq_interleave="True" little_endian="True" protected="False" signed_type="True" data_encoding="int16" samples="{samples}"/>
</data_files>
<marker_files>
<marker name="{stem}.xmrk" format="XML" count="15"/>
</marker_files>
</xcom_header>
"""


def _write_xdat(folder: Path, stem: str = "file_192517_0001", *, samples: int = 20_000, rate: int = 20_000, tone_hz: float = 2_500.0, short_by: int = 0) -> Path:
    """A small XDAT recording (int16 interleaved I/Q tone) with its .xhdr and .xmrk."""
    folder.mkdir(parents=True, exist_ok=True)
    n = np.arange(samples, dtype=np.float64)
    iq = 0.5 * np.exp(2j * np.pi * tone_hz / rate * n)
    pcm = np.empty(samples * 2, dtype="<i2")
    pcm[0::2] = np.round(iq.real * 32767).astype("<i2")
    pcm[1::2] = np.round(iq.imag * 32767).astype("<i2")
    data = pcm.tobytes()
    if short_by:
        data = data[:-short_by]
    xdat = folder / f"{stem}.xdat"
    xdat.write_bytes(data)
    (folder / f"{stem}.xhdr").write_text(XHDR_TEMPLATE.format(stem=stem, rate=rate, samples=samples), encoding="utf-8")
    (folder / f"{stem}.xmrk").write_text("<markers/>", encoding="utf-8")
    return xdat


def test_xdat_duration_start_time_and_tuning_come_from_the_sensors_header(tmp_path: Path):
    from crfs_iq_recorder.capture_measure import classify, measure_parts
    from crfs_iq_recorder.xdat_header import inspect_xdat, parse_capture_time

    assert parse_capture_time("2026:308:19:25:18.001002104") == datetime(2026, 11, 4, 19, 25, 18, 1002, tzinfo=timezone.utc)
    assert parse_capture_time("") is None and parse_capture_time("not a time") is None
    xdat = _write_xdat(tmp_path)
    info = inspect_xdat(xdat)
    assert info.container == "XDAT" and info.frames == 20_000 and info.fmt.sample_rate == 20_000 and not info.problems
    assert info.meta.rf_frequency_hz == 708e6 and info.meta.bandwidth_hz == 10e6
    assert info.meta.signal_time == datetime(2026, 11, 4, 19, 25, 18, 1002, tzinfo=timezone.utc)
    # Measured like a WAVE part; the sidecars are not parts.
    measurement = measure_parts(None, [str(xdat), str(xdat.with_suffix(".xhdr")), str(xdat.with_suffix(".xmrk"))])
    assert len(measurement.parts) == 1 and measurement.duration_s == pytest.approx(1.0)
    assert measurement.signal_time == info.meta.signal_time and measurement.centre_hz == 708e6
    assert classify(requested_s=1.0, measurement=measurement, named_parts=[xdat.name]).result == "completed"
    # A .xdat the sensor has not finished (no .xhdr yet) is read again, never sized.
    xdat.with_suffix(".xhdr").unlink()
    pending = measure_parts(None, [str(xdat)])
    assert pending.duration_s is None and pending.parts[0].header_pending
    assert classify(requested_s=1.0, measurement=pending, named_parts=[xdat.name]).result == "unconfirmed"


def test_a_wave_part_the_node_is_still_writing_is_told_apart_from_a_finished_one(tmp_path: Path):
    from crfs_iq_recorder.capture_measure import measure_part
    from crfs_iq_recorder.iq_wav import crfs_style_wav_bytes

    final = crfs_style_wav_bytes(
        frames=4_000, sample_rate=8_000, center_hz=2.14e9, bandwidth_hz=60e6, signal_time="2026-11-05T19:46:42.000Z"
    )
    data_at = final.index(b"data", 12)
    data_size = int.from_bytes(final[data_at + 4:data_at + 8], "little")
    done = tmp_path / "iq_20261105_194642_0001.wav"
    done.write_bytes(final)
    assert not measure_part(None, str(done)).header_pending and not measure_part(None, str(done)).being_written

    # Node 100-18 mid-write: RIFF and data sizes are the ASCII placeholder "size", no meta trailer yet.
    writing = tmp_path / "iq_20261105_194643_0001.wav"
    writing.write_bytes(final[:4] + b"size" + final[8:data_at + 4] + b"size" + final[data_at + 8:data_at + 8 + data_size])
    part = measure_part(None, str(writing))
    assert part.header_pending and part.being_written

    # A finished file without the CRFS metadata: its sizes match the file, so it is not being written.
    bare = final[:data_at + 8 + data_size]
    no_meta = tmp_path / "iq_20261105_194644_0001.wav"
    no_meta.write_bytes(bare[:4] + (len(bare) - 8).to_bytes(4, "little") + bare[8:])
    part = measure_part(None, str(no_meta))
    assert part.header_pending and not part.being_written


def test_xdat_shorter_than_its_header_is_cut_short_and_an_unknown_layout_is_not_decoded(tmp_path: Path):
    from crfs_iq_recorder.iq_merge import MergeError
    from crfs_iq_recorder.xdat_header import inspect_xdat

    short = _write_xdat(tmp_path / "short", short_by=400)
    info = inspect_xdat(short)
    assert info.frames == 19_900 and any("truncated" in p for p in info.problems)
    quality = cq.validate_capture([short, short.with_suffix(".xhdr")], expected_duration_s=1.0)
    assert "truncated" in quality.flags and quality.level == cq.LEVEL_ERROR
    odd = _write_xdat(tmp_path / "odd", short_by=1)
    assert any("middle of a sample" in p for p in inspect_xdat(odd).problems)
    other = _write_xdat(tmp_path / "other")
    header = other.with_suffix(".xhdr")
    header.write_text(header.read_text(encoding="utf-8").replace('data_encoding="int16"', 'data_encoding="float32"').replace('sample_resolution="16"', 'sample_resolution="32"'), encoding="utf-8")
    with pytest.raises(MergeError, match="not decoded here"):
        inspect_xdat(other)
    quality = cq.validate_capture([other], expected_duration_s=1.0)
    assert quality.level == cq.LEVEL_NOT_CHECKED and "not_wave" in quality.flags


def test_xdat_quality_check_and_previews_use_the_header_for_an_absolute_axis(tmp_path: Path):
    pytest.importorskip("matplotlib")
    xdat = _write_xdat(tmp_path, tone_hz=2_500.0)
    quality = cq.validate_capture(
        [xdat, xdat.with_suffix(".xhdr"), xdat.with_suffix(".xmrk")],
        expected_duration_s=1.0, expected_center_hz=708_000_000, expected_bandwidth_hz=10_000_000,
    )
    assert quality.level == cq.LEVEL_OK and quality.flags == [] and len(quality.files) == 1
    assert quality.files[0].readable and quality.files[0].center_hz == 708e6 and quality.files[0].duration_s == pytest.approx(1.0)
    assert any("Kept with the recording" in n and ".xhdr" in n for n in quality.notes)
    axis = cq.describe_preview_axis([xdat], requested_start_hz=703 * MHZ, requested_stop_hz=713 * MHZ)
    assert axis["axis"].startswith("absolute") and axis["center_hz"] == 708e6 and axis["sample_rate_hz"] == 20_000.0
    result = build_preview_from_path(xdat, requested_start_hz=703 * MHZ, requested_stop_hz=713 * MHZ)
    assert "XDAT" in result.layout and result.sample_rate == 20_000 and "708" in result.caption
    out = cq.write_preview([xdat], tmp_path / "p.png", requested_start_hz=703 * MHZ, requested_stop_hz=713 * MHZ)
    assert out.is_file() and out.stat().st_size > 1000


def test_xdat_local_copy_structure_is_verified_against_the_header_or_the_sensor_measurement(qapp, tmp_path: Path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    xdat = _write_xdat(tmp_path / "dl")
    item = {"name": xdat.name, "remote_path": f"{REMOTE_DIR}/{xdat.name}", "size": xdat.stat().st_size}
    problem, stages = runner._verify_local_copy(None, item, xdat)
    assert problem == "" and stages[V_SIZE] == V_OK and stages[V_STRUCTURE] == V_OK
    # Without the local .xhdr: compared with what was read on the sensor at confirmation.
    xdat.with_suffix(".xhdr").unlink()

    class State:
        summary = {"measurement": {"parts": [{"path": item["remote_path"], "duration_s": 1.0, "sample_rate_hz": 20_000.0}]}}

    problem, stages = runner._verify_local_copy(State(), item, xdat)
    assert problem == "" and stages[V_STRUCTURE] == V_OK
    State.summary["measurement"]["parts"][0]["duration_s"] = 1.5
    problem, stages = runner._verify_local_copy(State(), item, xdat)
    assert stages[V_STRUCTURE] == V_FAILED and "30,000" in problem
    problem, stages = runner._verify_local_copy(None, item, xdat)
    assert stages[V_STRUCTURE] == V_NA and problem == ""
    assert "structure not available" in verification_text(stages)
    sidecar = xdat.with_suffix(".xmrk")
    problem, stages = runner._verify_local_copy(None, {"name": sidecar.name, "size": sidecar.stat().st_size}, sidecar)
    assert problem == "" and stages[V_STRUCTURE] == "not_applicable"


def test_results_sheet_in_an_open_log_workbook_is_added_through_excel(tmp_path: Path, monkeypatch):
    """The Excel log workbook is open in Excel: the results sheet is created through the open
    window (never by rewriting the file underneath Excel); a busy Excel keeps the row queued."""
    from openpyxl import Workbook

    from crfs_iq_recorder import cellular_excel
    from crfs_iq_recorder.cellular_excel import RESULTS_HEADERS, RESULTS_SHEET, ensure_results_sheet

    path = tmp_path / "log.xlsx"
    wb = Workbook()
    wb.active.title = "Tactical Radio"
    wb.save(path)

    class Store:
        backups = 0

        def ensure_backup(self, _p):
            self.backups += 1

    store = Store()
    added: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_workbook_state", lambda _p: "open")
    monkeypatch.setattr("crfs_iq_recorder.excel_com.add_worksheet_com", lambda p, sheet, headers: added.append((sheet, list(headers))) or True)
    assert ensure_results_sheet(store, path, RESULTS_SHEET) is True
    assert added == [(RESULTS_SHEET, list(RESULTS_HEADERS))] and store.backups == 1
    from openpyxl import load_workbook

    assert RESULTS_SHEET not in load_workbook(path, read_only=True).sheetnames, "the file was not rewritten behind Excel"
    monkeypatch.setattr("crfs_iq_recorder.excel_com.add_worksheet_com", lambda p, sheet, headers: False)
    with pytest.raises(ExcelBusyError, match="open in Excel"):
        ensure_results_sheet(store, path, RESULTS_SHEET)
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_workbook_state", lambda _p: "busy")
    with pytest.raises(ExcelBusyError, match="busy"):
        ensure_results_sheet(store, path, RESULTS_SHEET)
    assert cellular_excel is not None
