import pytest

from crfs_iq_recorder.demo_sftp import reset_shared_demo_browser, shared_demo_browser
from crfs_iq_recorder.sensor_storage import (
    DEMO_FREE_BYTES,
    DEMO_TOTAL_BYTES,
    LEVEL_CRITICAL,
    LEVEL_OK,
    LEVEL_UNAVAILABLE,
    LEVEL_WARN,
    StorageSnapshot,
    estimated_recording_bytes,
    format_storage_bytes,
    needed_bytes_with_margin,
    query_iq_storage,
    recording_blocked_reason,
    snapshot_from_df_text,
    snapshot_from_statvfs,
    storage_line,
    storage_note,
    warning_level,
)
from crfs_iq_recorder.sftp_client import DemoSftpBrowser


@pytest.fixture(autouse=True)
def isolated_app_files(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.paths.settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "recordings.json")


@pytest.fixture
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


class _FakeVfs:
    def __init__(self, frsize, blocks, bavail):
        self.st_frsize = frsize
        self.st_blocks = blocks
        self.st_bavail = bavail
        self.st_bfree = bavail


def test_format_storage_bytes_uses_mb_gb_tb():
    assert format_storage_bytes(20 * 1024 * 1024).endswith("MB")
    assert "GB" in format_storage_bytes(int(18.4 * 1024 ** 3))
    assert "TB" in format_storage_bytes(2 * 1024 ** 4)


def test_snapshot_from_statvfs():
    snap = snapshot_from_statvfs("/mnt/1/remdata/", _FakeVfs(4096, 1000, 200))
    assert snap is not None
    assert snap.ok
    assert snap.total_bytes == 4096 * 1000
    assert snap.free_bytes == 4096 * 200


def test_snapshot_from_df_text():
    text = (
        "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
        "/dev/sda1        134217728 2000000 132217728       2% /mnt/1\n"
    )
    snap = snapshot_from_df_text(text, "/mnt/1/remdata/")
    assert snap is not None
    assert snap.total_bytes == 134217728 * 1024
    assert snap.free_bytes == 132217728 * 1024


def test_warning_levels():
    total = 1000
    assert warning_level(StorageSnapshot(total_bytes=total, free_bytes=200, ok=True)) == LEVEL_OK
    assert warning_level(StorageSnapshot(total_bytes=total, free_bytes=80, ok=True)) == LEVEL_WARN
    assert warning_level(StorageSnapshot(total_bytes=total, free_bytes=40, ok=True)) == LEVEL_CRITICAL
    assert warning_level(StorageSnapshot.unavailable()) == LEVEL_UNAVAILABLE


def test_recording_blocked_when_free_space_is_too_small():
    estimate = 50 * 1024 * 1024
    needed = needed_bytes_with_margin(estimate)
    snap = StorageSnapshot(total_bytes=10 * 1024 ** 3, free_bytes=needed - 1, ok=True)
    reason = recording_blocked_reason(snap, estimate)
    assert reason is not None
    assert "Not enough IQ storage" in reason
    plenty = StorageSnapshot(total_bytes=10 * 1024 ** 3, free_bytes=needed + 1, ok=True)
    assert recording_blocked_reason(plenty, estimate) is None


def test_unknown_storage_does_not_block_recording():
    assert recording_blocked_reason(StorageSnapshot.unavailable(), 10_000_000) is None


def test_query_uses_remdata_root_when_today_folder_is_missing():
    browser = DemoSftpBrowser(include_today=False)
    browser.set_storage(total=DEMO_TOTAL_BYTES, free=DEMO_FREE_BYTES, fail=False)
    snap = query_iq_storage(browser)
    assert snap.ok
    assert snap.path == "/mnt/1/remdata/"
    assert snap.free_bytes == DEMO_FREE_BYTES


def test_query_failure_returns_unavailable():
    browser = DemoSftpBrowser(include_today=False)
    browser.set_storage(fail=True)
    snap = query_iq_storage(browser)
    assert not snap.ok
    assert storage_line(snap) == "Storage unavailable"


def test_stale_snapshot_keeps_last_numbers():
    previous = StorageSnapshot(path="/mnt/1/remdata/", total_bytes=1000, free_bytes=400, ok=True)
    stale = previous.as_stale("timeout")
    assert stale.stale
    assert stale.free_bytes == 400
    assert "(outdated)" in storage_line(stale)
    assert "outdated" in storage_note(stale).lower()


def test_demo_delete_frees_space():
    browser = DemoSftpBrowser()
    before = browser.storage_usage("/mnt/1/remdata/").free_bytes
    files = list(browser._files)
    assert files
    path = files[0]
    size = len(browser._files[path])
    browser.remove_file(path)
    after = browser.storage_usage("/mnt/1/remdata/").free_bytes
    assert after == before + size


def test_estimated_bytes_match_empirical_mb():
    bytes_ = estimated_recording_bytes(10_000_000, 1)
    assert bytes_ == 61_051_000


def test_gui_shows_demo_storage_and_blocks_when_full(qapp, monkeypatch):
    reset_shared_demo_browser()
    from PySide6.QtWidgets import QMessageBox
    from crfs_iq_recorder.gui import MainWindow

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: 0)
    window = MainWindow(demo=True)
    try:
        assert "free /" in window.storage_value.text()
        assert window.storage_value.text() != "Storage unavailable"
        shared_demo_browser().set_storage(total=1000, free=10, fail=False)
        window._refresh_storage()
        qapp.processEvents()
        assert "under 5%" in window.storage_note.text().lower() or "critical" in window.storage_note.text().lower()
        assert window.storage_note.objectName() == "storageNoteCritical"
        window.time_edit.setText("10")
        window._refresh_size()
        window._on_start()
        qapp.processEvents()
        assert window._record_phase == "idle"
        assert "Not enough IQ storage" in window.log_edit.toPlainText()
    finally:
        window.close()
        reset_shared_demo_browser()


def test_sftp_delete_increases_reported_free_space(qapp):
    reset_shared_demo_browser()
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        browser = shared_demo_browser()
        before = window._storage.free_bytes
        path = next(iter(browser._files))
        size = len(browser._files[path])
        browser.remove_file(path)
        window._refresh_storage()
        qapp.processEvents()
        assert window._storage.free_bytes == before + size
    finally:
        window.close()
        reset_shared_demo_browser()


def test_gui_marks_storage_outdated_when_query_fails(qapp):
    reset_shared_demo_browser()
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        shared_demo_browser().set_storage(fail=True)
        window._refresh_storage()
        qapp.processEvents()
        assert "(outdated)" in window.storage_value.text()
        assert "outdated" in window.storage_note.text().lower()
    finally:
        window.close()
        reset_shared_demo_browser()
