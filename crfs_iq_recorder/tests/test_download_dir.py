from pathlib import Path
import os

from crfs_iq_recorder.connection_state import ConnectionState
from crfs_iq_recorder.constants import DOWNLOAD_APP_FOLDER, DOWNLOAD_SUBFOLDER
from crfs_iq_recorder.paths import (
    default_download_dir,
    ensure_download_dir,
    is_cloud_path,
    load_settings,
    resolved_download_dir,
    save_settings,
    user_documents_dir,
)


def test_user_documents_dir_is_writable():
    docs = user_documents_dir()
    assert docs.is_dir()
    probe = docs / f".crfs_test_probe_{os.getpid()}"
    probe.mkdir()
    probe.rmdir()


def test_ensure_falls_back_when_default_folder_is_unusable(tmp_path, monkeypatch):
    from crfs_iq_recorder import paths

    blocked = tmp_path / "not_a_dir"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(paths, "default_download_dir", lambda: blocked / "Recordings")
    monkeypatch.setattr(paths, "_local_app_folder", lambda: tmp_path / "fallback_app")
    created = ensure_download_dir("")
    assert created.is_dir()
    assert created == (tmp_path / "fallback_app" / DOWNLOAD_SUBFOLDER).resolve()


def test_default_download_dir_is_local_profile_recordings():
    path = default_download_dir()
    assert path.is_absolute()
    assert path.name == DOWNLOAD_SUBFOLDER
    assert path.parent.name == DOWNLOAD_APP_FOLDER
    assert path.parent.parent == user_documents_dir()
    assert not is_cloud_path(path)


def test_cloud_download_dir_is_rejected():
    cloud = r"C:\Users\example\OneDrive\Documents\CRFS IQ Recorder\Recordings"
    assert is_cloud_path(cloud)
    assert resolved_download_dir(cloud) == default_download_dir()
    state = ConnectionState.from_settings({"host": "192.0.2.10", "download_dir": cloud})
    assert state.download_dir == ""
    assert not is_cloud_path(state.persistable()["download_dir"])


def test_empty_stored_path_uses_default():
    assert resolved_download_dir("") == default_download_dir()
    assert resolved_download_dir(None) == default_download_dir()


def test_ensure_download_dir_creates_folder(tmp_path):
    target = tmp_path / "CRFS IQ Recorder" / "Recordings"
    created = ensure_download_dir(str(target))
    assert created.is_dir()
    assert created == target.resolve()


def test_unique_names_in_fixed_folder(tmp_path):
    from crfs_iq_recorder.download_util import unique_destination

    folder = ensure_download_dir(str(tmp_path / "Recordings"))
    first = folder / "iq.wav"
    first.write_bytes(b"one")
    second = unique_destination(folder / "iq.wav")
    assert second.name == "iq (2).wav"
    assert second.parent == folder


def test_settings_remember_download_dir(tmp_path, monkeypatch):
    from crfs_iq_recorder import paths

    settings = tmp_path / ".crfs_iq_recorder_settings.json"
    monkeypatch.setattr(paths, "settings_path", lambda: settings)
    chosen = tmp_path / "My IQ"
    state = ConnectionState(host="192.0.2.10", download_dir=str(chosen))
    save_settings(state.persistable())
    loaded = ConnectionState.from_settings(load_settings())
    assert loaded.host == "192.0.2.10"
    assert loaded.download_dir == str(chosen)
    assert "http_password" not in load_settings()
    assert "sftp_password" not in load_settings()
    assert "demo" not in load_settings()
    assert "password" not in settings.read_text(encoding="utf-8").casefold()


def test_settings_remember_band_time_unit_and_format(tmp_path, monkeypatch):
    from crfs_iq_recorder import paths

    settings = tmp_path / ".crfs_iq_recorder_settings.json"
    monkeypatch.setattr(paths, "settings_path", lambda: settings)
    state = ConnectionState(
        host="192.0.2.13",
        freq_unit="GHz",
        freq_mode="start_end",
        start_hz=790_000_000,
        end_hz=810_000_000,
        center_hz=800_000_000,
        bandwidth_hz=20_000_000,
        recorded_time_s=4,
        recording_format="HDF5",
    )
    save_settings(state.persistable())
    loaded = ConnectionState.from_settings(load_settings())
    assert loaded.freq_unit == "GHz"
    assert loaded.freq_mode == "start_end"
    assert loaded.start_hz == 790_000_000
    assert loaded.end_hz == 810_000_000
    assert loaded.recorded_time_s == 4
    assert loaded.recording_format == "HDF5"
    plan = loaded.frequency_plan()
    assert plan.center_hz == 800_000_000
    assert plan.bandwidth_hz == 20_000_000
    assert "demo" not in load_settings()


def test_unverified_format_is_not_restored():
    state = ConnectionState.from_settings({"recording_format": "BIN", "freq_unit": "THz"})
    assert state.recording_format == "WAVE"
    assert state.freq_unit == "MHz"
    hz_saved = ConnectionState.from_settings({"freq_unit": "Hz"})
    assert hz_saved.freq_unit == "MHz"


def test_legacy_settings_without_download_dir_use_default():
    state = ConnectionState.from_settings({"host": "192.0.2.12"})
    assert state.download_dir == ""
    assert resolved_download_dir(state.download_dir) == default_download_dir()
    assert Path(state.persistable()["download_dir"]) == default_download_dir()
