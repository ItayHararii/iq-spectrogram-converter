from datetime import datetime

from crfs_iq_recorder.recording_history import (
    add_recording,
    format_columns,
    iq_group_key,
    live_capture_files,
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
    assert parse_iq_timestamp(
        "file_194326_0001.wav",
        folder_date="20260910",
    ) == datetime(2026, 9, 10, 19, 43, 26)
    from crfs_iq_recorder.recording_history import folder_date_from_path

    assert folder_date_from_path("/mnt/1/remdata/20260910/file_194326_0001.wav") == "20260910"


def test_iq_group_key_strips_part_suffix():
    assert iq_group_key("iq_20260910_161629_0001.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_20260910_161629_0002.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_20260910_161629_0003.wav") == "iq_20260910_161629"
    assert iq_group_key("iq_rfeye300539_20260813_181512_0001.wav") == "iq_rfeye300539_20260813_181512"
    assert iq_group_key("iq-demo.wav") == "iq-demo.wav"


def test_iq_filenames_join_split_and_dedupe():
    from crfs_iq_recorder.recording_history import join_iq_filenames, split_iq_filenames

    text = join_iq_filenames(["iq_x_0002.wav", "iq_x_0001.wav", "iq_x_0001.wav"])
    assert text == "iq_x_0001.wav\niq_x_0002.wav"
    assert split_iq_filenames(text) == ["iq_x_0001.wav", "iq_x_0002.wav"]


def test_expand_group_entries_includes_all_split_parts():
    from datetime import datetime

    from crfs_iq_recorder.recording_history import expand_group_entries

    now = datetime.now()
    part1 = RemoteEntry("iq_20260910_161629_0001.wav", "/r/iq_20260910_161629_0001.wav", False, 10, now)
    part2 = RemoteEntry("iq_20260910_161629_0002.wav", "/r/iq_20260910_161629_0002.wav", False, 20, now)
    other = RemoteEntry("iq-demo.wav", "/r/iq-demo.wav", False, 5, now)
    expanded = expand_group_entries([part2], [other, part2, part1])
    assert [entry.name for entry in expanded] == [
        "iq_20260910_161629_0001.wav",
        "iq_20260910_161629_0002.wav",
    ]


def test_recording_history_roundtrip_omits_secrets(tmp_path):
    path = tmp_path / "recordings.json"
    record = new_recording(
        host="192.0.2.12",
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
        host="192.0.2.12",
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
    matched = match_recordings_to_entries([entry, other], [record], host="192.0.2.12")
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
        host="192.0.2.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    first = RemoteEntry("iq_20260910_183000_0001.wav", "/a/iq_20260910_183000_0001.wav", False, 1, started)
    second = RemoteEntry("iq_20260910_183001_0001.wav", "/a/iq_20260910_183001_0001.wav", False, 1, started)
    matched = match_recordings_to_entries([first, second], [record], host="192.0.2.12")
    assert len(matched) == 1
    assert first.path in matched


def test_split_iq_parts_share_the_same_recording():
    started = datetime(2026, 9, 10, 19, 16, 20)
    record = new_recording(
        host="192.0.2.12",
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
    matched = match_recordings_to_entries(parts, [record], host="192.0.2.12")
    assert set(matched) == {part.path for part in parts}
    assert all(matched[part.path] == record for part in parts)
    assert format_columns(record)[4] == "4"


def test_later_split_part_matches_without_first_file():
    started = datetime(2026, 9, 10, 19, 16, 20)
    record = new_recording(
        host="192.0.2.12",
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
    matched = match_recordings_to_entries([later], [record], host="192.0.2.12")
    assert matched[later.path] == record


def test_match_utc_filename_using_local_mtime():
    started = datetime(2026, 9, 10, 18, 42, 37)
    record = new_recording(
        host="192.0.2.12",
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
    matched = match_recordings_to_entries([entry], [record], host="192.0.2.12")
    assert matched[entry.path] == record


def test_match_utc_filename_without_mtime():
    started = datetime(2026, 9, 10, 18, 42, 37)
    record = new_recording(
        host="192.0.2.12",
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
    matched = match_recordings_to_entries([entry], [record], host="192.0.2.12")
    assert matched[entry.path] == record


def test_metadata_survives_sensor_ip_change(tmp_path):
    started = datetime(2026, 9, 10, 18, 30, 0)
    record = new_recording(
        host="192.0.2.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
        iq_stem="iq_rfeye300539_20260910_183000",
    )
    add_recording(record, tmp_path / "recordings.json")
    loaded = load_recordings(tmp_path / "recordings.json")
    entry = RemoteEntry(
        "iq_rfeye300539_20260910_183000_0002.wav",
        "/mnt/1/remdata/20260910/iq_rfeye300539_20260910_183000_0002.wav",
        False,
        1000,
        datetime(2026, 9, 10, 18, 45, 0),
    )
    matched = match_recordings_to_entries([entry], loaded, host="192.0.2.10")
    assert matched[entry.path].id == record.id
    assert matched[entry.path].duration_s == 4
    assert matched[entry.path].center_hz == 800_000_000


def test_does_not_assign_metadata_to_unrelated_newest_file():
    started = datetime(2026, 9, 10, 12, 0, 0)
    record = new_recording(
        host="192.0.2.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    newest = RemoteEntry(
        "iq_20260910_220000_0001.wav",
        "/mnt/1/remdata/20260910/iq_20260910_220000_0001.wav",
        False,
        999,
        datetime(2026, 9, 10, 22, 0, 0),
    )
    matched = match_recordings_to_entries([newest], [record], host="192.0.2.12")
    assert newest.path not in matched


def test_persist_stem_then_match_later_parts(tmp_path):
    from crfs_iq_recorder.recording_history import persist_matched_stems

    started = datetime(2026, 9, 10, 18, 30, 0)
    record = new_recording(
        host="192.0.2.12",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    path = tmp_path / "recordings.json"
    add_recording(record, path)
    first = RemoteEntry(
        "iq_20260910_183000_0001.wav",
        "/a/iq_20260910_183000_0001.wav",
        False,
        1,
        started,
    )
    matched = match_recordings_to_entries([first], [record], host="192.0.2.1")
    persist_matched_stems(matched, path)
    loaded = load_recordings(path)
    assert loaded[0].iq_stem == "iq_20260910_183000"
    later = RemoteEntry(
        "iq_20260910_183000_0003.wav",
        "/a/iq_20260910_183000_0003.wav",
        False,
        1,
        datetime(2026, 9, 10, 19, 0, 0),
    )
    matched_later = match_recordings_to_entries([later], loaded, host="192.0.2.10")
    assert matched_later[later.path].id == record.id


def test_match_time_only_filename_with_folder_date():
    started = datetime(2026, 9, 10, 22, 43, 30)
    record = new_recording(
        host="192.0.2.10",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        duration_s=4,
        started_at=started,
    )
    entry = RemoteEntry(
        "file_194326_0001.wav",
        "/mnt/1/remdata/20260910/file_194326_0001.wav",
        False,
        251_413_044,
        None,
    )
    matched = match_recordings_to_entries([entry], [record], host="192.0.2.12")
    assert matched[entry.path] == record


def _record(started: datetime, *, stem: str = "", seconds: float = 3.0):
    from dataclasses import replace

    record = new_recording(
        host="192.0.2.10",
        start_hz=410_000_000,
        end_hz=412_000_000,
        center_hz=411_000_000,
        bandwidth_hz=2_000_000,
        duration_s=seconds,
        started_at=started,
    )
    return replace(record, iq_stem=stem) if stem else record


def _wav(name: str, when: datetime, size: int = 12_571_192) -> RemoteEntry:
    return RemoteEntry(name, f"/mnt/1/remdata/20261031/{name}", False, size, when)


def test_live_watch_does_not_adopt_a_file_owned_by_an_earlier_recording():
    # Seen on hardware: the POST was black-holed, yet a file from a recording
    # 178 s earlier fell inside the +-180 s window and was declared "finished".
    earlier = _record(datetime(2026, 10, 31, 21, 22, 5), stem="file_192206")
    current = _record(datetime(2026, 10, 31, 21, 25, 4))
    old_file = _wav("file_192206_0001.wav", datetime(2026, 10, 31, 21, 22, 6))
    assert live_capture_files([old_file], current, known=[earlier, current]) == []
    # Without the owner in the history the file still predates the request by far.
    assert live_capture_files([old_file], current, known=[current]) == []


def test_live_watch_uses_the_names_the_sensor_reported():
    current = _record(datetime(2026, 10, 31, 21, 3, 6), seconds=20)
    parts = [
        _wav("file_190307_0001.wav", datetime(2026, 10, 31, 21, 3, 15), 523_776_564),
        _wav("file_190307_0002.wav", datetime(2026, 10, 31, 21, 3, 23), 523_776_692),
        _wav("file_190307_0003.wav", datetime(2026, 10, 31, 21, 3, 27), 205_320_884),
    ]
    stranger = _wav("file_190218_0001.wav", datetime(2026, 10, 31, 21, 2, 20), 125_706_804)
    mine = live_capture_files([stranger, *parts], current, known=[current], sensor_parts=["file_190307_0001.wav"])
    assert [entry.name for entry in mine] == [entry.name for entry in parts]
    # Fallback (no stream): a file written a few seconds after the request is accepted.
    assert live_capture_files(parts, current, known=[current]) == parts


def test_live_watch_tolerates_a_sensor_clock_slightly_behind():
    current = _record(datetime(2026, 10, 31, 21, 25, 4))
    slightly_early = _wav("file_192434_0001.wav", datetime(2026, 10, 31, 21, 24, 34))  # -30 s
    much_earlier = _wav("file_192300_0001.wav", datetime(2026, 10, 31, 21, 23, 0))  # -124 s
    assert live_capture_files([slightly_early], current, known=[current]) == [slightly_early]
    assert live_capture_files([much_earlier], current, known=[current]) == []

def test_pinned_recording_is_not_reassigned_when_its_file_is_deleted():
    """Seen on an RFeye Node (2026-10-31): after deleting file_205734_0001.wav the
    listing showed its 410-412 MHz metadata on file_185751_0001.wav, a file
    recorded two hours earlier, because the +4 h guess landed 20 s from the start."""
    started = datetime(2026, 10, 31, 22, 57, 31)
    record = new_recording(
        host="192.0.2.12",
        start_hz=410_000_000,
        end_hz=412_000_000,
        center_hz=411_000_000,
        bandwidth_hz=2_000_000,
        duration_s=2,
        started_at=started,
        iq_stem="file_205734",
    )
    stranger = RemoteEntry(
        "file_185751_0001.wav",
        "/mnt/1/remdata/20261031/file_185751_0001.wav",
        False,
        91_900_000,
        datetime(2026, 10, 31, 20, 57, 59),
    )
    assert match_recordings_to_entries([stranger], [record], host="192.0.2.12") == {}
    # The pinned file itself still matches by stem.
    own = RemoteEntry(
        "file_205734_0001.wav",
        "/mnt/1/remdata/20261031/file_205734_0001.wav",
        False,
        33_522_232,
        datetime(2026, 10, 31, 22, 57, 40),
    )
    matched = match_recordings_to_entries([stranger, own], [record], host="192.0.2.12")
    assert matched == {own.path: record}


def test_hour_offset_guesses_must_agree_with_the_files_mtime():
    """An unpinned record must not adopt a file whose mtime is hours away."""
    started = datetime(2026, 10, 31, 22, 57, 31)
    record = new_recording(
        host="192.0.2.12",
        start_hz=410_000_000,
        end_hz=412_000_000,
        center_hz=411_000_000,
        bandwidth_hz=2_000_000,
        duration_s=2,
        started_at=started,
    )
    stranger = RemoteEntry(
        "file_185751_0001.wav",
        "/mnt/1/remdata/20261031/file_185751_0001.wav",
        False,
        91_900_000,
        datetime(2026, 10, 31, 20, 57, 59),
    )
    assert match_recordings_to_entries([stranger], [record], host="192.0.2.12") == {}
    # Without an mtime the offset guesses are all we have, and they still work.
    unknown = RemoteEntry(
        "file_205734_0001.wav",
        "/mnt/1/remdata/20261031/file_205734_0001.wav",
        False,
        33_522_232,
        None,
    )
    assert match_recordings_to_entries([unknown], [record], host="192.0.2.12") == {unknown.path: record}