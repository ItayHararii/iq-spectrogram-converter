from crfs_iq_recorder.connection_monitor import (
    CHECKING,
    CONNECTED,
    DISCONNECTED,
    IDLE,
    LINK_CHECK_INTERVAL_MS,
    LINK_CHECK_TIMEOUT_S,
    RECONNECTING,
    LinkMonitor,
    connection_identity,
)


def test_timeout_and_interval_are_in_range():
    assert LINK_CHECK_INTERVAL_MS == 10_000
    assert 3.0 <= LINK_CHECK_TIMEOUT_S <= 5.0


def test_identity_changes_with_host_and_password():
    left = connection_identity("192.0.2.10", "80", False, "admin", "pass", False)
    right = connection_identity("192.0.2.11", "80", False, "admin", "pass", False)
    other_pw = connection_identity("192.0.2.10", "80", False, "admin", "other", False)
    assert left != right
    assert left != other_pw
    assert left == connection_identity("192.0.2.10", "80", False, "admin", "pass", False)


def test_success_failure_restore_labels():
    monitor = LinkMonitor()
    monitor.reset((), demo=False, has_host=True)
    assert monitor.state == CHECKING
    assert monitor.label() == "Checking…"
    assert monitor.style() == "wait"
    assert monitor.note_success() is True
    assert monitor.state == CONNECTED
    assert monitor.label() == "Connected"
    assert monitor.style() == "ok"
    assert monitor.note_success() is False
    assert monitor.note_failure() is True
    assert monitor.state == RECONNECTING
    assert monitor.label() == "Reconnecting..."
    assert monitor.style() == "wait"
    assert monitor.note_failure() is True
    assert monitor.state == DISCONNECTED
    assert monitor.label() == "Disconnected"
    assert monitor.style() == "bad"
    assert monitor.note_failure() is False
    assert monitor.state == DISCONNECTED
    assert monitor.note_success() is True
    assert monitor.state == CONNECTED
    assert monitor.label() == "Connected"


def test_demo_stays_labeled_demo_when_connected():
    monitor = LinkMonitor()
    monitor.reset((), demo=True, has_host=True)
    assert monitor.state == CONNECTED
    assert monitor.label() == "Demo"
    assert monitor.note_success() is False
    assert monitor.label() == "Demo"


def test_idle_without_host():
    monitor = LinkMonitor()
    monitor.reset((), demo=False, has_host=False)
    assert monitor.state == IDLE
    assert monitor.label() == "No sensor IP"
    assert monitor.style() == "idle"
