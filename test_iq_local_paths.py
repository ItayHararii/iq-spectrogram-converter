"""Converter library defaults. No GUI."""

from pathlib import Path

import iq_gui


def test_remap_c_project_to_app_dir():
    out = iq_gui.remap_legacy_library_path(r"C:\IQ Spectogram Converter\IQ Results")
    assert Path(out) == iq_gui.APP_DIR / "IQ Results"
    col = iq_gui.remap_legacy_library_path(r"C:\IQ Spectogram Converter\IQ Collection")
    assert Path(col) == iq_gui.APP_DIR / "IQ Collection"


def test_remap_legacy_recordings_to_collection():
    mapped = iq_gui.remap_legacy_library_path(r"C:\Users\example\CRFS IQ Recorder\Recordings")
    assert Path(mapped) == iq_gui.APP_DIR / "IQ Collection"


def test_remap_leaves_custom_c_path():
    custom = r"C:\Collection\Other WAVs"
    assert iq_gui.remap_legacy_library_path(custom) == custom


def test_defaults_are_next_to_the_app():
    assert iq_gui.DEFAULT_OUTPUT == iq_gui.APP_DIR / "IQ Results"
    assert iq_gui.DEFAULT_INPUT_DIR == iq_gui.APP_DIR / "IQ Collection"


def test_brand_icons_exist():
    assert iq_gui.ICON_ICO_PATH.is_file()
    assert iq_gui.ICON_PNG_PATH.is_file()
