from datetime import datetime

from crfs_iq_recorder.recording_history import (
    add_recording,
    format_columns,
    iq_group_key,
    load_recordings,
    match_recordings_to_entries,
    new_recording,
    parse_iq_timestamp,
    remove_recording,
)
from crfs_iq_recorder.sftp_client import RemoteEntry


def test_parse_crfs_filename_timestamps():
    assert parse_iq_timestamp("iq_rfeye300539_20260813_181512_0001.wav") == datetime(2026, 8, 13, 18, 15, 12)
    assert parse_iq_timestamp("iq_20260805_081231_0001.wav") == datetime(2026, 8, 5, 8, 12, 31)
    assert parse_iq_timestamp("notes.txt") is None


def test_iq_group_key_strips_part_suffix():
    assert iq_group_key("iq_20260910_161629_0001.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_20260910_161629_0002.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_20260910_161629_0003.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_rfeye300539_20260813_181512_0001.wav") == "iq_rfeye300539_20260813_181512"
    assert iq_group_key("iq-demo.wav") == "iq-demo.wav"


def test_recording_history_roundtrip_omits_secrets(tmp_path):
    path = tmp_path / "recordings.json"
    record = new_recording(
        host="192.168.1.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=datetime(2026, 9, 10, 18, 30, 0),
    )
    add_recording(record, path)
    loaded = load_recordings(path)
    assert len(loaded) == 1
    assert loaded[0].start_hz == 790_000_000
    assert loaded[0].end_hz == 810_000_000
    assert loaded[0].center_hz == 800_000_000
    assert loaded[0].bandwidth_hz == 20_000_000
    assert loaded[0].duration_s == 4
    blob = path.read_text(encoding="utf-8")
    assert "password" not in blob
    remove_recording(record.id, path)
    assert load_recordings(path) == []


def test_match_iq_file_to_saved_parameters():
    started = datetime(2026, 9, 10, 18, 30, 0)
    record = new_recording(
        host="192.168.1.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    entry = RemoteEntry(
        "iq_rfeye300539_20260910_183000_0001.wav",
        "/mnt/1/remdata/20260910/iq_rfeye300539_20260910_183000_0001.wav",
        False,
        1000,
        started,
    )
    other = RemoteEntry(
        "iq_rfeye300539_20260910_120000_0001.wav",
        "/mnt/1/remdata/20260910/iq_rfeye300539_20260910_120000_0001.wav",
        False,
        1000,
        datetime(2026, 9, 10, 12, 0, 0),
    )
    matched = match_recordings_to_entries([entry, other], [record], host="192.168.1.12")
    assert matched[entry.path] == record
    assert other.path not in matched
    start, end, center, bw, duration = format_columns(record)
    assert start == "790"
    assert end == "810"
    assert center == "800"
    assert bw == "20"
    assert duration == "4"


def test_each_recording_matches_at_most_one_file_group():
    started = datetime(2026, 9, 10, 18, 30, 0)
    record = new_recording(
        host="192.168.1.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    first = RemoteEntry("iq_20260910_183000_0001.wav", "/a/iq_20260910_183000_0001.wav", False, 1, started)
    second = RemoteEntry("iq_20260910_183001_0001.wav", "/a/iq_20260910_183001_0001.wav", False, 1, started)
    matched = match_recordings_to_entries([first, second], [record], host="192.168.1.12")
    assert len(matched) == 1
    assert first.path in matched


def test_split_iq_parts_share_the_same_recording():
    started = datetime(2026, 9, 10, 19, 16, 20)
    record = new_recording(
        host="192.168.1.12",
        start_hz=5_000_000_000,
        end_hz=5_100_000_000,
        center_hz=5_050_000_000,
        bandwidth_hz=100_000_000,
        duration_s=4,
        started_at=started,
    )
    parts = [
        RemoteEntry(
            "iq_20260910_161629_0001.wav",
            "/mnt/1/remdata/20260910/iq_20260910_161629_0001.wav",
            False,
            838_042_176,
            datetime(2026, 9, 10, 19, 16, 37),
        ),
        RemoteEntry(
            "iq_20260910_161629_0002.wav",
            "/mnt/1/remdata/20260910/iq_20260910_161629_0002.wav",
            False,
            838_042_304,
            datetime(2026, 9, 10, 19, 16, 46),
        ),
        RemoteEntry(
            "iq_20260910_161629_0003.wav",
            "/mnt/1/remdata/20260910/iq_20260910_161629_0003.wav",
            False,
            326_836_928,
            datetime(2026, 9, 10, 19, 16, 54),
        ),
    ]
    matched = match_recordings_to_entries(parts, [record], host="192.168.1.12")
    assert set(matched) == {part.path for part in parts}
    assert all(matched[part.path] == record for part in parts)
    assert format_columns(record)[4] == "4"


def test_later_split_part_matches_without_first_file():
    started = datetime(2026, 9, 10, 19, 16, 20)
    record = new_recording(
        host="192.168.1.12",
        start_hz=5_000_000_000,
        end_hz=5_100_000_000,
        center_hz=5_050_000_000,
        bandwidth_hz=100_000_000,
        duration_s=4,
        started_at=started,
    )
    later = RemoteEntry(
        "iq_20260910_161629_0003.wav",
        "/mnt/1/remdata/20260910/iq_20260910_161629_0003.wav",
        False,
        326_836_928,
        datetime(2026, 9, 10, 19, 30, 0),
    )
    matched = match_recordings_to_entries([later], [record], host="192.168.1.12")
    assert matched[later.path] == record


def test_match_utc_filename_using_local_mtime():
    started = datetime(2026, 9, 10, 18, 42, 37)
    record = new_recording(
        host="192.168.1.12",
        start_hz=793_750_000,
        end_hz=806_250_000,
        center_hz=800_000_000,
        bandwidth_hz=12_500_000,
        duration_s=0.1,
        started_at=started,
    )
    entry = RemoteEntry(
        "iq_20260910_154239_0001.wav",
        "/mnt/1/remdata/20260910/iq_20260910_154239_0001.wav",
        False,
        8_380_992,
        datetime(2026, 9, 10, 18, 42, 43),
    )
    matched = match_recordings_to_entries([entry], [record], host="192.168.1.12")
    assert matched[entry.path] == record


def test_match_utc_filename_without_mtime():
    started = datetime(2026, 9, 10, 18, 42, 37)
    record = new_recording(
        host="192.168.1.12",
        start_hz=793_750_000,
        end_hz=806_250_000,
        center_hz=800_000_000,
        bandwidth_hz=12_500_000,
        duration_s=0.1,
        started_at=started,
    )
    entry = RemoteEntry(
        "iq_20260910_154239_0001.wav",
        "/mnt/1/remdata/20260910/iq_20260910_154239_0001.wav",
        False,
        8_380_992,
        None,
    )
    matched = match_recordings_to_entries([entry], [record], host="192.168.1.12")
    assert matched[entry.path] == record
