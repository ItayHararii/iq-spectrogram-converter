"""Converter D: library defaults. No GUI."""

from pathlib import Path

import iq_gui


def test_remap_c_project_to_d(monkeypatch, tmp_path):
    fake_d = tmp_path / "D"
    fake_d.mkdir()
    monkeypatch.setattr(iq_gui, "data_drive_root", lambda: fake_d)
    monkeypatch.setattr(iq_gui, "preferred_library_dir", lambda: fake_d / iq_gui.CONVERTER_FOLDER_NAME)
    out = iq_gui.remap_legacy_library_path(r"C:\IQ Spectogram Converter\IQ Results")
    assert Path(out) == fake_d / "IQ Spectogram Converter" / "IQ Results"
    col = iq_gui.remap_legacy_library_path(r"C:\IQ Spectogram Converter\IQ Collection")
    assert Path(col) == fake_d / "IQ Spectogram Converter" / "IQ Collection"


def test_remap_legacy_recordings_to_collection(monkeypatch, tmp_path):
    fake_d = tmp_path / "D"
    fake_d.mkdir()
    monkeypatch.setattr(iq_gui, "data_drive_root", lambda: fake_d)
    monkeypatch.setattr(iq_gui, "preferred_library_dir", lambda: fake_d / iq_gui.CONVERTER_FOLDER_NAME)
    mapped = iq_gui.remap_legacy_library_path(r"C:\Users\example\CRFS IQ Recorder\Recordings")
    assert Path(mapped) == fake_d / "IQ Spectogram Converter" / "IQ Collection"


def test_remap_leaves_custom_c_path(monkeypatch, tmp_path):
    fake_d = tmp_path / "D"
    fake_d.mkdir()
    monkeypatch.setattr(iq_gui, "data_drive_root", lambda: fake_d)
    custom = r"C:\Collection\Other WAVs"
    assert iq_gui.remap_legacy_library_path(custom) == custom


def test_brand_icons_exist():
    assert iq_gui.ICON_ICO_PATH.is_file()
    assert iq_gui.ICON_PNG_PATH.is_file()
