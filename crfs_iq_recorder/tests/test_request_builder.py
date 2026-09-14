import json

from crfs_iq_recorder.frequency import from_center_bandwidth
from crfs_iq_recorder.request_builder import (
    build_payload_for_recording_time,
    build_recording_payload,
    default_payload,
)


EXPECTED_DEFAULT = {
    "remote_recording_scans": [
        {
            "center_frequency": 800000000,
            "bandwidth": 12500000,
            "task_id": "iq-testing",
            "duration": 0.1,
            "rate": 0.1,
            "capture_length": 0.1,
            "recording_format": "WAVE",
        }
    ]
}


def test_default_payload_exact_dict():
    assert default_payload() == EXPECTED_DEFAULT


def test_default_payload_json_types():
    encoded = json.dumps(default_payload(), separators=(",", ":"))
    assert encoded == json.dumps(EXPECTED_DEFAULT, separators=(",", ":"))
    parsed = json.loads(encoded)
    scan = parsed["remote_recording_scans"][0]
    assert isinstance(scan["center_frequency"], int)
    assert isinstance(scan["bandwidth"], int)
    assert isinstance(scan["duration"], float)
    assert scan["duration"] == 0.1
    assert scan["recording_format"] == "WAVE"


def test_key_order_matches_contract():
    scan = default_payload()["remote_recording_scans"][0]
    assert list(scan.keys()) == [
        "center_frequency",
        "bandwidth",
        "task_id",
        "duration",
        "rate",
        "capture_length",
        "recording_format",
    ]


def test_gui_only_recorded_time_is_not_in_payload():
    payload = build_recording_payload(
        from_center_bandwidth(800_000_000, 12_500_000),
        task_id="iq-testing",
        duration=0.1,
        rate=0.1,
        capture_length=0.1,
        recording_format="WAVE",
    )
    blob = json.dumps(payload)
    assert "recorded_time" not in blob
    assert "password" not in blob
    assert "username" not in blob


def test_recording_seconds_copied_to_all_timing_fields():
    payload = build_payload_for_recording_time(
        from_center_bandwidth(800_000_000, 12_500_000),
        2.5,
    )
    scan = payload["remote_recording_scans"][0]
    assert scan["duration"] == 2.5
    assert scan["rate"] == 2.5
    assert scan["capture_length"] == 2.5
    assert scan["duration"] == scan["rate"] == scan["capture_length"]
    assert scan["recording_format"] == "WAVE"
    assert json.loads(json.dumps(scan))["duration"] == 2.5


def test_recording_seconds_payload_can_use_verified_crfs_format():
    payload = build_payload_for_recording_time(
        from_center_bandwidth(800_000_000, 12_500_000),
        2.5,
        recording_format="XDAT",
    )
    assert payload["remote_recording_scans"][0]["recording_format"] == "XDAT"


def test_mhz_values_convert_to_hz_in_payload():
    from crfs_iq_recorder.frequency import parse_to_integer_hz

    center = parse_to_integer_hz("800", "MHz", field="center")
    bandwidth = parse_to_integer_hz("12.5", "MHz", field="bandwidth")
    payload = build_payload_for_recording_time(from_center_bandwidth(center, bandwidth), 0.1)
    scan = payload["remote_recording_scans"][0]
    assert scan["center_frequency"] == 800_000_000
    assert scan["bandwidth"] == 12_500_000


def test_unique_task_id_is_timestamped_and_distinct():
    from datetime import datetime

    from crfs_iq_recorder.request_builder import unique_task_id

    first = unique_task_id(datetime(2026, 9, 10, 21, 30, 5))
    second = unique_task_id(datetime(2026, 9, 10, 21, 30, 6))
    assert first == "iq-20260910-213005"
    assert second == "iq-20260910-213006"
    payload = build_payload_for_recording_time(
        from_center_bandwidth(800_000_000, 12_500_000),
        2.5,
        task_id=first,
    )
    scan = payload["remote_recording_scans"][0]
    assert scan["task_id"] == first
    assert scan["duration"] == scan["rate"] == scan["capture_length"] == 2.5


def test_unverified_format_identifier_is_still_sent_verbatim():
    payload = build_recording_payload(
        from_center_bandwidth(800_000_000, 12_500_000),
        task_id="iq-testing",
        duration=0.1,
        rate=0.1,
        capture_length=0.1,
        recording_format="BIN",
    )
    assert payload["remote_recording_scans"][0]["recording_format"] == "BIN"
