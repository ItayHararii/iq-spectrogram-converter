"""What a recording holds is read from its files; the result class follows from that alone."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crfs_iq_recorder.capture_measure import (
    PARTIAL_TOLERANCE,
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_PARTIAL,
    RESULT_UNCONFIRMED,
    CaptureMeasurement,
    PartMeasurement,
    classify,
    coverage_differs,
    coverage_text,
    local_time,
    measure_part,
    measure_parts,
    received_text,
)
from crfs_iq_recorder.iq_wav import crfs_style_wav_bytes

FOLDER = "/mnt/1/remdata/20261103"


def _wav(tmp_path, name, *, frames, rate=1000, centre=3.55e9, bandwidth=1e8, when="2026-11-03T17:14:05.001Z"):
    path = tmp_path / name
    path.write_bytes(
        crfs_style_wav_bytes(frames=frames, sample_rate=rate, center_hz=centre, bandwidth_hz=bandwidth, signal_time=when)
    )
    return path


def _part(name: str, seconds: float | None, **kwargs) -> PartMeasurement:
    return PartMeasurement(path=f"{FOLDER}/{name}", duration_s=seconds, **kwargs)


def test_duration_and_metadata_come_from_the_file(tmp_path):
    path = _wav(tmp_path, "iq_20261103_171405_0001.wav", frames=1400)
    part = measure_part(None, str(path))
    assert part.duration_s == pytest.approx(1.4)
    assert part.sample_rate_hz == 1000
    assert part.centre_hz == 3.55e9 and part.bandwidth_hz == 1e8
    assert part.signal_time == datetime(2026, 11, 3, 17, 14, 5, 1000, tzinfo=timezone.utc)
    assert part.final and not part.truncated and part.problems == []
    measurement = CaptureMeasurement(parts=[part])
    assert received_text(measurement, 1) == "1 file, 1.40 s"
    assert coverage_text(measurement) == "3500-3600 MHz (100 MHz)"


def test_split_parts_add_up_and_the_earliest_start_is_the_recording_start(tmp_path):
    first = _wav(tmp_path, "iq_20261103_171405_0001.wav", frames=1000, when="2026-11-03T17:14:05Z")
    second = _wav(tmp_path, "iq_20261103_171405_0002.wav", frames=500, when="2026-11-03T17:14:06Z")
    measurement = measure_parts(None, [str(first), str(second)])
    assert measurement.duration_s == pytest.approx(1.5)
    assert measurement.signal_time == datetime(2026, 11, 3, 17, 14, 5, tzinfo=timezone.utc)
    assert received_text(measurement, 2) == "2 files, 1.50 s"
    restored = CaptureMeasurement.from_dict(measurement.to_dict())
    assert restored.duration_s == pytest.approx(1.5) and restored.signal_time == measurement.signal_time


def test_a_wide_request_reports_what_the_metadata_shows(tmp_path):
    """Seen on the Node 100-18: 3300-3800 MHz requested, 3500-3600 MHz recorded."""
    path = _wav(tmp_path, "iq_20261103_171405_0001.wav", frames=1000, centre=3.55e9, bandwidth=1e8)
    measurement = measure_parts(None, [str(path)])
    assert coverage_differs(measurement, 3_300_000_000, 3_800_000_000)
    assert not coverage_differs(measurement, 3_500_000_000, 3_600_000_000)


def test_an_unreadable_or_unfinished_file_stays_unknown(tmp_path):
    other = tmp_path / "iq_20261103_171405_0001.wav"
    other.write_bytes(b"not a wave file at all")
    part = measure_part(None, str(other))
    assert part.duration_s is None and part.problems
    empty = _wav(tmp_path, "iq_20261103_171406_0001.wav", frames=0)
    unfinished = measure_part(None, str(empty))
    assert not unfinished.final
    assert CaptureMeasurement(parts=[part]).duration_s is None
    assert received_text(CaptureMeasurement(parts=[part]), 1) == "1 file, duration unknown"
    assert coverage_text(None) == "unknown"


def test_a_cut_short_file_is_flagged(tmp_path):
    path = _wav(tmp_path, "iq_20261103_171405_0001.wav", frames=1000)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    part = measure_part(None, str(path))
    assert part.truncated
    verdict = classify(requested_s=1.0, measurement=CaptureMeasurement(parts=[part]))
    assert verdict.result == RESULT_PARTIAL


@pytest.mark.parametrize(
    ("kwargs", "result", "reason"),
    [
        (dict(request_sent=False), RESULT_FAILED, "not sent"),
        (dict(sensor_error="Unable to schedule the command"), RESULT_FAILED, "rejected by the sensor"),
        (dict(), RESULT_UNCONFIRMED, "no file confirmed"),
        (dict(sensor_error="lost", named_parts=["iq_a_0001.wav"]), RESULT_UNCONFIRMED, "no file confirmed"),
    ],
)
def test_no_files(kwargs, result, reason):
    verdict = classify(requested_s=2.0, measurement=None, **kwargs)
    assert (verdict.result, verdict.reason) == (result, reason)


def test_the_partial_tolerance_is_two_percent():
    assert PARTIAL_TOLERANCE == 0.02
    complete = CaptureMeasurement(parts=[_part("iq_a_0001.wav", 1.96)])
    short = CaptureMeasurement(parts=[_part("iq_a_0001.wav", 1.95)])
    assert classify(requested_s=2.0, measurement=complete).result == RESULT_COMPLETED
    verdict = classify(requested_s=2.0, measurement=short)
    assert (verdict.result, verdict.reason) == (RESULT_PARTIAL, "shorter than requested")
    # A Node 100-18 writes a little more than asked: 1.00565 s for 1 s.
    node = CaptureMeasurement(parts=[_part("iq_a_0001.wav", 1.00565)])
    assert classify(requested_s=1.0, measurement=node).result == RESULT_COMPLETED


def test_files_that_do_not_match_what_the_sensor_said_are_partial():
    one = CaptureMeasurement(parts=[_part("iq_a_0001.wav", 2.0)])
    missing = classify(requested_s=2.0, measurement=one, named_parts=["iq_a_0001.wav", "iq_a_0002.wav"])
    assert (missing.result, missing.reason) == (RESULT_PARTIAL, "missing iq_a_0002.wav")
    error = classify(requested_s=2.0, measurement=one, sensor_error="Receiver overflow")
    assert (error.result, error.reason) == (RESULT_PARTIAL, "sensor error during the recording")
    unknown = CaptureMeasurement(parts=[_part("iq_a_0001.wav", None)])
    assert classify(requested_s=2.0, measurement=unknown).result == RESULT_UNCONFIRMED
    assert classify(requested_s=2.0, measurement=unknown, sensor_error="x").result == RESULT_PARTIAL


def test_sensor_utc_becomes_local_wall_clock_time_for_the_workbook():
    utc = datetime(2026, 11, 3, 17, 14, 5, tzinfo=timezone.utc)
    local = local_time(utc)
    assert local.tzinfo is None
    assert local == utc.astimezone().replace(tzinfo=None)
    assert local_time(None) is None
