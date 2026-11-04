"""Local destinations mirror the sensor's dated folders; nothing escapes the download root."""

from __future__ import annotations

from pathlib import Path

import pytest

from crfs_iq_recorder.download_paths import (
    LocalCopies,
    candidate_paths,
    existing_copy,
    local_destination,
    local_folder,
    plan_destinations,
    remote_subfolder,
    sensor_folder_name,
)

REMOTE = "/mnt/1/remdata/20261104/iq_20261104_232001_0001.wav"
REMOTE2 = "/mnt/1/remdata/20261104/iq_20261104_232001_0002.wav"


def test_the_dated_folder_comes_from_the_remote_path_not_the_computer_date(tmp_path):
    assert remote_subfolder(REMOTE) == "20261104"
    assert local_destination(tmp_path, REMOTE) == tmp_path / "20261104" / "iq_20261104_232001_0001.wav"
    # Just after midnight the sensor still writes yesterday's folder when the file started yesterday.
    assert local_destination(tmp_path, "/mnt/1/remdata/20261103/iq_20261103_235959_0001.wav").parent.name == "20261103"


def test_meaningful_sub_folders_are_kept_and_the_root_is_used_for_root_files(tmp_path):
    assert remote_subfolder("/mnt/1/remdata/20261104/site-a/iq_x.wav") == "20261104/site-a"
    assert local_destination(tmp_path, "/mnt/1/remdata/20261104/site-a/iq_x.wav") == tmp_path / "20261104" / "site-a" / "iq_x.wav"
    assert remote_subfolder("/mnt/1/remdata/iq_x.wav") == ""
    assert local_destination(tmp_path, "/mnt/1/remdata/iq_x.wav") == tmp_path / "iq_x.wav"
    # A path outside the sensor's recording root still mirrors its own last folder.
    assert local_destination(tmp_path, "/mnt/2/other/20261104/iq_x.wav").parent == tmp_path / "20261104"


@pytest.mark.parametrize(
    "remote",
    [
        "/mnt/1/remdata/../../etc/iq_x.wav",
        "/mnt/1/remdata/20261104/../iq_x.wav",
        "/mnt/1/remdata/C:/iq_x.wav",
        "/mnt/1/remdata/..\\iq_x.wav",
        "/mnt/1/remdata/con/iq_x.wav",
        "/mnt/1/remdata/a<b/iq_x.wav",
    ],
)
def test_paths_that_would_leave_the_root_fall_back_to_the_root(tmp_path, remote):
    dest = local_destination(tmp_path, remote)
    assert dest.parent == tmp_path and dest.name == "iq_x.wav"
    assert local_folder(tmp_path, remote) == tmp_path
    assert tmp_path in dest.resolve().parents


def test_existing_copies_are_recognised_where_they_are(tmp_path):
    flat = tmp_path / "iq_20261104_232001_0001.wav"
    flat.write_bytes(b"x" * 10)
    assert existing_copy(tmp_path, REMOTE, 10) == flat, "an older flat download is used, not moved"
    assert existing_copy(tmp_path, REMOTE, 11) is None, "a different size is not the same file"
    dated = tmp_path / "20261104" / "iq_20261104_232001_0001.wav"
    dated.parent.mkdir()
    dated.write_bytes(b"x" * 10)
    assert existing_copy(tmp_path, REMOTE, 10) == dated, "the mirrored folder is looked at first"
    assert candidate_paths(tmp_path, REMOTE)[0] == dated and candidate_paths(tmp_path, REMOTE)[-1] == flat


def test_a_listing_snapshot_gives_the_same_answers_with_one_read_per_folder(tmp_path, monkeypatch):
    import os

    import crfs_iq_recorder.download_paths as module

    flat = tmp_path / "iq_20261104_232001_0001.wav"
    flat.write_bytes(b"x" * 10)
    dated = tmp_path / "20261104" / "iq_20261104_232001_0002.wav"
    dated.parent.mkdir()
    dated.write_bytes(b"y" * 20)
    (tmp_path / "20261104" / "rfeye300540").mkdir()
    (tmp_path / "20261104" / "rfeye300540" / "iq_20261104_232001_0003.wav").write_bytes(b"z" * 30)
    scans: list[str] = []
    real_scandir = os.scandir

    def counting(path):
        scans.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(module.os, "scandir", counting)
    copies = LocalCopies(tmp_path, sensor="rfeye300540")
    remote3 = "/mnt/1/remdata/20261104/iq_20261104_232001_0003.wav"
    for remote, size in [(REMOTE, 10), (REMOTE2, 20), (remote3, 30), (REMOTE, 11), ("/mnt/1/remdata/20261104/missing.wav", 5)]:
        assert copies.find(remote, size) == existing_copy(tmp_path, remote, size, sensor="rfeye300540")
    assert copies.find(REMOTE, 10) == flat and copies.find(REMOTE2, 20) == dated
    assert copies.find(remote3, 30) == tmp_path / "20261104" / "rfeye300540" / "iq_20261104_232001_0003.wav"
    assert copies.find(REMOTE, 0) is None
    assert len(scans) == 3, scans  # root, dated folder and sensor folder: each read once
    # A folder that does not exist is simply empty, not an error.
    assert LocalCopies(tmp_path / "nowhere").find(REMOTE, 10) is None


def test_parts_of_one_recording_share_a_folder(tmp_path):
    plan = plan_destinations(tmp_path, [REMOTE, REMOTE2], lambda p: 10, sensor="rfeye300540")
    assert {p.parent for p in plan.values()} == {tmp_path / "20261104"}
    assert [p.name for p in plan.values()] == ["iq_20261104_232001_0001.wav", "iq_20261104_232001_0002.wav"]


def test_a_different_file_of_the_same_name_is_never_overwritten(tmp_path):
    other = tmp_path / "20261104" / "iq_20261104_232001_0001.wav"
    other.parent.mkdir()
    other.write_bytes(b"z" * 99)  # another sensor's file with the same name
    plan = plan_destinations(tmp_path, [REMOTE, REMOTE2], lambda p: 10, sensor="rfeye300540")
    assert {p.parent for p in plan.values()} == {tmp_path / "20261104" / "rfeye300540"}, "the whole group moves to the sensor folder"
    assert other.read_bytes() == b"z" * 99
    # The sensor folder taken too: a numbered name, still inside the root.
    (tmp_path / "20261104" / "rfeye300540").mkdir()
    (tmp_path / "20261104" / "rfeye300540" / "iq_20261104_232001_0001.wav").write_bytes(b"q" * 5)
    plan = plan_destinations(tmp_path, [REMOTE], lambda p: 10, sensor="rfeye300540")
    dest = plan[REMOTE]
    assert dest.parent == tmp_path / "20261104" / "rfeye300540" and dest.name != "iq_20261104_232001_0001.wav"
    assert tmp_path in dest.resolve().parents


def test_a_same_size_copy_is_returned_as_the_destination(tmp_path):
    dated = tmp_path / "20261104" / "iq_20261104_232001_0001.wav"
    dated.parent.mkdir()
    dated.write_bytes(b"x" * 10)
    plan = plan_destinations(tmp_path, [REMOTE], lambda p: 10, sensor="rfeye300540")
    assert plan[REMOTE] == dated


def test_sensor_folder_names_are_safe():
    assert sensor_folder_name("rfeye300540") == "rfeye300540"
    assert sensor_folder_name("10.1.0.11") == "10.1.0.11"
    assert "/" not in sensor_folder_name("a/b:c") and ":" not in sensor_folder_name("a/b:c")
    assert sensor_folder_name("") == "", "no identity: no sensor folder (the dated folder alone)"
    assert sensor_folder_name("..") == "" and sensor_folder_name("con") == ""
    assert isinstance(Path(sensor_folder_name("x")), Path)
