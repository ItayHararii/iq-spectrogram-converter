from datetime import datetime

from crfs_iq_recorder.constants import SFTP_REMDATA_ROOT
from crfs_iq_recorder.sftp_paths import parent_directory, resolve_start_directory, today_remdata_directory


def test_today_folder_uses_local_date_not_a_hardcoded_example():
    when = datetime(2026, 9, 10, 17, 47, 0)
    assert today_remdata_directory(when) == "/mnt/1/remdata/20260910/"
    live = today_remdata_directory()
    assert live == f"/mnt/1/remdata/{datetime.now():%Y%m%d}/"


def test_today_folder_changes_with_the_given_date():
    assert today_remdata_directory(datetime(2026, 1, 2)) == "/mnt/1/remdata/20260102/"


def test_missing_today_folder_falls_back_to_remdata_root():
    today = "/mnt/1/remdata/20260910/"
    path, message = resolve_start_directory(today, exists=False)
    assert path == SFTP_REMDATA_ROOT
    assert today in (message or "")
    assert SFTP_REMDATA_ROOT in (message or "")
    assert "after a recording" in (message or "")


def test_existing_today_folder_is_kept():
    today = "/mnt/1/remdata/20260910/"
    path, message = resolve_start_directory(today, exists=True)
    assert path == today
    assert message is None


def test_parent_of_dated_folder():
    assert parent_directory("/mnt/1/remdata/20260910/") == "/mnt/1/remdata/"


def test_parent_walks_to_filesystem_root():
    assert parent_directory("/mnt/1/remdata/") == "/mnt/1/"
    assert parent_directory("/mnt/1/") == "/mnt/"
    assert parent_directory("/mnt/") == "/"
    assert parent_directory("/") == "/"


def test_remdata_root_detection():
    from crfs_iq_recorder.sftp_paths import is_remdata_root

    assert is_remdata_root("/mnt/1/remdata/")
    assert is_remdata_root("/mnt/1/remdata")
    assert not is_remdata_root("/mnt/1/remdata/20260910/")


def test_candidate_today_includes_utc_when_dates_differ():
    from datetime import datetime, timedelta, timezone

    from crfs_iq_recorder.sftp_paths import candidate_today_directories, resolve_today_directory

    local = datetime(2026, 9, 10, 1, 30, 0)
    paths = candidate_today_directories(local)
    assert paths[0] == "/mnt/1/remdata/20260910/"

    offset = timezone(timedelta(hours=3))
    near_midnight = datetime(2026, 9, 10, 1, 30, tzinfo=offset)
    both = candidate_today_directories(near_midnight)
    assert "/mnt/1/remdata/20260910/" in both
    assert "/mnt/1/remdata/20260909/" in both

    path, message = resolve_today_directory(
        lambda p: False,
        now=datetime(2026, 9, 10, 2, 0),
    )
    assert path == "/mnt/1/remdata/"
    assert message
    assert "does not exist yet" in message

