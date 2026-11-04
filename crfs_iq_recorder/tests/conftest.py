"""Pytest config: keep tests off the real network."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def _blocked_socket(*_args, **_kwargs):
        raise RuntimeError("Tests must not open TCP sockets to real hosts.")

    def _blocked_request(*_args, **_kwargs):
        raise RuntimeError("Tests must not call requests.Session.request.")

    monkeypatch.setattr("socket.create_connection", _blocked_socket)
    monkeypatch.setattr("crfs_iq_recorder.api_client.socket.create_connection", _blocked_socket)
    monkeypatch.setattr("requests.sessions.Session.request", _blocked_request)

    def _blocked_sftp(*_args, **_kwargs):
        raise RuntimeError("Tests must not open real SFTP connections.")

    monkeypatch.setattr("crfs_iq_recorder.sftp_client.connect_sftp", _blocked_sftp)


@pytest.fixture(autouse=True, scope="session")
def stop_worker_threads_at_exit():
    """GUI tests leave sensor checks and previews running; finish them before
    the interpreter deletes their QThreads (Qt would abort with 0xC0000409)."""
    yield
    from crfs_iq_recorder.worker_threads import stop_worker_threads

    stop_worker_threads(timeout_s=15.0)


@pytest.fixture(autouse=True)
def release_closed_windows():
    """Delete the windows a test closed.

    Closed windows used to stay alive until the end of the session (signal
    lambdas keep the Python wrappers, Qt keeps the C++ widgets), so every later
    ``configure_appearance()`` and theme toggle re-polished hundreds of dead
    widgets and the suite slowed down test by test. Worker threads are finished
    first: deleting a window while one of its jobs runs is the 0xC0000409 crash.
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - Qt-free test runs
        return
    app = QApplication.instance()
    if app is None:
        return
    from PySide6.QtCore import QEvent

    from crfs_iq_recorder.worker_threads import stop_worker_threads

    stop_worker_threads(timeout_s=5.0)
    app.processEvents()
    for top in list(app.topLevelWidgets()):
        try:
            if top.isVisible() or top.parent() is not None:
                continue
            top.deleteLater()
        except RuntimeError:
            pass  # already gone
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def fresh_connect_pacer(monkeypatch):
    """Each test starts with no SSH connection paced or held back by an earlier test."""
    from crfs_iq_recorder import sftp_client

    monkeypatch.setattr(sftp_client, "connect_pacer", sftp_client.ConnectPacer())


@pytest.fixture(autouse=True)
def reset_demo_sftp():
    from crfs_iq_recorder.demo_sftp import reset_shared_demo_browser

    reset_shared_demo_browser()
    yield
    reset_shared_demo_browser()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """Keep every test out of the developer's own config folder (panel settings,
    cellular sessions, merge history)."""
    config = tmp_path / "config"
    monkeypatch.setattr("crfs_iq_recorder.paths.user_config_dir", lambda: config)
    monkeypatch.setattr("crfs_iq_recorder.gui.user_config_dir", lambda: config)
    monkeypatch.setattr("crfs_iq_recorder.merge_controller.merge_history_path", lambda: config / "merge_history.json")


@pytest.fixture(autouse=True)
def isolated_diagnostic_log():
    """Each test starts with an empty diagnostic log; its file (in the test's
    config folder) is closed afterwards so the folder can be removed."""
    from crfs_iq_recorder import diagnostic_log

    diagnostic_log.reset_for_tests()
    yield
    diagnostic_log.reset_for_tests()


@pytest.fixture(autouse=True)
def no_desktop_open(monkeypatch):
    """Explorer and default applications are never launched by tests; the calls are recorded."""
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr("crfs_iq_recorder.desktop_open.open_path", lambda path: opened.append(("open", str(path))) or True)
    monkeypatch.setattr("crfs_iq_recorder.desktop_open.reveal", lambda path: opened.append(("reveal", str(path))) or True)
    return opened


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "operator_text_unchecked: the test feeds deliberately bad text to the Activity panel"
    )


@pytest.fixture(autouse=True)
def operator_messages_follow_the_rules(monkeypatch, request):
    """Every Activity line and status message a test produces must pass tests/operator_rules.py.

    Only modules the test already imported are watched, so Qt-free tests stay Qt-free.
    """
    import sys

    seen: list[tuple[str, str]] = []
    gui = sys.modules.get("crfs_iq_recorder.gui")
    if gui is not None:
        window_log = gui.MainWindow.log
        window_msg = gui.MainWindow._set_msg

        def log(self, message, *, level="info"):
            seen.append(("Activity", gui.scrub_operator(str(message))))
            return window_log(self, message, level=level)

        def set_msg(self, text, *, error=False):
            seen.append(("Status", str(text)))
            return window_msg(self, text, error=error)

        monkeypatch.setattr(gui.MainWindow, "log", log)
        monkeypatch.setattr(gui.MainWindow, "_set_msg", set_msg)
    runner = sys.modules.get("crfs_iq_recorder.cellular_runner")
    if runner is not None:
        runner_log = runner.CellularRunner._log

        def runner_message(self, message, level="info"):
            seen.append(("Activity", str(message)))
            return runner_log(self, message, level)

        monkeypatch.setattr(runner.CellularRunner, "_log", runner_message)
    yield seen
    if request.node.get_closest_marker("operator_text_unchecked"):
        return
    from crfs_iq_recorder import diagnostic_log
    from operator_rules import problems

    # The Activity panel keeps an ordinary-word password ("pass") as a word; see scrub_operator.
    secrets = tuple(s for s in diagnostic_log._registered() if not diagnostic_log._ordinary_word(s))  # noqa: SLF001
    bad = []
    for where, text in seen:
        found = problems(text, secrets=secrets)
        if found:
            bad.append(f"{where}: {text!r}: {', '.join(found)}")
    if bad:
        pytest.fail("Operator messages break the rules:\n" + "\n".join(dict.fromkeys(bad)), pytrace=False)


@pytest.fixture(autouse=True)
def isolated_excel_log(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crfs_iq_recorder.paths.excel_log_path",
        lambda: tmp_path / "excel_log.json",
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.excel_log.excel_log_path",
        lambda: tmp_path / "excel_log.json",
    )
    monkeypatch.setattr("crfs_iq_recorder.excel_log.excel_has_workbook", lambda _path: False)
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_has_workbook", lambda _path: False)
    # The protocol-workbook queue asks Excel through this tri-state probe; a test
    # machine may well have Excel open, so it is never asked here.
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_workbook_state", lambda _path: "closed")
