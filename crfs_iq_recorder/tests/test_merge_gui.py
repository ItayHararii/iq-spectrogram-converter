"""Merge recording parts: toggle, auto-merge after download, manual merge, dedupe, cancel.

Offscreen Qt, demo SFTP (no sensor). The "sensor" files are Node-like parts
built by test_iq_merge.write_part so the engine's checks are exercised for real.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_iq_merge import FS, T0, write_part


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


@pytest.fixture(autouse=True)
def isolated_everything(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "recordings.json")
    monkeypatch.setattr("crfs_iq_recorder.paths.settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr("crfs_iq_recorder.merge_controller.merge_history_path", lambda: tmp_path / "merge_history.json")
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", lambda *_a, **_k: True)


def _pump(qapp, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def _wait(qapp, predicate, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def publish_node_parts(tmp_path: Path, stem: str = "file_063929", frames=(250, 250, 100)) -> tuple[list[str], list[bytes], str]:
    """Node-like parts in the demo sensor's folder for today. Returns (names, payloads, directory)."""
    from crfs_iq_recorder.demo_sftp import shared_demo_browser
    from crfs_iq_recorder.sftp_paths import today_remdata_directory

    directory = today_remdata_directory()
    browser = shared_demo_browser()
    names, blobs, start = [], [], T0
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    for index, count in enumerate(frames, start=1):
        name = f"{stem}_{index:04d}.wav"
        path = scratch / name
        write_part(path, frames=count, start=start, seed=index)
        blob = path.read_bytes()
        browser.add_file(name, directory, payload=blob, size=len(blob), modified=datetime.now() + timedelta(seconds=index))
        names.append(name)
        blobs.append(blob)
        start += timedelta(seconds=count / FS)
    return names, blobs, directory


def _open_sftp_window(window, qapp):
    window._open_sftp()
    win = window._sftp_win
    assert win is not None
    assert _wait(qapp, lambda: win.table.topLevelItemCount() >= 1 and not win._busy, 5.0)
    return win


def _select_group(win, stem: str) -> None:
    from crfs_iq_recorder.sftp_window import ROLE_GROUP, ROLE_KIND

    for index in range(win.table.topLevelItemCount()):
        item = win.table.topLevelItem(index)
        if item is None:
            continue
        if str(item.data(0, ROLE_KIND) or "") == "group" and f"|{stem}|" in str(item.data(0, ROLE_GROUP) or ""):
            win.table.clearSelection()
            item.setSelected(True)
            win.table.setCurrentItem(item)
            return
    raise AssertionError(f"group {stem} not listed")


def test_merge_toggle_is_off_by_default_and_persists(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.paths import load_settings

    window = MainWindow(demo=True)
    try:
        assert window.merge_check.isChecked() is False
        assert window._merge.enabled is False
        assert window.merge_status.isHidden()
        window.merge_check.setChecked(True)
        qapp.processEvents()
        assert window._merge.enabled is True
        assert load_settings().get("merge_parts") is True
        assert "Merge recording parts: on." in window.log_edit.toPlainText()
    finally:
        window.close()
        qapp.processEvents()
    again = MainWindow(demo=True)
    try:
        assert again.merge_check.isChecked() is True
        assert again._merge.enabled is True
        again.merge_check.setChecked(False)
        assert load_settings().get("merge_parts") is False
    finally:
        again.close()
        qapp.processEvents()


def test_auto_merge_runs_once_after_all_parts_are_downloaded(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.iq_merge import inspect_part, read_sidecar

    names, blobs, _directory = publish_node_parts(tmp_path)
    window = MainWindow(demo=True)
    downloads = tmp_path / "downloads"
    window._conn.download_dir = str(downloads)
    window.merge_check.setChecked(True)
    merged: list = []
    window._merge.merged.connect(lambda stem, result: merged.append((stem, result)))
    try:
        win = _open_sftp_window(window, qapp)
        _select_group(win, "file_063929")
        qapp.processEvents()
        assert win.download_btn.text() == "Download this recording"
        assert win.merge_btn.isEnabled() and win.merge_btn.text() == "Merge 3 parts"
        win._download_selected()
        assert _wait(qapp, lambda: len(merged) == 1, 15.0), window.log_edit.toPlainText()
        stem, result = merged[0]
        assert stem == "file_063929"
        assert result.output == downloads / "file_063929_merged.wav"
        assert result.output.exists() and result.frames == 600
        for name, blob in zip(names, blobs):
            assert (downloads / name).read_bytes() == blob, "originals kept byte for byte"
        info = inspect_part(result.output)
        payload = b"".join(inspect_part(downloads / n).path.read_bytes()[44:44 + inspect_part(downloads / n).data_size] for n in names)
        assert result.output.read_bytes()[info.data_offset:info.data_offset + info.data_size] == payload
        assert read_sidecar(result.output)["continuity"] == "verified"
        log = window.log_edit.toPlainText()
        assert "Auto-merge queued for file_063929 (3 parts)." in log
        assert "Merged file_063929: file_063929_merged.wav, " in log and "Originals kept." in log
        assert window.merge_status.text().startswith("Merged file_063929 into file_063929_merged.wav")
        assert window.merge_cancel_btn.isHidden() and window.merge_progress.isHidden()
        # Recording status is untouched by merging.
        assert window._record_phase == "idle" and window._recordings_completed == 0

        # Download the same recording again (refresh / reconnect): no second merge.
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: not win._dl_active and not win._dl_pending and not win._busy, 10.0)
        _pump(qapp, 0.5)
        assert len(merged) == 1
        assert "Already merged: file_063929 into file_063929_merged.wav." in window.log_edit.toPlainText()
        assert not (downloads / "file_063929_merged (2).wav").exists()
    finally:
        window.close()
        qapp.processEvents()

    # A fresh application instance remembers the merge as well (registry on disk).
    again = MainWindow(demo=True)
    again._conn.download_dir = str(downloads)
    again.merge_check.setChecked(True)
    merged_again: list = []
    again._merge.merged.connect(lambda stem, result: merged_again.append(stem))
    try:
        win = _open_sftp_window(again, qapp)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: not win._dl_active and not win._dl_pending and not win._busy, 10.0)
        _pump(qapp, 0.5)
        assert merged_again == []
        assert "Already merged" in again.log_edit.toPlainText()
    finally:
        again.close()
        qapp.processEvents()


def test_toggle_off_leaves_downloads_alone_and_manual_merge_fetches_missing_parts(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder import sftp_window as sftp_module
    from crfs_iq_recorder.gui import MainWindow

    names, blobs, _directory = publish_node_parts(tmp_path)
    window = MainWindow(demo=True)
    downloads = tmp_path / "downloads"
    window._conn.download_dir = str(downloads)
    assert window.merge_check.isChecked() is False
    merged: list = []
    window._merge.merged.connect(lambda stem, result: merged.append(result))
    try:
        win = _open_sftp_window(window, qapp)
        # Download only the first two parts with merging off: nothing is merged.
        by_name = {e.name: e for e in win._entries}
        win._begin_downloads([by_name[names[0]], by_name[names[1]]])
        assert _wait(qapp, lambda: not win._dl_active and not win._dl_pending and not win._busy, 10.0)
        _pump(qapp, 0.3)
        assert merged == [] and not list(downloads.glob("*_merged*"))
        assert (downloads / names[0]).exists() and (downloads / names[1]).exists()
        assert not (downloads / names[2]).exists()

        # Manual merge: the confirmation names the missing part and the estimate.
        shown: list[str] = []

        def fake_question(_parent, _title, text, *_a, **_k):
            shown.append(text)
            return sftp_module.QMessageBox.StandardButton.Yes

        monkeypatch.setattr(sftp_module.QMessageBox, "question", staticmethod(fake_question))
        _select_group(win, "file_063929")
        qapp.processEvents()
        assert win.merge_btn.isEnabled()
        win._merge_selected()
        assert shown and "1 part(s) are not in the download folder yet" in shown[0]
        assert "Estimated output" in shown[0] and "original parts are kept" in shown[0]
        assert _wait(qapp, lambda: len(merged) == 1, 15.0), window.log_edit.toPlainText()
        assert (downloads / names[2]).read_bytes() == blobs[2]
        assert merged[0].output == downloads / "file_063929_merged.wav" and merged[0].frames == 600
        log = window.log_edit.toPlainText()
        assert "downloading 1 missing part(s) first" in log
        assert "Merge queued for file_063929 (3 parts)." in log
        assert window._merge.enabled is False, "a manual merge does not switch automatic merging on"

        # Manual merge again with everything local: the dialog shows the verified figures,
        # and the output gets a fresh unique name (the first one is protected).
        shown.clear()
        _select_group(win, "file_063929")
        win._merge_selected()
        assert "Verified duration: 0.600 s (600 samples)" in shown[0]
        assert "file_063929_merged (2).wav" in shown[0] and "RIFF" in shown[0]
        assert "verified from the sensor's time stamps" in shown[0]
        assert _wait(qapp, lambda: len(merged) == 2, 15.0), window.log_edit.toPlainText()
        assert merged[1].output == downloads / "file_063929_merged (2).wav"
        assert (downloads / "file_063929_merged.wav").exists()
    finally:
        window.close()
        qapp.processEvents()


def test_failed_merge_does_not_touch_recording_status(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    names, _blobs, _directory = publish_node_parts(tmp_path)
    # Corrupt part 2 on the "sensor": its header promises more bytes than it holds.
    from crfs_iq_recorder.demo_sftp import shared_demo_browser
    from crfs_iq_recorder.sftp_paths import today_remdata_directory

    browser = shared_demo_browser()
    directory = today_remdata_directory()
    scratch = tmp_path / "scratch"
    write_part(scratch / names[1], frames=250, start=T0 + timedelta(seconds=0.25), seed=2, truncate_by=300)
    blob = (scratch / names[1]).read_bytes()
    browser.add_file(names[1], directory, payload=blob, size=len(blob), modified=datetime.now())

    window = MainWindow(demo=True)
    downloads = tmp_path / "downloads"
    window._conn.download_dir = str(downloads)
    window.merge_check.setChecked(True)
    failures: list = []
    window._merge.merge_failed.connect(lambda stem, reason: failures.append((stem, reason)))
    try:
        window._recordings_completed = 4
        win = _open_sftp_window(window, qapp)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: len(failures) == 1, 15.0), window.log_edit.toPlainText()
        stem, reason = failures[0]
        assert stem == "file_063929" and names[1] in reason and "cut short" in reason
        assert not list(downloads.glob("*_merged*")), "no output for a failed merge"
        assert window._recordings_completed == 4 and window._record_phase == "idle"
        assert window.merge_status.text().startswith("Merge failed: file_063929.")
        assert "The recording is not affected." in window.log_edit.toPlainText()
        # Nothing is remembered for a failed merge: fixing the part and downloading again merges.
        write_part(scratch / names[1], frames=250, start=T0 + timedelta(seconds=0.25), seed=2)
        blob = (scratch / names[1]).read_bytes()
        browser.add_file(names[1], directory, payload=blob, size=len(blob), modified=datetime.now())
        merged: list = []
        window._merge.merged.connect(lambda s, r: merged.append(r))
        win.refresh()
        assert _wait(qapp, lambda: not win._busy, 5.0)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: len(merged) == 1, 15.0), window.log_edit.toPlainText()
        assert merged[0].frames == 600
    finally:
        window.close()
        qapp.processEvents()


def test_cancel_merge_removes_partial_output_and_frees_the_queue(qapp, tmp_path, monkeypatch):
    import threading

    from crfs_iq_recorder import iq_merge
    from crfs_iq_recorder.gui import MainWindow

    publish_node_parts(tmp_path, frames=(400, 400, 400))
    window = MainWindow(demo=True)
    downloads = tmp_path / "downloads"
    window._conn.download_dir = str(downloads)
    window.merge_check.setChecked(True)
    # Slow the copy down so the cancel lands mid-way.
    real_merge = iq_merge.merge_parts
    started = threading.Event()

    def slow_merge(plan, output, *, progress=None, cancel=None, **kw):
        def slow_progress(done, total):
            started.set()
            time.sleep(0.05)
            if progress:
                progress(done, total)

        return real_merge(plan, output, progress=slow_progress, cancel=cancel, chunk_bytes=64, **kw)

    monkeypatch.setattr("crfs_iq_recorder.merge_controller.merge_parts", slow_merge)
    failures: list = []
    window._merge.merge_failed.connect(lambda stem, reason: failures.append(reason))
    try:
        win = _open_sftp_window(window, qapp)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: started.is_set() and (not window.merge_cancel_btn.isHidden()), 10.0)
        assert not window.merge_progress.isHidden()
        assert window.merge_status.text().startswith("Merging file_063929 (3 parts, 1.20 s)…")
        window.merge_cancel_btn.click()
        assert _wait(qapp, lambda: failures == ["Merge cancelled."], 10.0), window.log_edit.toPlainText()
        assert _wait(qapp, lambda: not window._merge.is_busy(), 5.0)
        assert not list(downloads.glob("*_merged*")), "cancelled merge leaves nothing behind"
        assert "Merge cancelled for file_063929" in window.log_edit.toPlainText()
        assert window.merge_cancel_btn.isHidden()
        # The group can be merged again afterwards (cancel is not remembered as done).
        merged: list = []
        window._merge.merged.connect(lambda s, r: merged.append(r))
        monkeypatch.setattr("crfs_iq_recorder.merge_controller.merge_parts", real_merge)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: len(merged) == 1, 15.0), window.log_edit.toPlainText()
    finally:
        window.close()
        qapp.processEvents()


def test_auto_merge_waits_while_the_recording_is_still_live(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.recording_status import FINALIZING

    publish_node_parts(tmp_path)
    window = MainWindow(demo=True)
    downloads = tmp_path / "downloads"
    window._conn.download_dir = str(downloads)
    window.merge_check.setChecked(True)
    try:
        # Pretend file_063929 is the recording still being confirmed.
        window._record_phase = FINALIZING
        window._sensor_parts = ["file_063929_0003.wav"]
        assert window._live_recording_stem() == "file_063929"
        win = _open_sftp_window(window, qapp)
        _select_group(win, "file_063929")
        win._download_selected()
        assert _wait(qapp, lambda: not win._dl_active and not win._dl_pending and not win._busy, 10.0)
        _pump(qapp, 0.3)
        assert not list(downloads.glob("*_merged*"))
        assert not window._merge.is_busy()
    finally:
        window._record_phase = "idle"
        window.close()
        qapp.processEvents()


def test_startup_removes_unfinished_merge_outputs(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    leftover = downloads / "file_063929_merged.wav.partial"
    leftover.write_bytes(b"half")
    keep = downloads / "file_063929_0001.wav"
    keep.write_bytes(b"not ours to touch")
    from crfs_iq_recorder.paths import save_settings

    save_settings({"download_dir": str(downloads)})
    window = MainWindow(demo=True)
    try:
        assert not leftover.exists()
        assert keep.exists()
        assert "Removed unfinished merge output file_063929_merged.wav.partial." in window.log_edit.toPlainText()
    finally:
        window.close()
        qapp.processEvents()
