from pathlib import Path

import pytest

from crfs_iq_recorder.download_util import (
    DownloadError,
    cleanup_part,
    finalize_download,
    part_path,
    unique_destination,
)


def test_part_path_appends_part_suffix():
    dest = Path("C:/tmp/iq_demo.wav")
    assert part_path(dest).name == "iq_demo.wav.part"


def test_unique_destination_does_not_overwrite(tmp_path):
    existing = tmp_path / "iq.wav"
    existing.write_bytes(b"old")
    other = unique_destination(existing)
    assert other != existing
    assert other.name == "iq (2).wav"
    other.write_bytes(b"new")
    third = unique_destination(existing)
    assert third.name == "iq (3).wav"
    assert existing.read_bytes() == b"old"


def test_finalize_rejects_incomplete_and_removes_part(tmp_path):
    dest = tmp_path / "iq.wav"
    part = part_path(dest)
    part.write_bytes(b"abc")
    with pytest.raises(DownloadError, match="Incomplete"):
        finalize_download(part, dest, expected_size=10)
    assert not part.exists()
    assert not dest.exists()


def test_finalize_allows_file_larger_than_listed_size(tmp_path):
    dest = tmp_path / "iq.wav"
    part = part_path(dest)
    part.write_bytes(b"abcdefghij")
    finalize_download(part, dest, expected_size=6)
    assert dest.read_bytes() == b"abcdefghij"


def test_finalize_moves_complete_file(tmp_path):
    dest = tmp_path / "iq.wav"
    part = part_path(dest)
    part.write_bytes(b"abcdef")
    finalize_download(part, dest, expected_size=6)
    assert dest.read_bytes() == b"abcdef"
    assert not part.exists()


def test_cleanup_part_ignores_missing(tmp_path):
    cleanup_part(tmp_path / "missing.part")
