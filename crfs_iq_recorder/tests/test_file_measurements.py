"""What was read from each sensor file is shown as such; anything not read is "unknown"."""

from __future__ import annotations

from crfs_iq_recorder.file_measurements import load_measurements, lookup, record_parts, recorded_columns
from crfs_iq_recorder.operator_text import size_detail, size_text
from crfs_iq_recorder.sensor_storage import format_storage_bytes
from crfs_iq_recorder.theme import format_bytes

FOLDER = "/mnt/1/remdata/20261104"


def _part(name, size, seconds, rate, centre, bandwidth, problems=()):
    return {"path": f"{FOLDER}/{name}", "size": size, "duration_s": seconds, "sample_rate_hz": rate,
            "centre_hz": centre, "bandwidth_hz": bandwidth, "problems": list(problems)}


# Read on the RFeye Node 100-18 (4 Nov 2026, 0.5 s requests): the 832-862 MHz row and the
# 3300-3800 MHz row, which the sensor recorded as 100 MHz around 3550 MHz.
ROW_30 = _part("iq_20261104_085101_0001.wav", 125_706_816, 0.502824448, 62.5e6, 847e6, 30e6)
ROW_500 = _part("iq_20261104_085608_0001.wav", 251_413_056, 0.502824704, 125e6, 3550e6, 100e6)


def test_readings_are_kept_by_the_sensor_path_and_apply_only_at_the_size_they_were_read(tmp_path):
    store_file = tmp_path / "m.json"
    assert record_parts("192.0.2.8", [ROW_30, ROW_500], path=store_file) == 2
    assert record_parts("192.0.2.9", [ROW_30], path=store_file) == 0, "the same reading is not written twice"
    store = load_measurements(store_file)
    assert lookup(store, ROW_30["path"], 125_706_816)["sample_rate_hz"] == 62.5e6
    assert lookup(store, ROW_30["path"], 125_706_000) is None, "another size is another file state"
    assert lookup(store, f"{FOLDER}/other.wav", 1) is None


def test_unread_or_unfinished_files_are_not_kept(tmp_path):
    store_file = tmp_path / "m.json"
    parts = [
        _part("a.wav", 1000, None, None, None, None),
        _part("b.wav", 0, 0.5, 1e6, 1e9, 1e6),
        _part("c.wav", 1000, 0.5, 1e6, 1e9, 1e6, problems=["data chunk truncated"]),
        _part("d.wav", 1000, 0.5, 1e6, 1e9, 1e6, problems=["still being written"]),
    ]
    assert record_parts("h", parts, path=store_file) == 0
    assert not store_file.exists()


def test_recorded_columns_show_the_file_values_and_never_the_requested_ones():
    assert recorded_columns([ROW_30], is_iq=True) == ["832–862", "0.5028", "62.5"]
    assert recorded_columns([ROW_500], is_iq=True) == ["3500–3600", "0.5028", "125"], "500 MHz requested, 100 MHz recorded"
    second = dict(ROW_500, path=f"{FOLDER}/iq_20261104_085608_0002.wav")
    assert recorded_columns([ROW_500, second], is_iq=True) == ["3500–3600", "1.0056", "125"]
    assert recorded_columns([ROW_500, None], is_iq=True) == ["unknown"] * 3, "one part not read: unknown"
    assert recorded_columns([ROW_30, ROW_500], is_iq=True)[0] == "unknown", "parts that disagree are not merged"
    assert recorded_columns([], is_iq=True) == ["unknown"] * 3
    assert recorded_columns([ROW_30], is_iq=False) == ["", "", ""]


def test_a_downloaded_copy_gives_a_reading_of_a_file_the_app_never_read(tmp_path):
    """A file recorded by another program or an older version gets its reading from the copy."""
    from crfs_iq_recorder.file_measurements import reading_from_local_copy
    from crfs_iq_recorder.iq_wav import crfs_style_wav_bytes

    data = crfs_style_wav_bytes(frames=2000, sample_rate=1000, center_hz=3550e6, bandwidth_hz=100e6,
                                signal_time="2026-11-04T08:56:08.000Z")
    local = tmp_path / "iq.wav"
    local.write_bytes(data)
    reading = reading_from_local_copy(f"{FOLDER}/iq.wav", local)
    assert reading["size"] == len(data) and reading["duration_s"] == 2.0 and reading["sample_rate_hz"] == 1000
    assert reading["centre_hz"] == 3550e6 and reading["bandwidth_hz"] == 100e6
    assert record_parts("h", [reading], path=tmp_path / "m.json") == 1
    local.write_bytes(b"not a wave file")
    assert reading_from_local_copy(f"{FOLDER}/iq.wav", local) is None


def test_sizes_use_one_decimal_convention_and_the_tooltip_gives_both():
    """Hardware, 4 Nov: the same 125,706,816-byte file showed as 125.7 MB in one place and
    119.9 MB (really MiB) in Sensor Files."""
    assert size_text(125_706_816) == "125.7 MB" == format_bytes(125_706_816)
    assert size_text(251_413_056) == "251.4 MB"
    assert size_detail(125_706_816) == "125,706,816 bytes (125.7 MB, 119.9 MiB)"
    assert size_text(0) == "0 B" and size_text(999) == "999 B" and size_text(15_404) == "15.4 kB"
    assert size_text(2_510_000_000) == "2.51 GB" and size_text(125_000_000) == "125 MB"
    assert format_storage_bytes(118_000_000_000) == "118 GB"
