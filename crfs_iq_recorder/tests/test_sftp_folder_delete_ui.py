"""Sensor Files: the right-click menu and Delete folder, on the demo sensor (offscreen)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = "/mnt/1/remdata/"
NAME = "crfs_delete_test_20261109_120000"
TEST = f"/mnt/1/remdata/{NAME}"
OLD = datetime.now() - timedelta(hours=2)


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


@pytest.fixture
def opened_urls(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QDesktopServices.openUrl",
        lambda url, *_a, **_k: urls.append(url.toLocalFile()) or True,
    )
    return urls


class FakeHost:
    """What Sensor Files asks the main window: identity, recording, collection, manual download."""

    def __init__(self) -> None:
        self.capture = ""
        self.stem = None
        self._sensor_info = SimpleNamespace(host="192.0.2.10", model="R100-18", serial="rfeye100400")
        self.cellular_panel = SimpleNamespace(runner=SimpleNamespace(state="idle", _downloads_pending=[]))
        self._manual_download = None

    def capture_busy_reason(self) -> str:
        return self.capture

    def _live_recording_stem(self):
        return self.stem


def _pump(qapp, until, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return until()


def _sensor():
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    browser = shared_demo_browser()
    browser.add_folder(TEST, modified=OLD)
    browser.add_folder(f"{TEST}/nested", modified=OLD)
    browser.add_folder(f"{TEST}_outside", modified=OLD)
    for index in range(3):
        browser.add_file(f"take{index}.wav", TEST, payload=b"x" * (200 + index), modified=OLD)
    browser.add_file("inner.bin", f"{TEST}/nested", payload=b"y" * 20, modified=OLD)
    browser.add_file("canary.txt", f"{TEST}_outside", payload=b"canary", modified=OLD)
    browser.add_link(f"{TEST}/escape", f"{TEST}_outside", modified=OLD)
    return browser


def _tree(browser) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, entries in browser._dirs.items():
        out[key] = "dir"
        for entry in entries:
            if entry.path in browser._links:
                out[entry.path] = f"link->{browser._links[entry.path]}"
            elif not entry.is_dir:
                out[entry.path] = f"file {len(browser._files.get(entry.path, b''))}"
    return out


@pytest.fixture
def window(qapp, tmp_path, opened_urls):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    _sensor()
    host = FakeHost()
    win = SftpWindow(ConnectionState(demo=True, host="192.0.2.10", download_dir=str(tmp_path / "downloads")))
    win.set_host(host)
    win.fake_host = host
    win.activity_lines = []
    win.activity.connect(win.activity_lines.append)
    win.show()
    assert _pump(qapp, lambda: not win._busy and win.table.topLevelItemCount() > 0)
    yield win
    win.close()
    qapp.processEvents()


def _go(window, qapp, path):
    window._request_list(path)
    assert _pump(qapp, lambda: not window._busy and window._path.rstrip("/") == path.rstrip("/"))


def _row(window, path):
    for item in window._iter_tree_items():
        from crfs_iq_recorder.sftp_window import ROLE_PATH

        if str(item.data(0, ROLE_PATH) or "").rstrip("/") == path.rstrip("/"):
            return item
    raise AssertionError(f"no row for {path}")


def _actions(menu) -> dict[str, object]:
    return {action.objectName(): action for action in menu.actions() if action.objectName()}


def _right_click(window, qapp, item, monkeypatch):
    """A real right press on the row, then the context-menu event Qt sends; returns the menu that would open."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    shown = []
    monkeypatch.setattr(window, "_exec_menu", lambda menu, _pos: shown.append((menu, dict(_actions(menu)), [a.text() for a in menu.actions()])))
    window.table.scrollToItem(item)
    rect = window.table.visualItemRect(item)
    pos = QPoint(rect.left() + 30, rect.center().y())
    viewport = window.table.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, viewport.mapToGlobal(pos)))
    # No event processing here: the closed menu is deleted later, its actions are triggered first.
    assert len(shown) == 1, "the right-click opened no menu"
    return shown[0]


def _check_operator_text(lines):
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from operator_rules import problems

    for line in lines:
        assert not problems(line), (line, problems(line))


# ---------------------------------------------------------------------------- targeting


def test_right_click_outside_the_selection_selects_and_targets_that_row(window, qapp, monkeypatch):
    _go(window, qapp, ROOT)
    other = _row(window, "/mnt/1/remdata/20260115")
    target = _row(window, TEST)
    window.table.clearSelection()
    other.setSelected(True)
    menu, actions, texts = _right_click(window, qapp, target, monkeypatch)
    assert [item for item in window.table.selectedItems()] == [target]
    assert set(actions) == {"open_remote", "open_local", "copy_name", "delete_folder"}
    assert actions["delete_folder"].isEnabled()
    assert "Delete folder…" in texts


def test_right_click_inside_a_multi_selection_keeps_it_and_acts_on_all(window, qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication

    _go(window, qapp, TEST)
    first, second = _row(window, f"{TEST}/take0.wav"), _row(window, f"{TEST}/take2.wav")
    window.table.clearSelection()
    first.setSelected(True)
    second.setSelected(True)
    menu, actions, texts = _right_click(window, qapp, second, monkeypatch)
    assert set(window.table.selectedItems()) == {first, second}
    assert "Download 2 files" in texts
    assert actions["copy_name"].text() == "Copy filenames"
    actions["copy_name"].trigger()
    from crfs_iq_recorder.sftp_window import ROLE_PATH

    in_list_order = [str(item.data(0, ROLE_PATH)).rsplit("/", 1)[-1] for item in window._iter_tree_items() if item in (first, second)]
    assert sorted(in_list_order) == ["take0.wav", "take2.wav"]
    assert QApplication.clipboard().text() == "\n".join(in_list_order)
    assert window.status.text() == "Copied 2 names."
    assert actions["delete_files"].text() == "Delete 2 files from sensor…"


def test_a_mixed_selection_offers_file_actions_and_explains_delete_folder(window, qapp):
    _go(window, qapp, TEST)
    window.table.clearSelection()
    _row(window, f"{TEST}/nested").setSelected(True)
    _row(window, f"{TEST}/take1.wav").setSelected(True)
    menu = window._context_menu(_row(window, f"{TEST}/take1.wav"))
    actions = _actions(menu)
    assert actions["copy_name"].text() == "Copy names"
    assert not actions["delete_folder"].isEnabled()
    assert actions["delete_folder"].text() == "Delete folder…\tSelect one folder only"
    menu.deleteLater()


# ---------------------------------------------------------------------------- actions


def test_download_then_open_folder_reveals_the_saved_copy(window, qapp, no_desktop_open, monkeypatch):
    from PySide6.QtWidgets import QApplication

    _go(window, qapp, TEST)
    menu, actions, texts = _right_click(window, qapp, _row(window, f"{TEST}/take1.wav"), monkeypatch)
    assert not actions["open_folder"].isEnabled()
    assert actions["open_folder"].text() == "Open folder\tNot downloaded yet"
    assert actions["open_folder"].toolTip() == "Not downloaded yet"
    actions["download"].trigger()
    saved = Path(window._state.download_dir) / NAME / "take1.wav"
    assert _pump(qapp, lambda: saved.is_file() and not window._dl_active and not window._dl_pending, timeout=8)
    assert saved.read_bytes() == b"x" * 201
    menu, actions, texts = _right_click(window, qapp, _row(window, f"{TEST}/take1.wav"), monkeypatch)
    assert actions["open_folder"].isEnabled()
    actions["open_folder"].trigger()
    assert no_desktop_open[-1] == ("reveal", str(saved))
    actions["copy_name"].trigger()
    assert QApplication.clipboard().text() == "take1.wav"
    assert window.status.text() == "Copied 1 name."


def test_a_verified_download_is_queued_for_its_spectrogram(window, qapp, no_desktop_open, monkeypatch):
    requests: list[tuple[str, dict]] = []
    window.fake_host.spectrograms = SimpleNamespace(request=lambda path, **kw: requests.append((str(path), kw)) or True)
    _go(window, qapp, TEST)
    menu, actions, _texts = _right_click(window, qapp, _row(window, f"{TEST}/take2.wav"), monkeypatch)
    actions["download"].trigger()
    saved = Path(window._state.download_dir) / NAME / "take2.wav"
    assert _pump(qapp, lambda: saved.is_file() and not window._dl_active and not window._dl_pending, timeout=8)
    assert requests == [(str(saved), {"remote_path": f"{TEST}/take2.wav", "origin": "sensor files"})]


def test_open_folder_finds_copies_saved_before_this_window_opened(window, qapp, no_desktop_open):
    downloads = Path(window._state.download_dir)
    dated = downloads / NAME / "take0.wav"
    dated.parent.mkdir(parents=True)
    dated.write_bytes(b"x" * 200)
    # a clash copy an earlier version put in the sensor-address folder
    by_address = downloads / NAME / "192.0.2.10" / "take2.wav"
    by_address.parent.mkdir(parents=True)
    by_address.write_bytes(b"x" * 202)
    _go(window, qapp, TEST)
    for name, expected in (("take0.wav", dated), ("take2.wav", by_address)):
        window.table.clearSelection()
        menu = window._context_menu(_row(window, f"{TEST}/{name}"))
        action = _actions(menu)["open_folder"]
        assert action.isEnabled(), name
        action.trigger()
        assert no_desktop_open[-1] == ("reveal", str(expected))
        menu.deleteLater()


def test_folder_row_actions(window, qapp, opened_urls):
    from PySide6.QtWidgets import QApplication

    _go(window, qapp, ROOT)
    menu = window._context_menu(_row(window, TEST))
    actions = _actions(menu)
    assert actions["open_local"].text() == "Open local folder\tNo local copies"
    actions["copy_name"].trigger()
    assert QApplication.clipboard().text() == NAME
    actions["open_remote"].trigger()
    assert _pump(qapp, lambda: not window._busy and window._path.rstrip("/") == TEST)
    menu.deleteLater()
    local = Path(window._state.download_dir) / NAME
    local.mkdir(parents=True)
    _go(window, qapp, ROOT)
    menu = window._context_menu(_row(window, TEST))
    actions = _actions(menu)
    assert actions["open_local"].isEnabled()
    actions["open_local"].trigger()
    assert Path(opened_urls[-1]) == local
    menu.deleteLater()


def test_unavailable_actions_say_why_in_every_busy_state(window, qapp):
    from crfs_iq_recorder.remote_delete import REASON_COLLECTION, REASON_LIVE, REASON_RECORDING

    _go(window, qapp, TEST)
    file_row = f"{TEST}/take0.wav"

    def menu_for(path):
        window.table.clearSelection()
        menu = window._context_menu(_row(window, path))
        found = {key: (action.isEnabled(), action.text()) for key, action in _actions(menu).items()}
        menu.deleteLater()
        return found

    idle = menu_for(file_row)
    assert idle["download"] == (True, "Download")
    assert idle["delete_files"] == (True, "Delete from sensor…")
    assert idle["copy_name"] == (True, "Copy filename")
    for attr, value, reason in (
        ("_busy", True, "Wait for the listing"),
        ("_dl_pending", [(SimpleNamespace(path=f"{TEST}/take2.wav"), "")], "Downloads in progress"),
        ("_folder_delete", "/mnt/1/remdata/x", "Deleting a folder"),
    ):
        saved = getattr(window, attr)
        setattr(window, attr, value)
        try:
            busy = menu_for(file_row)
        finally:
            setattr(window, attr, saved)
        assert busy["download"] == (False, f"Download\t{reason}"), attr
        assert busy["delete_files"] == (False, f"Delete from sensor…\t{reason}"), attr
        assert busy["copy_name"][0], attr
    # while recording, the live recording cannot be deleted; other files can
    window.fake_host.stem = "take0"
    assert menu_for(file_row)["delete_files"] == (False, f"Delete from sensor…\t{REASON_LIVE}")
    assert menu_for(f"{TEST}/take1.wav")["delete_files"][0]
    window.fake_host.stem = None
    _go(window, qapp, ROOT)
    window.fake_host.capture = "Recording is running"
    assert menu_for(TEST)["delete_folder"] == (False, f"Delete folder…\t{REASON_RECORDING}")
    window.fake_host.capture = ""
    window.fake_host.cellular_panel.runner.state = "running"
    assert menu_for(TEST)["delete_folder"] == (False, f"Delete folder…\t{REASON_COLLECTION}")
    window.fake_host.cellular_panel.runner.state = "idle"
    assert menu_for(TEST)["delete_folder"] == (True, "Delete folder…")


def test_the_menu_works_while_a_preview_runs(window, qapp):
    from PySide6.QtWidgets import QApplication

    _go(window, qapp, TEST)
    window.select_paths([f"{TEST}/take2.wav"])
    qapp.processEvents()
    menu = window._context_menu(_row(window, f"{TEST}/take2.wav"))
    actions = _actions(menu)
    assert actions["download"].isEnabled()
    assert actions["delete_files"].isEnabled()  # a preview of the file itself does not block deleting it
    actions["copy_name"].trigger()
    assert QApplication.clipboard().text() == "take2.wav"
    menu.deleteLater()


def test_own_preview_does_not_block_the_folder_but_other_readers_do(window, qapp, monkeypatch):
    from crfs_iq_recorder import worker_threads
    from crfs_iq_recorder.remote_delete import REASON_READING

    _go(window, qapp, ROOT)
    preview = SimpleNamespace(remote_paths=lambda: [f"{TEST}/take0.wav"])
    other = SimpleNamespace(remote_paths=lambda: [f"{TEST}/take1.wav"])
    running = [preview]
    monkeypatch.setattr(worker_threads, "tracked_workers", lambda: list(running))
    saved, window._preview_jobs = window._preview_jobs, [(None, preview)]
    try:
        assert window._folder_block_reason(TEST) == ""
        running.append(other)
        assert window._folder_block_reason(TEST) == REASON_READING
    finally:
        window._preview_jobs = saved


def test_root_and_outside_folders_cannot_be_deleted(window, qapp):
    _go(window, qapp, "/mnt/1/")
    menu = window._context_menu(_row(window, "/mnt/1/remdata"))
    assert _actions(menu)["delete_folder"].text() == "Delete folder…\tRecording root"
    menu.deleteLater()
    _go(window, qapp, "/mnt/")
    menu = window._context_menu(_row(window, "/mnt/1"))
    assert _actions(menu)["delete_folder"].text() == "Delete folder…\tOutside recording storage"
    menu.deleteLater()
    # the handler checks again, whatever the menu showed
    window._delete_folder("/mnt/1/remdata/")
    assert window.status.text() == "Folder not deleted: recording root."
    assert window.deleting_folder() == ""


# ---------------------------------------------------------------------------- deleting a folder


def test_the_one_confirmation_names_sensor_path_contents_and_permanence(window, qapp):
    from PySide6.QtWidgets import QMessageBox

    from crfs_iq_recorder.demo_sftp import shared_demo_browser
    from crfs_iq_recorder.remote_delete import plan_folder_delete

    plan = plan_folder_delete(shared_demo_browser(), TEST)
    window._state.demo = False  # the identity line of a real sensor
    try:
        box, delete_btn = window._folder_delete_box(plan)
    finally:
        window._state.demo = True
    text = box.informativeText()
    assert box.windowTitle() == "Delete folder from sensor"
    assert box.text() == f"Delete the folder {TEST}/ and everything in it?"
    assert "Sensor: R100-18, serial rfeye100400, at 192.0.2.10" in text
    assert f"Folder: {TEST}/" in text
    assert "Contents: 4 files (623 B), 1 subfolder, 1 link" in text
    assert "Its contents will be permanently deleted from the sensor." in text
    assert "Links are removed as links" in text
    assert "Local copies on this PC are kept." in text
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel)
    assert box.escapeButton() is box.button(QMessageBox.StandardButton.Cancel)
    assert box.buttonRole(delete_btn) == QMessageBox.ButtonRole.DestructiveRole
    box.deleteLater()
    window.fake_host._sensor_info = SimpleNamespace(host="192.0.2.99", model="R100-18", serial="other")
    window._state.demo = False
    try:
        assert window._sensor_identity_text() == "at 192.0.2.10 (model and serial not read yet)"
    finally:
        window._state.demo = True


def test_delete_folder_end_to_end(window, qapp, monkeypatch):
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    browser = shared_demo_browser()
    local = Path(window._state.download_dir) / NAME / "take0.wav"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"x" * 200)
    before = _tree(browser)
    asked = []
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: asked.append(plan) or True)
    progress = []
    window._worker.folder_progress.connect(lambda item: progress.append(item))
    _go(window, qapp, ROOT)
    menu = window._context_menu(_row(window, TEST))
    _actions(menu)["delete_folder"].trigger()
    menu.deleteLater()
    assert window.deleting_folder() == TEST
    assert not window.refresh_btn.isEnabled() and window.cancel_btn.isEnabled()
    assert _pump(qapp, lambda: not window.deleting_folder() and not window._busy, timeout=8)
    assert len(asked) == 1 and asked[0].folder == TEST
    after = _tree(browser)
    assert {path for path in before if path not in after} == {
        f"{TEST}/",
        f"{TEST}/nested/",
        f"{TEST}/take0.wav",
        f"{TEST}/take1.wav",
        f"{TEST}/take2.wav",
        f"{TEST}/nested/inner.bin",
        f"{TEST}/escape",
    }
    assert all(after[path] == kind for path, kind in before.items() if path in after)
    assert browser._files[f"{TEST}_outside/canary.txt"] == b"canary"
    assert local.read_bytes() == b"x" * 200
    assert window.status.text() == (
        f"Deleted {TEST}/ from the sensor: 5 items, 623 B. Local copies stay in {local.parent}."
    )
    with pytest.raises(AssertionError):
        _row(window, TEST)
    _row(window, f"{TEST}_outside")
    deleting = [item for item in progress if item.phase == "deleting"]
    assert deleting and deleting[-1].done == deleting[-1].total == 7
    assert deleting[-1].bytes_done == deleting[-1].bytes_total == 623
    assert window.activity_lines == [f"Deleted folder {NAME} from the sensor. Local copies are kept."]
    _check_operator_text(window.activity_lines)


def test_cancel_at_the_confirmation_deletes_nothing(window, qapp, monkeypatch):
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    before = _tree(shared_demo_browser())
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: False)
    _go(window, qapp, ROOT)
    window._delete_folder(TEST)
    assert _pump(qapp, lambda: not window.deleting_folder(), timeout=5)
    assert window.status.text() == "Folder not deleted."
    assert _tree(shared_demo_browser()) == before
    assert window.refresh_btn.isEnabled()


def test_a_folder_written_minutes_ago_is_refused_before_asking(window, qapp, monkeypatch):
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    browser = shared_demo_browser()
    browser.add_file("growing.wav", TEST, payload=b"g" * 10, modified=datetime.now())
    before = _tree(browser)
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: pytest.fail("must not ask"))
    _go(window, qapp, ROOT)
    window._delete_folder(TEST)
    assert _pump(qapp, lambda: not window.deleting_folder(), timeout=5)
    assert window.status.text() == "Folder not deleted: written in the last 2 min."
    assert _tree(browser) == before


def test_a_recording_that_starts_during_the_confirmation_stops_the_deletion(window, qapp, monkeypatch):
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    before = _tree(shared_demo_browser())

    def confirm(plan):
        window.fake_host.capture = "Recording is running"
        return True

    monkeypatch.setattr(window, "_confirm_folder_delete", confirm)
    _go(window, qapp, ROOT)
    window._delete_folder(TEST)
    assert _pump(qapp, lambda: not window.deleting_folder(), timeout=5)
    assert window.status.text() == "Folder not deleted: recording in progress."
    assert _tree(shared_demo_browser()) == before


def test_partial_failure_is_shown_and_the_folder_stays_listed(window, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    browser = shared_demo_browser()
    browser.set_remove_failure(f"{TEST}/take1.wav", "Permission denied")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text, *a, **k: warnings.append((title, text)))
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: True)
    _go(window, qapp, ROOT)
    window._delete_folder(TEST)
    assert _pump(qapp, lambda: not window.deleting_folder() and not window._busy, timeout=8)
    assert window.status.text() == "Deleted 4 of 5 items. 1 could not be deleted: take1.wav (Permission denied). The folder was kept."
    assert len(warnings) == 1 and f"{TEST}/take1.wav: Permission denied" in warnings[0][1]
    _row(window, TEST)
    assert [entry.name for entry in browser.listdir(TEST)] == ["take1.wav"]
    assert window.activity_lines == [f"Folder {NAME} was only partly deleted. See details."]
    _check_operator_text(window.activity_lines)


def test_delete_key_on_one_folder_row_asks_for_that_folder(window, qapp, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: asked.append(plan.folder) or False)
    monkeypatch.setattr(window.table, "hasFocus", lambda: True)
    _go(window, qapp, ROOT)
    window.table.clearSelection()
    _row(window, TEST).setSelected(True)
    window._delete_selected_remote()
    assert _pump(qapp, lambda: not window.deleting_folder(), timeout=5)
    assert asked == [TEST]


def test_cancel_button_stops_a_running_deletion(window, qapp, monkeypatch):
    from crfs_iq_recorder.demo_sftp import shared_demo_browser

    browser = shared_demo_browser()
    monkeypatch.setattr(window, "_confirm_folder_delete", lambda plan: True)

    def slow(_path):
        time.sleep(0.15)

    browser.set_before_remove(slow)
    _go(window, qapp, ROOT)
    window._delete_folder(TEST)
    assert _pump(qapp, lambda: window.progress.maximum() > 0, timeout=5)
    window.cancel_btn.click()
    assert _pump(qapp, lambda: not window.deleting_folder() and not window._busy, timeout=8)
    browser.set_before_remove(None)
    assert window.status.text().startswith("Stopped. Deleted ")
    assert window.status.text().endswith(" The folder was kept.")
    _row(window, TEST)


# ---------------------------------------------------------------------------- theme


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_menu_is_drawn_in_the_theme_colours(window, qapp, mode):
    from PySide6.QtGui import QColor

    from crfs_iq_recorder import theme

    previous = theme.current_mode()
    theme.apply_theme(qapp, mode)
    try:
        palette = theme.current_palette()
        sheet = qapp.styleSheet()
        assert "QMenuBar, QMenu {" not in sheet
        assert f"QMenu {{\n    color: {palette.text};\n    background: {palette.surface};" in sheet
        _go(window, qapp, TEST)
        window._busy = True  # a disabled Download row too
        try:
            menu = window._context_menu(_row(window, f"{TEST}/take0.wav"))
        finally:
            window._busy = False
        menu.ensurePolished()
        menu.adjustSize()
        image = menu.grab().toImage()
        background = QColor(image.pixelColor(image.width() // 2, 2))
        assert background.name() == QColor(palette.surface).name(), (mode, background.name())
        if mode == "dark":
            assert background.lightness() > 20  # not the plain black menu of 1.18
        menu.deleteLater()
    finally:
        theme.apply_theme(qapp, previous)
