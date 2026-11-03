"""What the operator sees after a recording: requested next to received, separate states."""

from __future__ import annotations

from types import SimpleNamespace

from crfs_iq_recorder.capture_measure import PARTIAL_TOLERANCE
from crfs_iq_recorder.result_view import (
    DOWNLOAD_FAILED,
    ON_SENSOR,
    SAVED_LOCALLY,
    local_copies,
    transfer_state,
    view_from_summary,
    view_from_task,
)

MHZ = 1_000_000
PATHS = ["/mnt/1/remdata/20261103/iq_20261103_171321_0001.wav", "/mnt/1/remdata/20261103/iq_20261103_171321_0002.wav"]


def _part(path: str, seconds: float | None, *, centre=3550 * MHZ, bandwidth=100 * MHZ) -> dict:
    return {"path": path, "size": 1000, "duration_s": seconds, "centre_hz": centre, "bandwidth_hz": bandwidth}


def _summary(result: str, parts: list[dict], *, requested=2.0, bandwidth=100 * MHZ, centre=3550 * MHZ) -> dict:
    return {
        "payload": {"center_frequency": centre, "bandwidth": bandwidth, "duration": requested},
        "requested_s": requested,
        "result": result,
        "finalized_paths": [p["path"] for p in parts],
        "measurement": {"parts": parts},
    }


def test_complete_recording_shows_requested_received_and_the_recorded_range():
    view = view_from_summary(_summary("completed", [_part(PATHS[0], 1.2), _part(PATHS[1], 0.8)]), title="3500-3600 MHz")
    assert view.requested == "Requested: 100 MHz for 2 s"
    assert view.received == "Received: 2 files, 2.00 s"
    assert view.recorded == "Recorded: 3500-3600 MHz (100 MHz)"
    assert (view.tag, view.tag_style) == ("Complete", "badgeOk")
    assert view.states_text == "Files ready · On the sensor"
    assert not view.local_ready


def test_short_recording_is_tagged_partial_and_the_tolerance_is_two_percent():
    assert PARTIAL_TOLERANCE == 0.02
    view = view_from_summary(_summary("partial", [_part(PATHS[0], 1.4)]))
    assert view.received == "Received: 1 file, 1.40 s"
    assert (view.tag, view.tag_style) == ("Partial", "badgeWait")
    assert view.recording_state == "Files ready"


def test_unread_metadata_reads_unknown_never_the_requested_values():
    parts = [{"path": PATHS[0], "size": 1000, "duration_s": None}]
    view = view_from_summary(_summary("unconfirmed", parts))
    assert view.received == "Received: 1 file, duration unknown"
    assert view.recorded == "Recorded range: unknown"
    assert (view.tag, view.recording_state) == ("Uncertain", "Not confirmed")


def test_a_wider_request_recorded_narrower_says_so():
    """Node 100-18: a 500 MHz request (3300-3800 MHz) is recorded as 100 MHz around the centre."""
    parts = [_part(PATHS[0], 2.0, centre=3550 * MHZ, bandwidth=100 * MHZ)]
    view = view_from_summary(_summary("completed", parts, bandwidth=500 * MHZ))
    assert view.requested == "Requested: 500 MHz for 2 s"
    assert view.recorded == "Recorded: 3500-3600 MHz (100 MHz), not the requested range"
    # Counted as completed (time and data), but the badge does not claim the requested range.
    assert view.result == "completed"
    assert (view.tag, view.tag_style) == ("Other range", "badgeWait")


def test_no_files_is_failed_with_no_transfer_state():
    view = view_from_summary(_summary("failed", []))
    assert view.received == "Received: no files" and view.recorded == "Recorded range: unknown"
    assert (view.tag, view.tag_style) == ("Failed", "badgeBad")
    assert view.transfer_state == "" and view.states_text == "Not recorded"


def test_local_copies_count_only_the_recording_s_files_in_the_folder(tmp_path):
    (tmp_path / "iq_20261103_171321_0001.wav").write_bytes(b"x" * 10)
    (tmp_path / "iq_20261103_171321_0002.wav").write_bytes(b"")  # an empty leftover is not a copy
    assert local_copies(PATHS, tmp_path) == [str(tmp_path / "iq_20261103_171321_0001.wav")]
    assert local_copies(PATHS, None) == []
    (tmp_path / "iq_20261103_171321_0002.wav").write_bytes(b"y")
    view = view_from_summary(_summary("completed", [_part(PATHS[0], 1.0), _part(PATHS[1], 1.0)]), local_dir=tmp_path)
    assert view.local_ready and view.transfer_state == SAVED_LOCALLY


def test_transfer_state_follows_the_task_files():
    assert transfer_state([]) == ""
    assert transfer_state([{"download_status": "verified"}, {"download_status": "verified"}]) == SAVED_LOCALLY
    assert transfer_state([{"download_status": "verified"}, {"download_status": "downloading"}]) == "Downloading 1 of 2"
    assert transfer_state([{"download_status": "present"}]) == "Downloading"
    assert transfer_state([{"download_status": "verified"}, {"download_status": "failed"}]) == DOWNLOAD_FAILED
    assert transfer_state([{"download_status": "not_requested"}]) == ON_SENSOR


def test_task_view_keeps_recording_transfer_and_excel_apart(tmp_path):
    local = tmp_path / "iq_20261103_171321_0001.wav"
    local.write_bytes(b"x")
    state = SimpleNamespace(
        summary=_summary("completed", [_part(PATHS[0], 2.0)]),
        files=[{"name": local.name, "download_status": "verified", "local_path": str(local)}],
        result="completed",
        attempts=2,
    )
    view = view_from_task(state, title="Row 1 of 1: 3500-3600 MHz", bandwidth_hz=100 * MHZ, centre_hz=3550 * MHZ,
                          seconds=2.0, excel_state="Excel update pending")
    assert view.states_text == "Files ready · Saved locally · Excel update pending"
    assert view.local_paths == [str(local)] and view.attempt == 2
    rejected = SimpleNamespace(summary={}, files=[], result="failed", attempts=1)
    view = view_from_task(rejected, title="Row 1 of 1", bandwidth_hz=100 * MHZ, centre_hz=3550 * MHZ, seconds=2.0)
    assert view.requested == "Requested: 100 MHz for 2 s" and view.received == "Received: no files"
    assert view.tag == "Failed" and view.transfer_state == ""
