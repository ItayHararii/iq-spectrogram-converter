"""Spectrograms of verified downloads: background job, queue, Excel thumbnails, links."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
import pytest
import soundfile as sf
from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from crfs_iq_recorder import openpyxl_links
from crfs_iq_recorder import spectrogram_job as job_mod
from crfs_iq_recorder import spectrogram_queue as sq
from crfs_iq_recorder.cellular_workbook import (
    WorkbookQueue,
    _ComSheet,
    ensure_backup,
    image_key,
    layout_from_import,
    plan_rows_from_import,
    write_results_openpyxl,
)
from crfs_iq_recorder.collection_catalog import HEADER_IQ, HEADER_SPECTROGRAM
from crfs_iq_recorder.excel_images import (
    KIND_LOG,
    LOCAL_TIP,
    SHARED_TIP,
    THUMB_WIDTH_PT,
    GAP_PT,
    ImageOutcome,
    BookResult,
    flush_images,
    link_for,
    part_slot,
    resolve_column,
    shape_name,
)
from crfs_iq_recorder.excel_log import ExcelLogStore
from crfs_iq_recorder.excel_writer import ExcelBusyError

EMU_PER_PT = 12700
STEM = "iq_20260101_120000"


# ----------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def _own_calibration(tmp_path, monkeypatch):
    """The in-process conversions set the converter's timing file; keep it in the test folder."""
    monkeypatch.setenv(job_mod.CALIBRATION_ENV, str(tmp_path / "eta.json"))
    monkeypatch.delenv(job_mod.SETTINGS_ENV, raising=False)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def write_iq(path: Path, *, fs: int = 1_000_000, seconds: float = 0.05, tone_hz: float = 150_000.0, stereo: bool = True) -> Path:
    n = int(fs * seconds)
    t = np.arange(n) / fs
    iq = 0.5 * np.exp(2j * np.pi * tone_hz * t) + 0.001 * (np.random.default_rng(3).standard_normal(n))
    data = np.column_stack((iq.real, iq.imag)).astype(np.float32) if stereo else iq.real.astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, fs, subtype="FLOAT")
    return path


def append_crfs_meta(path: Path, centre_hz: float, bandwidth_hz: float, fs: int) -> None:
    xml = (
        f'<?xml version="1.0"?><signalinfo><base><rf_frequency>{centre_hz:g}</rf_frequency>'
        f"<bandwidth>{bandwidth_hz:g}</bandwidth><data_type>complex</data_type>"
        f"<sample_rate>{fs:g}</sample_rate></base><extended><receiver>rfeye100400</receiver></extended></signalinfo>"
    ).encode("utf-8")
    pad = b"\x00" if len(xml) % 2 else b""
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        fh.write(b"meta" + struct.pack("<I", len(xml)) + xml + pad)
        size = fh.tell()
        fh.seek(4)
        fh.write(struct.pack("<I", size - 8))


def write_manifest(folder: Path, stem: str, start_hz: float, stop_hz: float) -> None:
    data = {
        "schema": 1,
        "kind": "cellular_capture",
        "identifiers": {"recording_stem": stem},
        "allocation": {"requested_start_hz": start_hz, "requested_stop_hz": stop_hz},
    }
    (folder / f"{stem}.cellular.json").write_text(json.dumps(data), encoding="utf-8")


def crfs_wav(folder: Path, name: str, *, manifest: bool = True) -> Path:
    wav = write_iq(folder / name)
    append_crfs_meta(wav, 1_842_500_000.0, 800_000.0, 1_000_000)
    if manifest:
        write_manifest(folder, name.rsplit("_", 1)[0], 1_842_300_000, 1_842_700_000)
    return wav


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_png(path: Path, width: int = 320, height: int = 160, colour=(20, 90, 200)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), colour).save(path)
    return path


def local_links(png):
    return link_for(png, mappings=[])


def job_for(wav: Path, tmp_path: Path, **extra) -> dict:
    return {"id": "t1", "wav": str(wav), "history": str(tmp_path / "none.json"), "settings": dict(job_mod.DEFAULT_SETTINGS), **extra}


class FakeProc:
    def __init__(self, code=None):
        self.code = code
        self.killed = False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9

    def wait(self, timeout=None):
        return self.code


def inline_launcher(calls: list | None = None):
    """Runs the job in this process (same entry point as the child) and hands back a finished 'process'."""

    def launch(job_file: Path):
        if calls is not None:
            calls.append(Path(job_file))
        return FakeProc(job_mod.run_job(job_file))

    return launch


def service(tmp_path: Path, *, queue=None, store=None, launcher=None, logs=None, flusher=None):
    queue = queue if queue is not None else WorkbookQueue(tmp_path / "config" / "workbook_queue.json")
    logs = logs if logs is not None else []
    svc = sq.SpectrogramService(
        None,
        config_dir=tmp_path / "config",
        workbook_queue=lambda: queue,
        excel_store=(lambda: store) if store is not None else None,
        log=lambda message, level="info": logs.append((level, message)),
        history_path=lambda: tmp_path / "none.json",
        launcher=launcher or inline_launcher(),
        image_flusher=flusher or (lambda q, **kw: flush_images(q, links=local_links, **kw)),
        settings=lambda: (dict(job_mod.DEFAULT_SETTINGS), "converter defaults"),
    )
    return svc, queue, logs


def run_all(svc, limit: int = 20) -> None:
    for _ in range(limit):
        svc._pump()
        if svc._proc is None:
            return
        svc._check_child()


def log_workbook(path: Path, rows: list[tuple[str, str]], *, extra_headers=()) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Log"
    ws.append(["Collection Event", HEADER_IQ, *extra_headers])
    ws["A1"].font = Font(bold=True)
    ws["B1"].font = Font(bold=True)
    for event, files in rows:
        ws.append([event, files])
    wb.save(path)
    wb.close()
    return path


def log_store(tmp_path: Path, book: Path, groups: dict[str, list[str]], sheet: str = "Log") -> ExcelLogStore:
    store = ExcelLogStore(tmp_path / "excel_log_store.json")
    store.groups = {key: {"filenames": names, "durations": {}, "workbook": str(book), "worksheet": sheet} for key, names in groups.items()}
    store.save()
    return store


def image_item(book: Path, file: str, thumb: Path, png: Path, *, sheet: str = "Log", row_hint: int = 0) -> dict:
    return {
        "kind": KIND_LOG, "workbook": str(book), "sheet": sheet, "header_row": 1, "iq_header": HEADER_IQ,
        "iq_col": 0, "row_hint": row_hint, "file": file, "thumb": str(thumb), "png": str(png),
        "thumb_stamp": sq.file_stamp(thumb),
    }


def close_book(wb) -> None:
    """openpyxl's close() leaves the macro archive of a keep_vba workbook open."""
    if getattr(wb, "vba_archive", None) is not None:
        wb.vba_archive.close()
    wb.close()


def pictures(path: Path, sheet: str = "Log") -> list[tuple[str, int, int, int, tuple | None]]:
    """(name, 0-based row, 0-based column, column offset in EMU, click link) of every picture."""
    wb = openpyxl_links.load_workbook_keeping_links(path)
    try:
        out = []
        for image in wb[sheet]._images:
            start = image.anchor._from
            out.append((openpyxl_links.picture_name(image), start.row, start.col, start.colOff, getattr(image, "_crfs_hlinkClick", None)))
        return sorted(out, key=lambda p: (p[1], p[2], p[3], p[0]))
    finally:
        wb.close()


def uri_path(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


def add_fake_vba(xlsx: Path, xlsm: Path, payload: bytes) -> Path:
    """An .xlsm made from an .xlsx: a vbaProject part, its relationship and content types."""
    with zipfile.ZipFile(xlsx) as src, zipfile.ZipFile(xlsm, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "[Content_Types].xml":
                text = data.decode("utf-8").replace(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                ).replace("</Types>", '<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>')
                data = text.encode("utf-8")
            elif info.filename == "xl/_rels/workbook.xml.rels":
                data = data.decode("utf-8").replace(
                    "</Relationships>",
                    '<Relationship Id="rIdVba1" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>',
                ).encode("utf-8")
            dst.writestr(info, data)
        dst.writestr("xl/vbaProject.bin", payload)
    return xlsm


def add_zip_part(path: Path, name: str, data: bytes) -> None:
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, data)


# ----------------------------------------------------------------------------- the conversion job
def test_job_writes_png_and_thumbnail_in_spectrograms_folder_and_leaves_the_wav_alone(tmp_path):
    folder = tmp_path / "rec"
    wav = crfs_wav(folder, f"{STEM}_0001.wav")
    before, stat = sha(wav), wav.stat()
    result = job_mod.convert(job_for(wav, tmp_path), cancel_event=threading.Event())
    assert result["ok"], result
    assert Path(result["png"]) == folder / "Spectrograms" / f"{STEM}_0001.png"
    assert Path(result["thumb"]) == folder / "Spectrograms" / f"{STEM}_0001.thumb.png"
    assert Path(result["png"]).is_file() and Path(result["thumb"]).is_file()
    assert result["absolute_axis"] and result["centre_hz"] == 1_842_500_000.0
    assert result["request_status"] == "matched" and "requested" in result["marker_kinds"], result
    assert result["wav_unchanged"] and sha(wav) == before and wav.stat().st_mtime_ns == stat.st_mtime_ns
    assert sorted(p.name for p in folder.iterdir()) == sorted(["Spectrograms", wav.name, f"{STEM}.cellular.json"])


def test_job_without_request_or_metadata_draws_no_invented_markers(tmp_path):
    wav = write_iq(tmp_path / "rec" / f"{STEM}_0001.wav")
    before = sha(wav)
    result = job_mod.convert(job_for(wav, tmp_path), cancel_event=threading.Event())
    assert result["ok"], result
    assert not result["absolute_axis"] and result["centre_hz"] is None
    assert "requested" not in result["marker_kinds"] and result["request_status"] != "matched"
    assert "centre unknown" in result["title"].casefold() or "baseband" in result["title"].casefold(), result["title"]
    assert sha(wav) == before


def test_job_marks_the_requested_range_from_the_recorder_history(tmp_path):
    wav = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav", manifest=False)
    history = tmp_path / "recordings.json"
    history.write_text(json.dumps([{
        "iq_stem": STEM, "start_hz": 1_842_300_000, "end_hz": 1_842_700_000, "center_hz": 1_842_500_000,
        "started_at": "2026-01-01T12:00:00",
    }]), encoding="utf-8")
    result = job_mod.convert(job_for(wav, tmp_path, history=str(history)), cancel_event=threading.Event())
    assert result["ok"] and result["request_status"] == "matched" and "requested" in result["marker_kinds"], result


def test_job_reports_a_mono_wav_as_not_iq(tmp_path):
    wav = write_iq(tmp_path / "rec" / "mono_0001.wav", stereo=False)
    result = job_mod.convert(job_for(wav, tmp_path), cancel_event=threading.Event())
    assert not result["ok"] and any(token in result["error"] for token in sq._NOT_IQ), result


def test_job_runs_in_a_child_process_through_the_command_line(tmp_path):
    wav = crfs_wav(tmp_path / "rec", f"{STEM}_0002.wav")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    job_file = jobs / "abc.json"
    job_file.write_text(json.dumps(job_for(wav, tmp_path, calibration=str(tmp_path / "eta.json"))), encoding="utf-8")
    argv, cwd = sq.job_command(job_file)
    assert argv[-2:] == [job_mod.JOB_FLAG, str(job_file)]
    done = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    result = json.loads(job_mod.job_paths(job_file)[0].read_text(encoding="utf-8"))
    assert result["ok"] and result["pid"] != os.getpid() and Path(result["thumb"]).is_file()
    assert result["peak_working_set_mb"] > 0


def test_cancel_file_stops_a_running_job(tmp_path):
    wav = crfs_wav(tmp_path / "rec", f"{STEM}_0003.wav")
    job_file = tmp_path / "j.json"
    job_file.write_text(json.dumps(job_for(wav, tmp_path)), encoding="utf-8")
    job_mod.job_paths(job_file)[1].write_text("cancel", encoding="utf-8")
    event = threading.Event()
    event.set()
    result = job_mod.convert(json.loads(job_file.read_text(encoding="utf-8")), cancel_event=event)
    assert not result["ok"] and result["cancelled"], result


def test_converter_settings_follow_the_converter_window_and_reject_bad_values(tmp_path, monkeypatch):
    good = tmp_path / ".iq_gui_settings.json"
    good.write_text(json.dumps({"rbw": "30000", "cmap": "Viridis", "db_auto": False, "db_vmin": -120, "db_vmax": "-20",
                                "range_markers": False, "freq_smooth": True}), encoding="utf-8")
    settings, source = job_mod.converter_settings(good)
    assert source == str(good)
    assert settings == {"rbw": "30000", "cmap": "viridis", "freq_smooth": True, "db_auto": False,
                        "db_vmin": "-120", "db_vmax": "-20", "range_markers": False}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"rbw": "abc", "cmap": "rainbow-ish", "db_auto": "yes"}), encoding="utf-8")
    assert job_mod.converter_settings(bad)[0] == job_mod.DEFAULT_SETTINGS
    monkeypatch.setenv(job_mod.SETTINGS_ENV, str(good))
    assert job_mod.find_converter_settings() == good
    monkeypatch.setenv(job_mod.SETTINGS_ENV, str(tmp_path / "missing.json"))
    assert job_mod.converter_settings() == (job_mod.DEFAULT_SETTINGS, "converter defaults")
    monkeypatch.delenv(job_mod.SETTINGS_ENV)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert job_mod.find_converter_settings(nested) == good


# ----------------------------------------------------------------------------- the queue
def test_queue_converts_one_at_a_time_and_never_twice(tmp_path, app):
    folder = tmp_path / "rec"
    a, b = crfs_wav(folder, f"{STEM}_0001.wav"), crfs_wav(folder, f"{STEM}_0002.wav")
    calls: list = []
    svc, _queue, logs = service(tmp_path, launcher=inline_launcher(calls))
    try:
        assert svc.request(a) and svc.request(b)
        assert not svc.request(a), "already queued"
        assert not svc.request(folder / f"{STEM}.cellular.json"), "not a WAV"
        run_all(svc)
        assert len(calls) == 2
        assert [j["state"] for j in svc.store.jobs] == ["done", "done"]
        assert ("info", f"Generating spectrogram: {a.name}.") in logs
        assert ("info", f"Spectrogram saved: {a.name}.") in logs  # in no Excel row
        assert not svc.request(a), "converted and unchanged"
        run_all(svc)
        assert len(calls) == 2
        # The file is replaced (new download): converted again.
        write_iq(a, seconds=0.06)
        assert svc.request(a)
        run_all(svc)
        assert len(calls) == 3 and len(svc.store.jobs) == 2
    finally:
        svc.shutdown()


def test_queue_survives_a_restart_and_adopts_a_finished_child(tmp_path, app):
    folder = tmp_path / "rec"
    a, b = crfs_wav(folder, f"{STEM}_0001.wav"), crfs_wav(folder, f"{STEM}_0002.wav")

    def finished_but_unseen(job_file):
        job_mod.run_job(job_file)  # the child finished just before the app went away
        return FakeProc(None)

    svc, _q, _logs = service(tmp_path, launcher=finished_but_unseen)
    svc.request(a)
    svc.request(b)
    svc._pump()
    assert [j["state"] for j in svc.store.jobs] == ["running", "pending"]
    svc._alive = False  # the app ends without shutdown (crash)
    for timer in (svc._poll, svc._retry, svc._images_timer, svc._tick):
        timer.stop()
    calls: list = []
    again, _q2, _logs2 = service(tmp_path, launcher=inline_launcher(calls))
    try:
        states = {j["name"]: j["state"] for j in again.store.jobs}
        assert states == {a.name: "done", b.name: "pending"}, "result adopted, the rest resumes"
        run_all(again)
        assert len(calls) == 1 and {j["state"] for j in again.store.jobs} == {"done"}
    finally:
        again.shutdown()


def test_interrupted_child_is_run_again_after_a_restart(tmp_path, app):
    a = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav")
    svc, _q, _logs = service(tmp_path, launcher=lambda job_file: FakeProc(None))
    svc.request(a)
    svc._pump()
    assert svc.store.jobs[0]["state"] == "running"
    svc._alive = False
    for timer in (svc._poll, svc._retry, svc._images_timer, svc._tick):
        timer.stop()
    calls: list = []
    again, _q2, _logs2 = service(tmp_path, launcher=inline_launcher(calls))
    try:
        assert again.store.jobs[0]["state"] == "pending"
        run_all(again)
        assert len(calls) == 1 and again.store.jobs[0]["state"] == "done"
    finally:
        again.shutdown()


def test_a_failing_conversion_is_retried_then_reported_once(tmp_path, app):
    a = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav")
    svc, _q, logs = service(tmp_path, launcher=lambda job_file: FakeProc(1))
    try:
        svc.request(a)
        for attempt in range(1, sq.MAX_ATTEMPTS + 1):
            job = svc.store.jobs[0]
            job.pop("not_before", None)  # the retry delay has passed
            run_all(svc)
            assert job["attempts"] == attempt
        assert job["state"] == "failed" and "without a result" in job["error"]
        failures = [m for level, m in logs if level == "error"]
        assert failures == [f"Spectrogram failed: {a.name}. See details."]
        assert sq.RETRY_DELAYS_S[0] > 0
    finally:
        svc.shutdown()


def test_a_wav_that_is_not_iq_is_skipped_without_a_failure(tmp_path, app):
    mono = write_iq(tmp_path / "rec" / "mono_0001.wav", stereo=False)
    svc, _q, logs = service(tmp_path)
    try:
        svc.request(mono)
        run_all(svc)
        assert svc.store.jobs[0]["state"] == "skipped"
        assert not [m for level, m in logs if level == "error"]
    finally:
        svc.shutdown()


def test_a_file_replaced_while_it_converts_is_converted_again(tmp_path, app):
    a = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav")
    procs: list[FakeProc] = []

    def slow(job_file):
        procs.append(FakeProc(None))
        return procs[-1]

    svc, _q, _logs = service(tmp_path, launcher=slow)
    try:
        svc.request(a)
        svc._pump()
        write_iq(a, seconds=0.07)
        assert svc.request(a)
        procs[-1].code = 0
        svc._check_child()
        job = svc.store.jobs[0]
        assert job["state"] == "pending" and job["stamp"] == sq.file_stamp(a)
    finally:
        svc.shutdown()


def test_shutdown_cancels_the_child_and_keeps_the_job(tmp_path, app, monkeypatch):
    monkeypatch.setattr(sq, "CANCEL_GRACE_S", 0.1)
    a = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav")
    procs: list[FakeProc] = []
    seen_cancel: list[bool] = []

    class Stubborn(FakeProc):
        def poll(self):
            if svc._proc_job is not None:
                seen_cancel.append(job_mod.job_paths(svc._job_file(svc._proc_job))[1].is_file())
            return self.code

    def launch(job_file):
        procs.append(Stubborn(None))
        return procs[-1]

    svc, _q, _logs = service(tmp_path, launcher=launch)
    svc.request(a)
    svc._pump()
    svc.shutdown()
    assert procs[0].killed and any(seen_cancel)
    assert svc.store.jobs[0]["state"] == "pending"
    assert not list((tmp_path / "config" / sq.JOBS_FOLDER).glob("*"))


def test_child_starts_below_normal_priority_without_a_window(tmp_path, app, monkeypatch):
    seen = {}

    class Popen:
        def __init__(self, argv, **kwargs):
            seen.update(kwargs, argv=argv)
            self._handle = 0

        def poll(self):
            return 0

    monkeypatch.setattr(sq.subprocess, "Popen", Popen)
    svc, _q, _logs = service(tmp_path)
    try:
        job_file = tmp_path / "config" / sq.JOBS_FOLDER / "x.json"
        job_file.parent.mkdir(parents=True)
        svc._launch_child(job_file)
        assert seen["creationflags"] == 0x4000 | 0x08000000
        assert seen["stdin"] == subprocess.DEVNULL and job_mod.JOB_FLAG in seen["argv"]
    finally:
        svc.shutdown()


# ----------------------------------------------------------------------------- rows and pictures
def test_pictures_reach_the_right_rows_whatever_order_files_finish_in(tmp_path, app):
    folder = tmp_path / "rec"
    a, b = crfs_wav(folder, f"{STEM}_0001.wav"), crfs_wav(folder, "iq_20260101_130000_0001.wav")
    book = log_workbook(tmp_path / "log.xlsx", [("first", a.name), ("second", b.name)])
    store = log_store(tmp_path, book, {"g1": [a.name], "g2": [b.name]})
    svc, queue, logs = service(tmp_path, store=store)
    try:
        svc.request(b)  # the later row finishes first
        svc.request(a)
        run_all(svc)
        assert queue.image_count() == 2
        svc._flush_images()
        assert ("info", "Spectrograms added: 2.") in logs
        pics = pictures(book)
        assert [(p[0], p[1], p[2]) for p in pics] == [(shape_name(a.name), 1, 2), (shape_name(b.name), 2, 2)]
        for _name, _row, _col, _off, link in pics:
            assert link and uri_path(link[0]).is_file() and uri_path(link[0]).suffix == ".png"
        wb = load_workbook(book)
        assert wb["Log"]["C1"].value == HEADER_SPECTROGRAM and wb["Log"]["C1"].font.bold
        wb.close()
        # The rows are sorted by hand, then file a is downloaded again: its picture follows its row, once.
        wb = load_workbook(book)
        ws = wb["Log"]
        ws["A2"], ws["B2"], ws["A3"], ws["B3"] = "second", b.name, "first", a.name
        wb.save(book)
        wb.close()
        write_iq(a, seconds=0.06)
        append_crfs_meta(a, 1_842_500_000.0, 800_000.0, 1_000_000)
        assert svc.request(a)
        run_all(svc)
        svc._flush_images()
        names = [(p[0], p[1]) for p in pictures(book)]
        assert names.count((shape_name(a.name), 2)) == 1 and len(names) == 2
    finally:
        svc.shutdown()


def test_a_multi_part_row_gets_one_picture_per_part_side_by_side(tmp_path):
    names = [f"{STEM}_0001.wav", f"{STEM}_0002.wav", f"{STEM}_0003.wav"]
    book = log_workbook(tmp_path / "log.xlsx", [("event", "\n".join(names))])
    queue = WorkbookQueue(tmp_path / "q.json")
    for name in reversed(names):  # finished in any order
        png = make_png(tmp_path / "Spectrograms" / name.replace(".wav", ".png"), 800, 400)
        thumb = make_png(tmp_path / "Spectrograms" / name.replace(".wav", ".thumb.png"))
        queue.enqueue_image(image_item(book, name, thumb, png))
    report = flush_images(queue, links=local_links)
    assert len(report.written) == 3 and queue.image_count() == 0
    pics = pictures(book)
    assert [p[0] for p in pics] == [shape_name(n) for n in names]
    assert {p[1] for p in pics} == {1} and {p[2] for p in pics} == {2}
    offsets = [p[3] for p in pics]
    step = round((THUMB_WIDTH_PT + GAP_PT) * EMU_PER_PT)
    assert offsets[1] - offsets[0] == pytest.approx(step, abs=2) and offsets[2] - offsets[1] == pytest.approx(step, abs=2)
    assert part_slot(names[2], names) == (2, 3)
    wb = load_workbook(book)
    ws = wb["Log"]
    assert ws.row_dimensions[2].height >= 2 * 3 + 48 and ws.column_dimensions["C"].width > 40
    wb.close()


def test_retries_and_restarts_never_duplicate_a_picture(tmp_path):
    name = f"{STEM}_0001.wav"
    book = log_workbook(tmp_path / "log.xlsx", [("event", name)])
    png = make_png(tmp_path / "S" / "a.png", 800, 400)
    thumb = make_png(tmp_path / "S" / "a.thumb.png")
    queue_file = tmp_path / "state" / "workbook_queue.json"
    queue = WorkbookQueue(queue_file)
    item = image_item(book, name, thumb, png)
    assert queue.enqueue_image(item) and queue.enqueue_image(item), "a repeat replaces the queued entry"
    assert queue.image_count() == 1
    flush_images(queue, links=local_links)
    assert not WorkbookQueue(queue_file).enqueue_image(item), "already in the sheet (after a restart too)"
    assert WorkbookQueue(queue_file).image_done(image_key(book, "Log", name))
    # A new thumbnail for the same file replaces the picture.
    make_png(thumb, colour=(200, 30, 30))
    os.utime(thumb, ns=(thumb.stat().st_atime_ns, thumb.stat().st_mtime_ns + 10_000_000))
    queue = WorkbookQueue(queue_file)
    assert queue.enqueue_image(image_item(book, name, thumb, png))
    flush_images(queue, links=local_links)
    assert [p[0] for p in pictures(book)] == [shape_name(name)]


def test_waiting_pictures_survive_a_restart(tmp_path):
    book = log_workbook(tmp_path / "log.xlsx", [("event", "other.wav")])
    png, thumb = make_png(tmp_path / "a.png"), make_png(tmp_path / "a.thumb.png")
    queue_file = tmp_path / "state" / "workbook_queue.json"
    queue = WorkbookQueue(queue_file)
    queue.enqueue_image(image_item(book, "late_0001.wav", thumb, png))
    report = flush_images(queue, links=local_links)
    assert report.waiting == 1 and not report.written
    restarted = WorkbookQueue(queue_file)
    pending = restarted.pending_images()
    assert len(pending) == 1 and pending[0]["last_error"] == "its row is not in the sheet yet" and pending[0]["attempts"] == 0
    # The row arrives later; the next flush places it.
    wb = load_workbook(book)
    wb["Log"].append(["late", "late_0001.wav"])
    wb.save(book)
    wb.close()
    assert len(flush_images(restarted, links=local_links).written) == 1


def test_pictures_wait_while_their_row_is_still_queued(tmp_path):
    name = f"{STEM}_0001.wav"
    book = log_workbook(tmp_path / "log.xlsx", [("event", name)])
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue_image(image_item(book, name, make_png(tmp_path / "a.thumb.png"), make_png(tmp_path / "a.png")))
    stamp = book.stat().st_mtime_ns
    report = flush_images(queue, links=local_links, row_queued=lambda item: True)
    assert report.waiting == 1 and queue.image_count() == 1 and book.stat().st_mtime_ns == stamp


def test_busy_excel_keeps_pictures_queued_and_they_land_when_it_is_free(tmp_path, app):
    name = f"{STEM}_0001.wav"
    book = log_workbook(tmp_path / "log.xlsx", [("event", name)])
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue_image(image_item(book, name, make_png(tmp_path / "a.thumb.png"), make_png(tmp_path / "a.png")))
    stamp = book.stat().st_mtime_ns
    report = flush_images(queue, excel_state=lambda p: "busy", links=local_links)
    assert "busy" in report.busy and queue.image_count() == 1 and queue.pending_images()[0]["attempts"] == 0
    calls: list[str] = []

    def cell_edit(file, items, links):
        calls.append("edit")
        raise ExcelBusyError("Excel is in cell-edit mode.")

    report = flush_images(queue, excel_state=lambda p: "open", links=local_links, com_writer=cell_edit)
    assert report.busy and queue.image_count() == 1 and calls == ["edit"]

    def com_ok(file, items, links):
        return BookResult(outcomes=[ImageOutcome(key=i["key"], status="written", row=2, column=3) for i in items])

    report = flush_images(queue, excel_state=lambda p: "open", links=local_links, com_writer=com_ok)
    assert len(report.written) == 1 and queue.image_count() == 0
    assert book.stat().st_mtime_ns == stamp, "the open workbook is written by Excel, not on disk"
    # Through the service: a busy Excel schedules a retry, nothing is lost.
    queue.enqueue_image(dict(image_item(book, "b_0001.wav", make_png(tmp_path / "b.thumb.png"), make_png(tmp_path / "b.png"))))
    svc, _q, logs = service(tmp_path, queue=queue, flusher=lambda q, **kw: flush_images(q, excel_state=lambda p: "busy", links=local_links, **kw))
    try:
        svc._flush_images()
        assert svc._images_timer.isActive() and svc._images_timer.remainingTime() > 10_000
        assert queue.image_count() == 1 and not [m for level, m in logs if level == "error"]
    finally:
        svc.shutdown()


def test_closed_workbook_keeps_formulas_formats_macros_and_other_pictures(tmp_path):
    openpyxl_links.install()
    other_png = make_png(tmp_path / "logo.png", 64, 64, (0, 0, 0))
    wb = Workbook()
    ws = wb.active
    ws.title = "Log"
    ws.append(["Collection Event", HEADER_IQ, None, None, "Total"])
    ws["A1"].font = Font(bold=True, color="FF0000")
    ws["B1"].font = Font(bold=True, color="FF0000")
    ws["B1"].fill = PatternFill("solid", fgColor="FFFF00")
    names = [f"{STEM}_0001.wav", "iq_20260101_130000_0001.wav"]
    ws.append(["first", names[0], None, None, "=1+2"])
    ws.append(["second", names[1], None, None, "=E2*2"])
    validation = DataValidation(type="list", formula1='"first,second"')
    validation.add("A2:A50")
    ws.add_data_validation(validation)
    ws.conditional_formatting.add("E2:E50", CellIsRule(operator="greaterThan", formula=["2"], fill=PatternFill("solid", fgColor="00FF00")))
    ws.freeze_panes = "A2"
    from openpyxl.drawing.image import Image as XLImage

    logo = XLImage(str(other_png))
    logo.anchor = "G2"
    openpyxl_links.set_picture_link(logo, other_png.resolve().as_uri(), tooltip="logo", name="Company logo", descr="logo")
    ws.add_image(logo)
    wb.create_sheet("Notes")["A1"] = "manual note"
    plain = tmp_path / "plain.xlsx"
    wb.save(plain)
    wb.close()
    vba = os.urandom(3000)
    book = add_fake_vba(plain, tmp_path / "log.xlsm", vba)
    queue = WorkbookQueue(tmp_path / "q.json")
    for name in names:
        stem = name[:-4]
        queue.enqueue_image(image_item(book, name, make_png(tmp_path / "Spectrograms" / f"{stem}.thumb.png"), make_png(tmp_path / "Spectrograms" / f"{stem}.png", 800, 400)))
    original = sha(book)
    report = flush_images(queue, links=local_links, backup=lambda f: ensure_backup(queue, f))
    assert len(report.written) == 2 and report.columns_created == [(str(book), "Log", 3)], report
    backups = list(tmp_path.glob("log.crfs-backup-*.xlsm"))
    assert len(backups) == 1 and sha(backups[0]) == original
    with zipfile.ZipFile(book) as archive:
        assert archive.read("xl/vbaProject.bin") == vba
        assert "macroEnabled" in archive.read("[Content_Types].xml").decode("utf-8")
        listed = archive.namelist()
        assert len(listed) == len(set(listed))
    wb = load_workbook(book, keep_vba=True)
    ws = wb["Log"]
    assert ws["C1"].value == HEADER_SPECTROGRAM and ws["C1"].font.bold and ws["C1"].fill.fgColor.rgb == ws["B1"].fill.fgColor.rgb
    assert (ws["E2"].value, ws["E3"].value) == ("=1+2", "=E2*2")
    assert len(ws.data_validations.dataValidation) == 1 and str(ws.data_validations.dataValidation[0].sqref) == "A2:A50"
    assert [str(r.sqref) for r in ws.conditional_formatting] == ["E2:E50"]
    assert ws.freeze_panes == "A2" and wb["Notes"]["A1"].value == "manual note"
    close_book(wb)
    pics = pictures(book)
    assert [p[0] for p in pics] == [shape_name(names[0]), "Company logo", shape_name(names[1])]
    for name, _row, _col, _off, link in pics:
        assert link and uri_path(link[0]).is_file(), (name, link)
    logo_link = next(p[4] for p in pics if p[0] == "Company logo")
    assert uri_path(logo_link[0]) == other_png.resolve()


def test_closed_workbook_that_needs_a_column_inserted_waits_for_excel(tmp_path, app):
    name = f"{STEM}_0001.wav"
    book = log_workbook(tmp_path / "log.xlsx", [("event", name)], extra_headers=("Notes",))
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue_image(image_item(book, name, make_png(tmp_path / "a.thumb.png"), make_png(tmp_path / "a.png")))
    before = sha(book)
    backups: list = []
    svc, _q, logs = service(tmp_path, queue=queue, flusher=lambda q, **kw: flush_images(q, links=local_links, **{**kw, "backup": backups.append}))
    try:
        svc._flush_images()
        svc._flush_images()
        assert sha(book) == before and queue.image_count() == 1
        assert "inserted" in queue.pending_images()[0]["last_error"]
        assert logs == [("info", "Spectrograms for log.xlsx are added when it is open in Excel. See details.")], "said once"
    finally:
        svc.shutdown()


def test_closed_workbook_with_form_controls_is_not_rewritten(tmp_path):
    name = f"{STEM}_0001.wav"
    book = log_workbook(tmp_path / "log.xlsx", [("event", name)])
    add_zip_part(book, "xl/ctrlProps/ctrlProp1.xml", b'<formControlPr xmlns="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" objectType="CheckBox"/>')
    assert openpyxl_links.closed_write_risks(book)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue_image(image_item(book, name, make_png(tmp_path / "a.thumb.png"), make_png(tmp_path / "a.png")))
    before = sha(book)
    made: list = []
    report = flush_images(queue, links=local_links, backup=made.append)
    assert report.deferred and report.deferred_books == ["log.xlsx"] and not made, "no backup, no write"
    assert sha(book) == before and queue.image_count() == 1


def test_the_spectrogram_column_is_created_once(tmp_path):
    names = [f"{STEM}_0001.wav", "iq_20260101_130000_0001.wav"]
    book = log_workbook(tmp_path / "log.xlsx", [("first", names[0]), ("second", names[1])])
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue_image(image_item(book, names[0], make_png(tmp_path / "a.thumb.png"), make_png(tmp_path / "a.png")))
    first = flush_images(queue, links=local_links)
    queue.enqueue_image(image_item(book, names[1], make_png(tmp_path / "b.thumb.png"), make_png(tmp_path / "b.png")))
    second = flush_images(queue, links=local_links)
    assert len(first.columns_created) == 1 and not second.columns_created
    wb = load_workbook(book)
    headers = [c.value for c in wb["Log"][1]]
    wb.close()
    assert headers.count(HEADER_SPECTROGRAM) == 1
    assert {p[2] for p in pictures(book)} == {2}
    assert resolve_column(["A", HEADER_IQ, "Spectrogram"], 2, adjacent_free=False) == (3, "reuse")
    assert resolve_column(["A", HEADER_IQ, "Notes", HEADER_SPECTROGRAM], 2, adjacent_free=False) == (4, "reuse")
    assert resolve_column(["A", HEADER_IQ, "Notes"], 2, adjacent_free=False) == (3, "insert")


def test_hidden_rows_wait_and_other_content_is_never_covered(tmp_path):
    names = [f"{STEM}_0001.wav", "iq_20260101_130000_0001.wav"]
    book = log_workbook(tmp_path / "log.xlsx", [("first", names[0]), ("second", names[1])], extra_headers=(HEADER_SPECTROGRAM,))
    wb = load_workbook(book)
    ws = wb["Log"]
    ws.row_dimensions[2].hidden = True  # filtered out
    ws["C3"] = "typed by hand"
    wb.save(book)
    wb.close()
    queue = WorkbookQueue(tmp_path / "q.json")
    for name in names:
        queue.enqueue_image(image_item(book, name, make_png(tmp_path / f"{name}.thumb.png"), make_png(tmp_path / f"{name}.png")))
    report = flush_images(queue, links=local_links)
    assert not report.written and report.waiting == 1
    assert [i["file"] for i in report.given_up] == [names[1]]
    assert queue.pending_images()[0]["last_error"] == "its row is hidden"
    wb = load_workbook(book)
    assert wb["Log"]["C3"].value == "typed by hand" and not wb["Log"]._images
    wb.close()


def test_row_writer_keeps_the_picture_links(tmp_path):
    from openpyxl.drawing.image import Image as XLImage
    from test_collection_excel import build_sample_workbook, haifa_mappings

    from crfs_iq_recorder.excel_writer import ExcelRow, append_rows_openpyxl

    openpyxl_links.install()
    book = build_sample_workbook(tmp_path / "sample.xlsx")
    png = make_png(tmp_path / "Spectrograms" / "a.png", 800, 400)
    wb = openpyxl_links.load_workbook_keeping_links(book)
    picture = XLImage(str(make_png(tmp_path / "Spectrograms" / "a.thumb.png")))
    picture.anchor = "P2"
    openpyxl_links.set_picture_link(picture, png.resolve().as_uri(), tooltip="t", name=shape_name("a_0001.wav"), descr="d")
    wb["SATCOM"].add_image(picture)
    wb.save(book)
    wb.close()
    before = openpyxl_links.drawing_hyperlinks(book)
    assert [t for links in before.values() for t in links.values()] == [(png.resolve().as_uri(), "External")]
    mapping = haifa_mappings()[1]
    row = ExcelRow(
        worksheet="SATCOM", collection_event="Haifa Port", target_class="Iridium",
        start_hz=mapping.start_hz, end_hz=mapping.end_hz, start_style=mapping.start_style, stop_style=mapping.stop_style,
        sensor="CRFS 100-18", filename="iq_later_0001.wav", remote_path="/mnt/1/remdata/20260915/iq_later_0001.wav",
        sensor_id="rfeyeDEMO", duration_s=0.5,
    )
    append_rows_openpyxl(book, [row])
    after = openpyxl_links.drawing_hyperlinks(book)
    assert [t for links in after.values() for t in links.values()] == [(png.resolve().as_uri(), "External")]
    pics = pictures(book, "SATCOM")
    assert [(p[0], p[4][0]) for p in pics] == [(shape_name("a_0001.wav"), png.resolve().as_uri())]
    assert not list(tmp_path.glob("*.crfs-tmp-*")), "the atomic save leaves no temporary file"


def test_service_finds_rows_logged_after_the_spectrogram_was_made(tmp_path, app):
    a = crfs_wav(tmp_path / "rec", f"{STEM}_0001.wav")
    book = log_workbook(tmp_path / "log.xlsx", [("event", a.name)])
    store = ExcelLogStore(tmp_path / "excel_log_store.json")
    svc, queue, logs = service(tmp_path, store=store)
    try:
        svc.request(a)
        run_all(svc)
        assert queue.image_count() == 0 and ("info", f"Spectrogram saved: {a.name}.") in logs
        store.groups = {"g": {"filenames": [a.name], "durations": {}, "workbook": str(book), "worksheet": "Log"}}
        svc._retarget_last = 0.0
        svc._retarget()
        assert queue.image_count() == 1
        svc._flush_images()
        assert ("info", "Spectrogram added.") in logs and len(pictures(book)) == 1
        svc._retarget_last = 0.0
        svc._retarget()
        assert queue.image_count() == 0, "attached once"
    finally:
        svc.shutdown()


def test_protocol_rows_find_both_queued_and_written_targets(tmp_path):
    from test_cellular_workbook import _import, _result, make_workbook

    path = make_workbook(tmp_path / "p.xlsx", table=False)
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    queue = WorkbookQueue(tmp_path / "q.json")
    queue.enqueue(path, layout, plan, _result("r1", 0, ["a_0001.wav"], 2.0, datetime(2026, 1, 1)))
    found = queue.rows_listing_many({"a_0001.wav"})["a_0001.wav"]
    assert found[0]["queued"] and found[0]["iq_col"] == layout.result and found[0]["sheet"] == "Cellular Protocol"


def test_protocol_results_follow_their_columns_after_the_spectrogram_column_is_inserted(tmp_path):
    from test_cellular_workbook import _import, _result, make_workbook

    path = make_workbook(tmp_path / "p.xlsx", table=False)
    imported = _import(path)
    layout, plan = layout_from_import(imported), plan_rows_from_import(imported)
    t = datetime(2026, 11, 2, 9)
    first = write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", 0, ["a_0001.wav"], 2.0, t)], blocks={})
    assert first.roles["result"] == HEADER_IQ and first.roles["duration"] == "IQ Duration (s)"
    # Excel inserts the "IQ Spectrogram" column right of the file names (what the COM writer does).
    wb = load_workbook(path)
    ws = wb["Cellular Protocol"]
    ws.insert_cols(8)
    ws.cell(row=1, column=8).value = HEADER_SPECTROGRAM
    wb.save(path)
    wb.close()
    write_results_openpyxl(path, layout=layout, plan=plan, results=[_result("r1", 1, ["b_0001.wav"], 3.5, t)], blocks=first.blocks, roles=first.roles)
    wb = load_workbook(path)
    ws = wb["Cellular Protocol"]
    row = [c.value for c in ws[3]]
    wb.close()
    assert row[6] == "b_0001.wav" and row[7] is None, "nothing written into the spectrogram column"
    assert row[8] == 3.5 and row[9] is not None, row


def test_block_copies_in_excel_do_not_copy_pictures():
    log: list = []

    class App:
        def __init__(self):
            self._objects = True
            self.CutCopyMode = True

        @property
        def CopyObjectsWithCells(self):
            return self._objects

        @CopyObjectsWithCells.setter
        def CopyObjectsWithCells(self, value):
            log.append(("objects", value))
            self._objects = value

    app = App()

    class Range:
        def Copy(self, target=None):
            log.append(("copy", app._objects))

        def PasteSpecial(self, Paste=None):
            log.append(("paste", app._objects))

    class Sheet:
        Application = app

        def Range(self, a, b):
            return Range()

        def Cells(self, r, c):
            return (r, c)

    _ComSheet(Sheet()).copy_cells(2, 6, 1, 9, values=True)
    _ComSheet(Sheet()).copy_cells(2, 7, 1, 9, values=False)
    assert ("copy", False) in log and ("paste", False) in log and app._objects is True


class FilteredSheet:
    """Excel sheet over COM as seen on hardware: with a filter hiding rows, Cells.Find("*") backwards answers row 1."""

    class _Cell:
        def __init__(self, row, col):
            self.Row, self.Column = row, col

    class _Range:
        def __init__(self, sheet, first, last):
            self.sheet, self.first, self.last = sheet, first, last

        @property
        def Value(self):
            rows = tuple(tuple(self.sheet.values.get((r, c)) for c in range(self.first.Column, self.last.Column + 1))
                         for r in range(self.first.Row, self.last.Row + 1))
            return rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else rows

    class _Cells:
        def __init__(self, sheet):
            self.sheet = sheet

        def __call__(self, row, col):
            return FilteredSheet._Cell(row, col)

        def Find(self, **_kw):
            if self.sheet.FilterMode or self.sheet.table_filtered:
                return FilteredSheet._Cell(1, 1)
            return FilteredSheet._Cell(max(r for r, _c in self.sheet.values), 1)

    def __init__(self, values, *, filtered=True, table_filtered=False, used_rows=None):
        self.values, self.FilterMode, self.table_filtered = values, filtered, table_filtered
        self.counts = 0
        rows = used_rows or max(r for r, _c in values)
        cols = max(c for _r, c in values)
        self.UsedRange = type("Used", (), {"Row": 1, "Column": 1, "Rows": type("R", (), {"Count": rows}), "Columns": type("C", (), {"Count": cols})})
        self.Cells = FilteredSheet._Cells(self)
        table = type("T", (), {"AutoFilter": type("A", (), {"FilterMode": table_filtered})})
        tables = type("Ts", (), {"Count": 1, "__call__": lambda _self, _i: table})
        self.ListObjects = tables()
        self.Application = type("App", (), {"WorksheetFunction": type("WF", (), {"CountA": staticmethod(self._counta)})})

    def Range(self, first, last):
        return FilteredSheet._Range(self, first, last)

    def _counta(self, rng):
        self.counts += 1
        return sum(1 for (r, c), v in self.values.items()
                   if rng.first.Row <= r <= rng.last.Row and rng.first.Column <= c <= rng.last.Column and v not in (None, ""))


def _log_values(last: int = 5) -> dict:
    values = {(1, 1): "Collection Event", (1, 2): "Target Class", (1, 3): HEADER_IQ}
    for r in range(2, last + 1):
        values.update({(r, 1): f"event {r}", (r, 2): "LTE", (r, 3): f"iq_20261109_10{r:02d}00_0001.wav"})
    return values


@pytest.mark.parametrize("sheet_filter, table_filter", [(True, False), (False, True)])
def test_last_row_on_a_filtered_sheet_counts_hidden_rows(sheet_filter, table_filter):
    from crfs_iq_recorder import excel_com
    from crfs_iq_recorder.excel_images import _com_last_row

    sheet = FilteredSheet(_log_values(5), filtered=sheet_filter, table_filtered=table_filter)
    assert sheet.Cells.Find().Row == 1  # what Excel's Find answers here
    assert excel_com.sheet_filtered(sheet)
    assert excel_com._last_used_row(sheet, 3) == 5  # an appended row goes to 6, never over row 2
    assert _com_last_row(sheet) == 5
    assert _ComSheet(sheet).last_used_row([1, 3]) == 5
    # the bottom row (hidden by the filter) is still found by its file name, so it is not written twice
    headers = {"Collection Event": 1, "Target Class": 2, HEADER_IQ: 3}
    assert excel_com._find_row_with_names(sheet, headers, ["iq_20261109_100500_0001.wav"]) == 5


def test_last_row_unfiltered_keeps_using_find_and_counting_is_bounded():
    from crfs_iq_recorder import excel_com

    plain = FilteredSheet(_log_values(5), filtered=False)
    assert excel_com._last_used_row(plain, 3) == 5 and plain.counts == 0
    stretched = FilteredSheet(_log_values(7), used_rows=1_048_576)  # stray formatting down to the last row
    assert excel_com.last_row_counting(stretched, 3) == 7
    assert stretched.counts <= 22
    header_only = FilteredSheet({(1, 1): "Collection Event", (1, 3): HEADER_IQ})
    assert excel_com.last_row_counting(header_only, 3) == 1


# ----------------------------------------------------------------------------- links
def test_link_is_local_unless_the_png_is_in_a_synced_folder(tmp_path):
    synced = tmp_path / "OneDrive - Org"
    inside = make_png(synced / "IQ" / "Spectrograms" / "a b.png")
    outside = make_png(tmp_path / "local" / "c.png")
    mapping = [(synced, "https://org.sharepoint.com/sites/team/Shared Documents")]
    shared = link_for(inside, mappings=mapping)
    assert shared.shared and shared.tooltip == SHARED_TIP
    assert shared.address == "https://org.sharepoint.com/sites/team/Shared%20Documents/IQ/Spectrograms/a%20b.png"
    local = link_for(outside, mappings=mapping)
    assert not local.shared and local.tooltip == LOCAL_TIP.format(path=outside.resolve()) and "on this PC" in local.tooltip
    assert local.uri == outside.resolve().as_uri() and not local.address.startswith("http")
    assert link_for(tmp_path / "missing.png", mappings=mapping) is None
    assert not link_for(inside, mappings=[]).shared, "no sync mapping, no web address"


def test_operator_messages_follow_the_rules():
    from operator_rules import problems

    long_name = "Cellular protocol (Office collection, building 2) copy.xlsx"
    for text in (
        f"Generating spectrogram: {STEM}_0001.wav.",
        "Spectrogram added.",
        "Spectrograms added: 12.",
        f"Spectrogram saved: {STEM}_0001.wav.",
        f"Spectrogram failed: {STEM}_0001.wav. See details.",
        f"Spectrograms for {long_name} are added when it is open in Excel. See details.",
    ):
        assert not problems(text), (text, problems(text))
