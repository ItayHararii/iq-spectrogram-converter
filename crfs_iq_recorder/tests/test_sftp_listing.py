from datetime import datetime, timedelta

from crfs_iq_recorder.sftp_client import DemoSftpBrowser, RemoteEntry, sort_remote_entries
from crfs_iq_recorder.sftp_window import discovered_new_paths


def test_files_and_folders_sort_newest_first():
    now = datetime(2026, 9, 13, 12, 0, 0)
    entries = [
        RemoteEntry("old.wav", "/a/old.wav", False, 1, now - timedelta(hours=2)),
        RemoteEntry("new.wav", "/a/new.wav", False, 1, now),
        RemoteEntry("mid.wav", "/a/mid.wav", False, 1, now - timedelta(minutes=5)),
        RemoteEntry("20260115", "/a/20260115", True, 0, now),
        RemoteEntry("20260913", "/a/20260913", True, 0, now - timedelta(days=30)),
    ]
    ordered = sort_remote_entries(entries)
    names = [item.name for item in ordered]
    assert names == ["new.wav", "mid.wav", "old.wav", "20260913", "20260115"]


def test_new_badges_only_after_baseline_listing():
    first_new, first_seen = discovered_new_paths(None, {"/a/one.wav", "/a/two.wav"})
    assert first_new == set()
    assert first_seen == {"/a/one.wav", "/a/two.wav"}
    later_new, later_seen = discovered_new_paths(first_seen, {"/a/one.wav", "/a/two.wav", "/a/three.wav"})
    assert later_new == {"/a/three.wav"}
    assert "/a/three.wav" in later_seen


def test_demo_browser_prefix_is_limited(tmp_path):
    browser = DemoSftpBrowser()
    dest = tmp_path / "prefix.wav"
    today = [path for path in browser._files][0]
    n = browser.read_prefix(today, dest, 64)
    assert n <= 64
    assert dest.stat().st_size == n
