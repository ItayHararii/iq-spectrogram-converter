"""Crash-safe JSON state: atomic writes, backup fallback, restart recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crfs_iq_recorder import json_store
from crfs_iq_recorder.json_store import backup_path, read_json, write_json


def test_write_then_read_round_trip_and_backup(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"a": 1, "rows": [1, 2, 3]})
    assert read_json(path) == {"a": 1, "rows": [1, 2, 3]}
    assert json.loads(backup_path(path).read_text(encoding="utf-8")) == {"a": 1, "rows": [1, 2, 3]}
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_missing_file_reads_as_none(tmp_path):
    assert read_json(tmp_path / "nothing.json") is None


def test_truncated_live_file_recovers_from_backup(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"generation": 1, "names": ["a", "b"]})
    write_json(path, {"generation": 2, "names": ["a", "b", "c"]})
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")  # what a kill mid-write leaves behind
    assert read_json(path) == {"generation": 2, "names": ["a", "b", "c"]}
    assert read_json(path, backup=False) is None


def test_empty_live_file_recovers_from_backup(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"ok": True})
    path.write_text("", encoding="utf-8")
    assert read_json(path) == {"ok": True}


def test_crash_while_writing_temp_file_keeps_old_document(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_json(path, {"version": "old"})
    original = Path.write_text

    def failing_write(self, text, *args, **kwargs):
        if self.suffix == ".tmp":
            original(self, text[: len(text) // 2], *args, **kwargs)
            raise OSError("simulated power loss")
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises(OSError):
        write_json(path, {"version": "new"})
    monkeypatch.undo()
    assert read_json(path, backup=False) == {"version": "old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_replace_blocked_by_open_handle_retries_then_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_json(path, {"version": "old"})
    attempts = []
    real_replace = os.replace

    def flaky_replace(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "replace", flaky_replace)
    monkeypatch.setattr(json_store, "_REPLACE_RETRY_S", 0.0)
    write_json(path, {"version": "new"})
    assert len(attempts) == 3
    assert read_json(path, backup=False) == {"version": "new"}
    assert not list(tmp_path.glob("*.tmp"))

    attempts.clear()
    monkeypatch.setattr(json_store.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    write_json(path, {"version": "newer"})
    assert read_json(path, backup=False) == {"version": "newer"}
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_disabled_leaves_only_the_target(tmp_path):
    path = tmp_path / "preset.json"
    write_json(path, {"preset_version": 1}, backup=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["preset.json"]


def test_recordings_survive_truncated_file_on_restart(tmp_path):
    from datetime import datetime

    from crfs_iq_recorder.recording_history import add_recording, load_recordings, new_recording

    file = tmp_path / "recordings.json"
    for n in range(3):
        add_recording(
            new_recording(
                host="192.0.2.10",
                start_hz=790_000_000 + n,
                end_hz=810_000_000,
                center_hz=800_000_000,
                bandwidth_hz=20_000_000,
                duration_s=4,
                started_at=datetime.now(),
                demo=True,
            ),
            file,
        )
    assert len(load_recordings(file)) == 3
    text = file.read_text(encoding="utf-8")
    file.write_text(text[:-40], encoding="utf-8")
    assert len(load_recordings(file)) == 3
    # The next save repairs the live file.
    add_recording(
        new_recording(
            host="192.0.2.10",
            start_hz=1_000_000_000,
            end_hz=1_020_000_000,
            center_hz=1_010_000_000,
            bandwidth_hz=20_000_000,
            duration_s=4,
            started_at=datetime.now(),
            demo=True,
        ),
        file,
    )
    json.loads(file.read_text(encoding="utf-8"))
    assert len(load_recordings(file)) == 4


def test_excel_log_dedupe_set_survives_corrupt_file(tmp_path):
    from crfs_iq_recorder.excel_log import ExcelLogStore

    path = tmp_path / "excel_log.json"
    store = ExcelLogStore(path)
    first = "/mnt/1/remdata/20260910/iq_demo_20260910_120000_abcd1234_0001.wav"
    second = "/mnt/1/remdata/20260910/iq_demo_20260910_120100_abcd1235_0001.wav"
    store.mark_logged("demo", first)
    store.mark_logged("demo", second)
    store.save()
    path.write_text("{\"logged\": [\"demo|/mnt/1/remdata/2026", encoding="utf-8")
    reloaded = ExcelLogStore(path)
    assert reloaded.is_logged("demo", first)
    assert reloaded.is_logged("demo", second)


def test_settings_survive_corrupt_file(tmp_path, monkeypatch):
    from crfs_iq_recorder import paths

    path = tmp_path / "settings.json"
    monkeypatch.setattr(paths, "settings_path", lambda: path)
    paths.save_settings({"host": "192.0.2.10", "download_dir": "D:\\somewhere", "password": "secret"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "password" not in saved
    path.write_text("not json at all", encoding="utf-8")
    loaded = paths.load_settings()
    assert loaded["host"] == "192.0.2.10"
    assert loaded["download_dir"] == "D:\\somewhere"
    assert "password" not in loaded
