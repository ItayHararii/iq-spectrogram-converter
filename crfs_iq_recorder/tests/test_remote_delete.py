"""Deleting one sensor folder: confinement, links, re-checks, partial failures and what counts as busy."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from crfs_iq_recorder.remote_delete import (
    REASON_COLLECTION,
    REASON_LIVE,
    REASON_READING,
    REASON_RECENT,
    REASON_RECORDING,
    REASON_UNCHECKED,
    DeleteRefused,
    SensorActivity,
    confined_folder,
    delete_planned,
    files_busy_reason,
    folder_busy_reason,
    folder_refusal,
    host_activity,
    plan_busy_reason,
    plan_folder_delete,
)
from crfs_iq_recorder.sftp_client import DemoSftpBrowser, SftpError

ROOT = "/mnt/1/remdata"
TEST = f"{ROOT}/crfs_delete_test_20261109_120000"
OUTSIDE = f"{ROOT}/crfs_delete_test_20261109_120000_outside"
OLD = datetime.now() - timedelta(hours=2)


def _tree(browser: DemoSftpBrowser) -> dict[str, str]:
    """Every path the demo sensor knows, with its kind and size (a listing to compare before and after)."""
    out: dict[str, str] = {}
    for key, entries in browser._dirs.items():
        out[key] = "dir"
        for entry in entries:
            if entry.path in browser._links:
                out[entry.path] = f"link->{browser._links[entry.path]}"
            elif not entry.is_dir:
                out[entry.path] = f"file {len(browser._files.get(entry.path, b''))}"
    return out


def _sensor(files: int = 3) -> DemoSftpBrowser:
    """The temp folder of the hardware check: small files, a nested folder, and links to a second temp folder."""
    browser = DemoSftpBrowser(include_today=False)
    browser.add_folder(TEST, modified=OLD)
    browser.add_folder(f"{TEST}/nested", modified=OLD)
    browser.add_folder(OUTSIDE, modified=OLD)
    for index in range(files):
        browser.add_file(f"part_{index:04d}.wav", TEST, payload=b"x" * (100 + index), modified=OLD)
    browser.add_file("inner.bin", f"{TEST}/nested", payload=b"y" * 50, modified=OLD)
    browser.add_file("canary.txt", OUTSIDE, payload=b"canary", modified=OLD)
    browser.add_link(f"{TEST}/escape", OUTSIDE, modified=OLD)
    browser.add_link(f"{TEST}/nested/up", "../../crfs_delete_test_20261109_120000_outside", modified=OLD)
    browser.add_link(f"{TEST}/canary_link", f"{OUTSIDE}/canary.txt", modified=OLD)
    return browser


# ---------------------------------------------------------------------------- confinement


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", "No folder selected"),
        ("   ", "No folder selected"),
        ("/mnt/1/remdata", "Recording root"),
        ("/mnt/1/remdata/", "Recording root"),
        ("//mnt//1//remdata//", "Recording root"),
        ("/mnt/1/remdata/..", "The path contains . or .."),
        ("/mnt/1/remdata/../../etc", "The path contains . or .."),
        ("/mnt/1/remdata/./20261109", "The path contains . or .."),
        ("/mnt/1/remdata/20261109/..", "The path contains . or .."),
        ("mnt/1/remdata/20261109", "Not a full sensor path"),
        ("/mnt/1/remdatax/20261109", "Outside recording storage"),
        ("/mnt/1", "Outside recording storage"),
        ("/", "Outside recording storage"),
        ("/etc", "Outside recording storage"),
        ("/mnt/1/remdata/a\nb", "Unusual characters in the path"),
    ],
)
def test_only_folders_strictly_inside_the_root_may_be_deleted(path, reason):
    with pytest.raises(DeleteRefused, match=f"^{reason.replace('.', '[.]')}$"):
        confined_folder(path)
    assert folder_refusal(path) == reason


def test_confined_folder_normalises_and_follows_the_configured_root():
    assert confined_folder("/mnt/1/remdata//20261109/") == "/mnt/1/remdata/20261109"
    assert confined_folder("/data/rec/x/y", "/data/rec/") == "/data/rec/x/y"
    assert folder_refusal("/mnt/1/remdata/20261109", "/data/rec/") == "Outside recording storage"
    assert folder_refusal("/mnt/1/remdata/x", "/") == "No recording root set"


def test_a_folder_that_is_a_link_is_refused_and_nothing_changes():
    browser = _sensor()
    browser.add_link(f"{ROOT}/link_to_outside_1", OUTSIDE)
    before = _tree(browser)
    with pytest.raises(DeleteRefused, match="This is a link, not a folder"):
        plan_folder_delete(browser, f"{ROOT}/link_to_outside_1")
    assert _tree(browser) == before


def test_a_folder_reached_through_a_linked_parent_is_refused():
    browser = _sensor()
    browser.add_folder("/srv/elsewhere/sub")
    browser.add_file("keep.wav", "/srv/elsewhere/sub", payload=b"k" * 10, modified=OLD)
    browser.add_link(f"{ROOT}/via", "/srv/elsewhere")
    before = _tree(browser)
    with pytest.raises(DeleteRefused, match="Reached through a link"):
        plan_folder_delete(browser, f"{ROOT}/via/sub")
    assert _tree(browser) == before


def test_a_root_that_is_itself_a_link_still_confines_by_real_path():
    browser = DemoSftpBrowser(include_today=False)
    del browser._dirs["/mnt/1/remdata/"]
    browser._dirs["/mnt/1/"] = []
    browser.add_folder("/data/remdata/20261109", modified=OLD)
    browser.add_file("a.wav", "/data/remdata/20261109", payload=b"a" * 10, modified=OLD)
    browser.add_link(ROOT, "/data/remdata")
    plan = plan_folder_delete(browser, f"{ROOT}/20261109")
    assert plan.real_root == "/data/remdata"
    assert [entry.path for entry in plan.entries] == [f"{ROOT}/20261109/a.wav"]
    result = delete_planned(browser, plan)
    assert result.complete
    assert "/data/remdata/20261109/" not in browser._dirs


def test_missing_folder_and_plain_file_are_refused():
    browser = _sensor()
    with pytest.raises(DeleteRefused, match="no longer there"):
        plan_folder_delete(browser, f"{ROOT}/gone_20261109")
    with pytest.raises(DeleteRefused, match="Not a folder"):
        plan_folder_delete(browser, f"{TEST}/part_0000.wav")


# ---------------------------------------------------------------------------- counting and deleting


def test_counting_uses_lstat_and_never_descends_links():
    browser = _sensor()
    progress = []
    plan = plan_folder_delete(browser, TEST, progress=progress.append)
    kinds = {entry.path.rsplit("/", 1)[-1]: entry.kind for entry in plan.entries}
    assert kinds == {
        "part_0000.wav": "file",
        "part_0001.wav": "file",
        "part_0002.wav": "file",
        "nested": "dir",
        "inner.bin": "file",
        "escape": "link",
        "up": "link",
        "canary_link": "link",
    }
    assert not any("canary.txt" in entry.path or OUTSIDE + "/" in entry.path for entry in plan.entries)
    assert (plan.files, plan.subfolders, plan.links, plan.total_bytes) == (4, 1, 3, 100 + 101 + 102 + 50)
    assert plan.contents_text() == "4 files (353 B), 1 subfolder, 3 links"
    assert progress and all(item.phase == "counting" for item in progress)


def test_delete_removes_links_as_links_and_leaves_their_targets_and_everything_else():
    browser = _sensor()
    before = _tree(browser)
    plan = plan_folder_delete(browser, TEST)
    progress = []
    result = delete_planned(browser, plan, progress=progress.append)
    assert result.complete, result
    assert (result.items_total, result.items_deleted, result.folders_deleted) == (7, 7, 2)
    after = _tree(browser)
    removed = {path for path in before if path not in after}
    assert all(path == TEST + "/" or path.startswith(TEST + "/") for path in removed)
    assert {path: kind for path, kind in before.items() if path not in removed} == after
    assert browser._files[f"{OUTSIDE}/canary.txt"] == b"canary"
    assert f"{OUTSIDE}/" in browser._dirs
    # progress: one step per item, the last one is n of n with every counted byte
    assert [item.done for item in progress] == list(range(1, len(plan.entries) + 2))
    assert progress[-1].total == len(plan.entries) + 1
    assert progress[-1].bytes_done == progress[-1].bytes_total == plan.total_bytes
    assert result.summary() == (
        "Deleted /mnt/1/remdata/crfs_delete_test_20261109_120000/ from the sensor: 7 items, 353 B."
    )


def test_one_file_reads_as_one_file():
    browser = _sensor()
    browser.add_folder(f"{ROOT}/single_20261109", modified=OLD)
    browser.add_file("only.wav", f"{ROOT}/single_20261109", payload=b"o" * 23, modified=OLD)
    result = delete_planned(browser, plan_folder_delete(browser, f"{ROOT}/single_20261109"))
    assert result.summary() == "Deleted /mnt/1/remdata/single_20261109/ from the sensor: 1 file, 23 B."


def test_an_empty_folder_is_deleted():
    browser = _sensor()
    browser.add_folder(f"{ROOT}/empty_20261109", modified=OLD)
    result = delete_planned(browser, plan_folder_delete(browser, f"{ROOT}/empty_20261109"))
    assert result.complete
    assert result.summary() == "Deleted the folder /mnt/1/remdata/empty_20261109/ from the sensor."


def test_partial_failure_is_reported_exactly_and_the_folder_is_kept():
    browser = DemoSftpBrowser(include_today=False)
    browser.add_folder(TEST, modified=OLD)
    for index in range(14):
        browser.add_file(f"iq_20261109_120000_{index:04d}.wav", TEST, payload=b"z" * 10, modified=OLD)
    browser.set_remove_failure(f"{TEST}/iq_20261109_120000_0003.wav", "Permission denied")
    browser.set_remove_failure(f"{TEST}/iq_20261109_120000_0009.wav", "Device or resource busy")
    result = delete_planned(browser, plan_folder_delete(browser, TEST))
    assert not result.complete and not result.folder_removed
    assert (result.items_deleted, result.items_total, result.bytes_deleted) == (12, 14, 120)
    assert result.summary() == (
        "Deleted 12 of 14 files. 2 could not be deleted: iq_20261109_120000_0003.wav (Permission denied), "
        "iq_20261109_120000_0009.wav (Device or resource busy). The folder was kept."
    )
    assert sorted(entry.name for entry in browser.listdir(TEST)) == [
        "iq_20261109_120000_0003.wav",
        "iq_20261109_120000_0009.wav",
    ]


def test_a_subfolder_that_cannot_go_is_named():
    browser = _sensor()
    browser.set_remove_failure(f"{TEST}/nested", "Permission denied")
    result = delete_planned(browser, plan_folder_delete(browser, TEST))
    assert result.items_deleted == result.items_total
    assert result.summary() == "Deleted 7 of 7 items. 1 subfolder could not be deleted: nested (Permission denied). The folder was kept."


def test_something_written_after_counting_stays_and_keeps_the_folder():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    added = []

    def new_file(_path):
        if not added:
            added.append(browser.add_file("late_0001.wav", TEST, payload=b"n" * 5))

    browser.set_before_remove(new_file)
    result = delete_planned(browser, plan)
    assert not result.folder_removed
    assert [entry.name for entry in browser.listdir(TEST)] == ["late_0001.wav"]
    assert result.items_deleted == result.items_total
    assert result.summary().endswith("The folder was kept: Directory not empty.")


def test_a_file_that_changed_after_counting_is_left():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    browser.add_file("part_0001.wav", TEST, payload=b"grown" * 100, modified=OLD)
    result = delete_planned(browser, plan)
    assert (f"{TEST}/part_0001.wav", "changed since it was counted") in result.failed
    assert f"{TEST}/part_0001.wav" in browser._files


def test_a_subfolder_swapped_for_a_link_after_counting_is_never_followed():
    """The nastiest race: nested/ becomes a link to the outside folder that holds a same-named file."""
    browser = _sensor()
    browser.add_file("inner.bin", OUTSIDE, payload=b"y" * 50, modified=OLD)  # same name, size and time
    plan = plan_folder_delete(browser, TEST)
    swapped = []

    def swap(_path):
        if swapped:
            return
        swapped.append(True)
        for key in [key for key in browser._dirs if key.startswith(f"{TEST}/nested/")]:
            del browser._dirs[key]
        browser._files.pop(f"{TEST}/nested/inner.bin", None)
        browser._links.pop(f"{TEST}/nested/up", None)
        browser._drop_entry_locked(f"{TEST}/nested")
        browser.add_link(f"{TEST}/nested", OUTSIDE)

    browser.set_before_remove(swap)
    result = delete_planned(browser, plan)
    assert browser._files[f"{OUTSIDE}/inner.bin"] == b"y" * 50
    assert browser._files[f"{OUTSIDE}/canary.txt"] == b"canary"
    reasons = dict(result.failed)
    assert reasons[f"{TEST}/nested/inner.bin"] == "its folder is now reached through a link"
    assert reasons[f"{TEST}/nested"] == "changed since it was counted"
    assert not result.folder_removed


def test_items_someone_else_removed_count_as_already_gone():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    browser.remove_file(f"{TEST}/part_0002.wav")
    result = delete_planned(browser, plan)
    assert result.complete
    assert (result.items_deleted, result.already_gone) == (6, 1)
    assert "(1 already gone)" in result.summary()


def test_cancel_stops_between_items_and_keeps_the_folder():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    cancel = threading.Event()

    def progress(item):
        if item.done == 2:
            cancel.set()

    result = delete_planned(browser, plan, cancel=cancel, progress=progress)
    assert result.cancelled and not result.folder_removed
    assert result.items_deleted == 2
    assert result.summary() == "Stopped. Deleted 2 of 7 items. The folder was kept."


class _Dropping:
    """Wraps a demo sensor whose connection drops after the first removal."""

    def __init__(self, inner: DemoSftpBrowser) -> None:
        self._inner = inner
        self._removed = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def is_alive(self) -> bool:
        return self._removed < 1

    def remove_file(self, path: str) -> None:
        if self._removed >= 1:
            raise SftpError("Socket is closed")
        self._removed += 1
        self._inner.remove_file(path)

    def realpath(self, path: str) -> str:
        if self._removed >= 1:
            raise SftpError("Socket is closed")
        return self._inner.realpath(path)


def test_a_lost_connection_stops_the_deletion_with_one_clear_line():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    result = delete_planned(_Dropping(browser), plan)
    assert result.connection_lost
    assert result.items_deleted == 1
    assert len(result.failed) == 1
    assert result.summary() == "The connection to the sensor was lost. Deleted 1 of 7 items. The folder was kept."


def test_limits_on_size_and_depth():
    browser = _sensor()
    with pytest.raises(DeleteRefused, match="More than 3 items"):
        plan_folder_delete(browser, TEST, max_entries=3)
    deep = TEST + "".join(f"/d{index}" for index in range(5))
    browser.add_folder(deep, modified=OLD)
    with pytest.raises(DeleteRefused, match="nested too deeply"):
        plan_folder_delete(browser, TEST, max_depth=3)


def test_cancel_while_counting():
    browser = _sensor()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DeleteRefused, match="Cancelled"):
        plan_folder_delete(browser, TEST, cancel=cancel)


# ---------------------------------------------------------------------------- busy: recordings and transfers


def test_recent_writes_use_the_sensor_clock():
    browser = _sensor()
    browser.add_file("fresh_0001.wav", TEST, payload=b"f", modified=datetime.now())
    browser.set_clock_offset(0)
    assert plan_busy_reason(plan_folder_delete(browser, TEST), SensorActivity()) == REASON_RECENT
    # the sensor's clock is 3 h ahead: "now" on this PC is long ago there
    browser.set_clock_offset(3 * 3600)
    assert plan_busy_reason(plan_folder_delete(browser, TEST), SensorActivity()) == ""
    # the sensor's clock is 3 h behind: a file stamped in its future still counts as being written
    browser.set_clock_offset(-3 * 3600)
    assert plan_busy_reason(plan_folder_delete(browser, TEST), SensorActivity()) == REASON_RECENT


def test_old_folder_without_activity_may_go():
    browser = _sensor()
    assert plan_busy_reason(plan_folder_delete(browser, TEST), SensorActivity()) == ""


def test_recording_collection_live_part_and_transfers_refuse():
    browser = _sensor()
    plan = plan_folder_delete(browser, TEST)
    assert plan_busy_reason(plan, SensorActivity(recording=REASON_RECORDING)) == REASON_RECORDING
    assert plan_busy_reason(plan, SensorActivity(recording=REASON_COLLECTION)) == REASON_COLLECTION
    assert plan_busy_reason(plan, SensorActivity(live_stems=frozenset({"PART"}))) == REASON_LIVE
    assert plan_busy_reason(plan, SensorActivity(live_stems=frozenset({f"{TEST}/part_0002.wav"}))) == REASON_LIVE
    assert plan_busy_reason(plan, SensorActivity(reading=frozenset({f"{TEST}/nested/inner.bin"}))) == REASON_READING
    assert plan_busy_reason(plan, SensorActivity(reading=frozenset({f"{OUTSIDE}/canary.txt"}))) == ""
    assert folder_busy_reason(TEST, SensorActivity(reading=frozenset({f"{TEST}x/a.wav"}))) == ""
    assert files_busy_reason([f"{TEST}/part_0000.wav"], SensorActivity(live_stems=frozenset({"part"}))) == REASON_LIVE
    assert files_busy_reason([f"{TEST}/part_0000.wav"], SensorActivity(reading=frozenset({f"{TEST}/part_0000.wav"}))) == REASON_READING
    assert files_busy_reason([f"{TEST}/part_0000.wav"], SensorActivity(recording=REASON_RECORDING)) == ""


class _Runner:
    def __init__(self, state="idle", pending=()):
        self.state = state
        self._downloads_pending = list(pending)


class _Panel:
    def __init__(self, runner):
        self.runner = runner


class _Job:
    def __init__(self, remote, busy=True):
        self._remote = list(remote)
        self._busy = busy

    def busy(self):
        return self._busy


class _Host:
    def __init__(self, *, capture="", runner=None, stem=None, job=None):
        self._capture = capture
        self.cellular_panel = _Panel(runner or _Runner())
        self._stem = stem
        self._manual_download = job

    def capture_busy_reason(self):
        return self._capture

    def _live_recording_stem(self):
        return self._stem


def test_host_activity_reads_the_main_window_and_the_collection():
    assert host_activity(None) == SensorActivity()
    assert host_activity(_Host()) == SensorActivity()
    assert host_activity(_Host(capture="Recording is running")).recording == REASON_RECORDING
    assert host_activity(_Host(runner=_Runner("running"))).recording == REASON_COLLECTION
    assert host_activity(_Host(runner=_Runner("paused"))).recording == ""
    assert host_activity(_Host(stem="IQ_20261109_1200")).live_stems == frozenset({"iq_20261109_1200"})
    pending = [("task", 1, {"remote_path": f"{TEST}/part_0000.wav"})]
    assert f"{TEST}/part_0000.wav" in host_activity(_Host(runner=_Runner(pending=pending))).reading
    assert f"{TEST}/a.wav" in host_activity(_Host(job=_Job([f"{TEST}/a.wav"]))).reading
    assert host_activity(_Host(job=_Job([f"{TEST}/a.wav"], busy=False))).reading == frozenset()
    assert host_activity(None, also_reading=[f"{TEST}/b.wav"]).reading == frozenset({f"{TEST}/b.wav"})


def test_a_main_window_that_cannot_be_asked_counts_as_recording():
    class Broken(_Host):
        def capture_busy_reason(self):
            raise RuntimeError("deleted")

    assert host_activity(Broken()).recording == REASON_UNCHECKED


def test_host_activity_sees_running_transfer_workers(monkeypatch):
    class Worker:
        def __init__(self, path):
            self._path = path

        def remote_paths(self):
            return [self._path]

    reading = Worker(f"{TEST}/part_0001.wav")
    skipped = Worker(f"{TEST}/part_0002.wav")
    monkeypatch.setattr("crfs_iq_recorder.worker_threads.tracked_workers", lambda: [reading, skipped, object()])
    activity = host_activity(None, skip=[skipped])
    assert activity.reading == frozenset({f"{TEST}/part_0001.wav"})
