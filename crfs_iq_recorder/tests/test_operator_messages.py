"""Operator messages are short field-engineer sentences; the detail is in the diagnostic log.

conftest.py checks every Activity line and status message the other tests
produce against `operator_rules`. This file tests the rules themselves, scans
the message texts written in the source, and checks that neither the
diagnostic log nor "Copy diagnostic details" ever holds a stored secret.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from crfs_iq_recorder import diagnostic_log
from crfs_iq_recorder.capture_api import CaptureSummary
from crfs_iq_recorder.diagnostic_log import diag, register_secret, scrub, scrub_operator
from crfs_iq_recorder.diagnostic_report import ADDRESS_MASK, OTHER_ADDRESS_MASK, build_report
from operator_rules import MAX_LENGTH, problems

PACKAGE = Path(__file__).resolve().parents[1] / "crfs_iq_recorder"
SECRET = "Zq9-test-pw"
SFTP_SECRET = "sftp-Kx7-test"
HOST = "10.20.30.40"

GOOD = [
    "Connected to CRFS 100-18.",
    "Recording row 3 of 15: 3300-3400 MHz for 2 s.",
    "Sensor finished. Waiting for files.",
    "Recording confirmed: 2 files, 1.98 s.",
    "Partial recording: requested 2 s, received 1.4 s.",
    "CRFS rejected the request. See details.",
    "Excel is busy. Update queued.",
    "Next recording in 10 s.",
    "Merged file_063929: file_063929_merged.wav, 1.20 s. Originals kept.",
    "Deleted iq_20261103_171321_0001.wav from the sensor.",
]

BAD = [
    ("x" * (MAX_LENGTH + 1), "longer than"),
    ("Recording failed\nsecond line", "more than one line"),
    ("window_for returned 3 windows", "program name"),
    ("Traceback (most recent call last):", "exception"),
    ("[WinError 10054] An existing connection was forcibly closed", "exception"),
    ("SftpError: Could not open the file", "exception"),
    ('File "gui.py", line 12, in <module>', "exception"),
    (r"Saved C:\Users\me\AppData\Local\Temp\x.wav", "temporary"),
    ("Login with password=hunter22", "credential"),
    ("GET http://admin:hunter22@sensor/emp/", "credential"),
    ("Sensor sent RemoteTimeData", "internal term"),
    ("Capture budget exceeded", "internal term"),
]


@pytest.mark.parametrize("text", GOOD)
def test_field_engineer_messages_pass(text):
    assert problems(text) == []


@pytest.mark.parametrize(("text", "expected"), BAD)
def test_technical_text_is_refused(text, expected):
    found = problems(text)
    assert any(expected in item for item in found), found


def test_a_stored_password_is_refused_where_it_stands_as_a_word():
    assert problems(f"Sensor login uses {SECRET}", secrets=(SECRET,))
    assert problems("Sensor ready.", secrets=(SECRET,)) == []


# ----------------------------------------------------------------------------- source scan


def _literal_text(node: ast.AST) -> str | None:
    """The written part of a message: a string, or an f-string with each value as "X"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "X" for v in node.values)
    return None


def _operator_literals():
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "emit":
                owner = func.value
                if not (isinstance(owner, ast.Attribute) and owner.attr in {"log", "activity"}):
                    continue
            elif name not in {"log", "_log", "_host_log", "_set_msg"}:
                continue
            text = _literal_text(node.args[0])
            if text:
                yield f"{path.name}:{node.lineno}", text


def test_every_activity_message_written_in_the_source_follows_the_rules():
    literals = list(_operator_literals())
    assert len(literals) > 60, "the scan found the Activity messages"
    bad = [f"{where}: {text!r}: {', '.join(problems(text))}" for where, text in literals if problems(text)]
    assert bad == []


# ----------------------------------------------------------------------------- diagnostic log


def test_the_diagnostic_log_keeps_the_payload_and_never_a_stored_password():
    register_secret(SECRET)
    diag(
        "http_request",
        f"POST with password {SECRET}",
        payload={"center_frequency": 3_350_000_000, "bandwidth": 100_000_000, "password": SECRET},
        headers={"Authorization": "Basic YWRtaW46WnE5LXRlc3QtcHc=", "Accept": "text/event-stream"},
        url=f"http://admin:{SECRET}@sensor.example/emp/?token=abc123def&x=1",
        http_secret="AQAAANCMnd8BFdERjHoAwE",
    )
    diag("sftp_failed", f"Authentication failed (password={SECRET})", error=f"Traceback: login {SECRET!r}")
    diag("sse", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
    on_disk = diagnostic_log.log_path().read_text(encoding="utf-8")
    kept = "\n".join(diagnostic_log.recent(10))
    for blob in (on_disk, kept):
        assert SECRET not in blob
        assert "YWRtaW46" not in blob and "abc123def" not in blob and "eyJhbGci" not in blob
        assert "AQAAANCMnd8" not in blob, "the protected form of a stored password"
        assert '"center_frequency": 3350000000' in blob, "the request itself is kept"
        assert "Traceback" in blob, "technical detail belongs here"


def test_an_ordinary_word_password_is_masked_in_the_log_but_kept_in_activity_sentences():
    register_secret("pass")
    assert scrub("Next pass at 14:00; 3 passes") == "Next *** at 14:00; 3 passes"
    assert scrub_operator("Next pass at 14:00") == "Next pass at 14:00"
    assert scrub_operator("password=pass") == "password=***"
    assert "pass@" not in scrub_operator("http://admin:pass@sensor/emp/")


# ----------------------------------------------------------------------------- copied details


def _summary() -> CaptureSummary:
    return CaptureSummary(
        outcome="confirmed",
        requested_s=2.0,
        parts=1,
        request_sent=True,
        started_at=datetime(2026, 11, 3, 17, 14, 5, tzinfo=timezone.utc),
        finished_at=datetime(2026, 11, 3, 17, 14, 11, tzinfo=timezone.utc),
        payload={
            "center_frequency": 3_550_000_000,
            "bandwidth": 100_000_000,
            "duration": 2.0,
            "task_id": "row3",
            "recording_format": "WAVE",
        },
        result="partial",
        result_reason="shorter than requested",
        measurement={
            "parts": [
                {
                    "path": "/mnt/1/remdata/20261103/iq_20261103_171405_0001.wav",
                    "duration_s": 1.4,
                    "centre_hz": 3.55e9,
                    "bandwidth_hz": 1e8,
                    "sample_rate_hz": 1.25e8,
                    "signal_time": "2026-11-03T17:14:05+00:00",
                }
            ]
        },
        sensor_reply={"center_frequency": 3_550_000_000, "bandwidth": 100_000_000, "file_count": 1},
        finalized_paths=["/mnt/1/remdata/20261103/iq_20261103_171405_0001.wav"],
        sensor_warnings=["The internal clock is used"],
        detail=f"login {SECRET} at {HOST}",
    )


def test_copied_details_hold_the_request_and_never_a_secret_or_an_address():
    register_secret(SECRET)
    diag("http_request", f"POST http://{HOST}/emp/ as admin:{SFTP_SECRET}", payload={"password": SECRET})
    diag("route", "this computer is 192.0.2.57 on the sensor network")
    sensor = SimpleNamespace(model="RFeye Node 100-18", serial="rfeye100400", firmware="2.25-325", demo=False)
    text = build_report(
        version="1.13.0",
        sensor=sensor,
        summary=_summary(),
        diagnostic_lines=diagnostic_log.recent(30),
        secrets=[SECRET, SFTP_SECRET],
        hosts=[HOST],
    )
    assert SECRET not in text and SFTP_SECRET not in text
    assert HOST not in text and "192.0.2.57" not in text
    assert ADDRESS_MASK in text and OTHER_ADDRESS_MASK in text
    assert "CRFS IQ Recorder 1.13.0" in text
    assert "rfeye100400" in text and "2.25-325" in text and "address not included" in text
    assert '"center_frequency": 3550000000' in text and '"duration": 2.0' in text
    assert "Requested: 100 MHz for 2 s" in text
    assert "Result: partial (shorter than requested)" in text
    assert "Received: 1 file, 1.40 s" in text
    assert "3500-3600 MHz (100 MHz)" in text
    assert "local (2026-11-03 17:14:05 UTC)" in text
    assert os.path.expanduser("~") not in text


def test_the_sensor_position_from_a_reply_body_is_never_written_out():
    """Hardware, 3 Nov: CRFS puts its GPS fix into every RemoteTimeData reply; the reply body went
    into the diagnostic log, and from there into copied details."""
    body = 'time.RemoteTimeData: [{"GPS":{"latitude":12.345678,"longitude":-98.765432,"altitude":100.2},"duration":9.0}]'
    diag("http_outcome", "finished", body=body)
    line = diagnostic_log.recent(1)[0]
    for value in ("12.345678", "98.765432", "100.2"):
        assert value not in line
    assert '\\"latitude\\":<location>' in line and '\\"duration\\":9.0' in line
    assert scrub("latitude = 12.5, longitude: 3") == "latitude = <location>, longitude: <location>"
    sensor = SimpleNamespace(model="RFeye Node 100-18", serial="rfeye100400", firmware="2.25-325", demo=False)
    text = build_report(version="1.13.0", sensor=sensor, summary=_summary(), diagnostic_lines=[line], secrets=[], hosts=[])
    assert "12.345678" not in text and "98.765432" not in text


def test_copy_details_in_the_window_masks_the_stored_passwords(qapp):
    from PySide6.QtWidgets import QApplication

    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window._conn.http_password = SECRET
        window._conn.sftp_password = SFTP_SECRET
        window._conn.host = HOST
        diag("http_request", f"POST http://{HOST}/emp/ password={SECRET} sftp {SFTP_SECRET}")
        window._copy_diagnostic_details()
        copied = QApplication.clipboard().text()
        assert copied.startswith("CRFS IQ Recorder ")
        assert SECRET not in copied and SFTP_SECRET not in copied and HOST not in copied
        assert "Diagnostic details copied." in window.log_edit.toPlainText()
    finally:
        window.close()
        qapp.processEvents()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app
