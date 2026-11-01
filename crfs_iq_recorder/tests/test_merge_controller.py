"""MergeController / MergeRegistry without the main window."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from test_iq_merge import make_recording


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _spin(qapp, predicate, timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _controller(tmp_path, qapp):
    from crfs_iq_recorder.merge_controller import MergeController, MergeRegistry

    registry = MergeRegistry(tmp_path / "merge_history.json")
    controller = MergeController(registry=registry)
    events: dict[str, list] = {"log": [], "status": [], "merged": [], "failed": [], "busy": []}
    controller.log.connect(lambda m, lvl: events["log"].append((lvl, m)))
    controller.status_changed.connect(lambda t, err: events["status"].append((err, t)))
    controller.merged.connect(lambda s, r: events["merged"].append((s, r)))
    controller.merge_failed.connect(lambda s, r: events["failed"].append((s, r)))
    controller.busy_changed.connect(lambda b: events["busy"].append(b))
    return controller, events


def test_auto_requests_need_the_toggle_and_a_finished_recording(qapp, tmp_path):
    controller, events = _controller(tmp_path, qapp)
    paths, _ = make_recording(tmp_path)
    assert controller.request("k", "file_063929", paths, tmp_path, auto=True) == "merging is off"
    controller.set_enabled(True)
    controller.live_stem = lambda: "file_063929"
    assert controller.request("k", "file_063929", paths, tmp_path, auto=True) == "the recording is still in progress"
    controller.live_stem = lambda: None
    assert controller.request("k", "file_063929", paths, tmp_path, auto=True) is None
    assert _spin(qapp, lambda: len(events["merged"]) == 1)
    stem, result = events["merged"][0]
    assert stem == "file_063929" and result.frames == 600
    assert (tmp_path / "file_063929_merged.wav").exists()
    assert events["busy"][0] is True and events["busy"][-1] is False
    assert any(t.startswith("Merged file_063929 -> file_063929_merged.wav") for _e, t in events["status"])


def test_manual_requests_ignore_the_toggle_but_not_the_queue(qapp, tmp_path):
    controller, events = _controller(tmp_path, qapp)
    paths, _ = make_recording(tmp_path)
    assert controller.enabled is False
    assert controller.request("k", "file_063929", paths, tmp_path, auto=False) is None
    assert controller.request("k", "file_063929", paths, tmp_path, auto=False) in ("already merging", "already queued")
    assert _spin(qapp, lambda: len(events["merged"]) == 1 and not controller.is_busy())


def test_registry_survives_a_restart_and_notices_changed_parts(qapp, tmp_path):
    from crfs_iq_recorder.merge_controller import MergeRegistry

    controller, events = _controller(tmp_path, qapp)
    controller.set_enabled(True)
    paths, _ = make_recording(tmp_path)
    assert controller.request("host|/mnt/1/remdata/20261101|file_063929", "file_063929", paths, tmp_path, auto=True) is None
    assert _spin(qapp, lambda: len(events["merged"]) == 1 and not controller.is_busy())
    output = events["merged"][0][1].output

    # "Restart": a new controller over the same history file.
    fresh = MergeRegistry(tmp_path / "merge_history.json")
    assert fresh.find("host|/mnt/1/remdata/20261101|file_063929", paths) == output
    # Registry wiped (or another PC): the sidecar next to the parts still answers.
    empty = MergeRegistry(tmp_path / "other_history.json")
    assert empty.find("whatever", paths) == output
    # Different part set (one part re-recorded with another size): not the same merge.
    paths[1].write_bytes(paths[1].read_bytes() + b"\x00\x00\x00\x00")
    assert fresh.find("host|/mnt/1/remdata/20261101|file_063929", paths) is None
    # Output deleted: nothing to point at.
    paths[1].write_bytes(paths[1].read_bytes()[:-4])
    output.unlink()
    assert fresh.find("host|/mnt/1/remdata/20261101|file_063929", paths) is None


def test_turning_the_toggle_off_drops_queued_auto_merges_but_keeps_manual_ones(qapp, tmp_path, monkeypatch):
    import threading

    from crfs_iq_recorder import merge_controller as mc

    controller, events = _controller(tmp_path, qapp)
    controller.set_enabled(True)
    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    a, _ = make_recording(tmp_path / "a", stem="file_000001")
    b, _ = make_recording(tmp_path / "b", stem="file_000002")
    c, _ = make_recording(tmp_path / "c", stem="file_000003")
    gate = threading.Event()
    real = mc.merge_parts

    def slow(plan, output, **kw):
        gate.wait(10)
        return real(plan, output, **kw)

    monkeypatch.setattr(mc, "merge_parts", slow)
    assert controller.request("a", "file_000001", a, tmp_path / "a", auto=True) is None  # running
    assert controller.request("b", "file_000002", b, tmp_path / "b", auto=True) is None  # queued auto
    assert controller.request("c", "file_000003", c, tmp_path / "c", auto=False) is None  # queued manual
    controller.set_enabled(False)
    assert any("Dropped 1 queued automatic merge(s)." in m for _l, m in events["log"])
    gate.set()
    assert _spin(qapp, lambda: not controller.is_busy(), 15.0)
    done = sorted(s for s, _r in events["merged"])
    assert done == ["file_000001", "file_000003"]
    assert not (tmp_path / "b" / "file_000002_merged.wav").exists()
    # The dropped group can be queued again later.
    controller.set_enabled(True)
    assert controller.request("b", "file_000002", b, tmp_path / "b", auto=True) is None
    assert _spin(qapp, lambda: len(events["merged"]) == 3, 15.0)


def test_progress_is_right_for_outputs_larger_than_2_gib(qapp, tmp_path, monkeypatch):
    """Hardware regression: an 8-part 3.75 GB merge showed 0 % throughout because the
    byte counts were marshalled through a 32-bit int signal, so Cancel could not be
    aimed at a percentage either."""
    from crfs_iq_recorder import merge_controller as mc

    controller, events = _controller(tmp_path, qapp)
    paths, _ = make_recording(tmp_path)
    real = mc.merge_parts
    percents: list[int] = []
    controller.progress_changed.connect(lambda p: percents.append(p))

    def big_progress(plan, output, *, progress=None, cancel=None, **kw):
        total = 3_750_236_914
        for done in (0, 1_000_000_000, 2_500_000_000, total):
            progress(done, total)
            time.sleep(0.25)
        return real(plan, output, progress=None, cancel=cancel, **kw)

    monkeypatch.setattr(mc, "merge_parts", big_progress)
    assert controller.request("k", "file_063929", paths, tmp_path, auto=False) is None
    assert _spin(qapp, lambda: len(events["merged"]) == 1 and not controller.is_busy(), 15.0)
    assert [p for p in percents if p >= 0] == [0, 0, 26, 66, 100]
    assert any(t.endswith("… 66 %") for _e, t in events["status"])


def test_planning_failures_are_reported_without_a_thread(qapp, tmp_path):
    controller, events = _controller(tmp_path, qapp)
    paths, _ = make_recording(tmp_path)
    paths[1].unlink()  # part 2 missing
    assert controller.request("k", "file_063929", [paths[0], paths[2]], tmp_path, auto=False) is None
    assert events["failed"] and "Missing part(s) 0002" in events["failed"][0][1]
    assert not controller.is_busy()
    assert events["status"][-1][0] is True  # shown as an error
    assert not list(tmp_path.glob("*_merged*"))
