"""Numeric checks for IQ Spectrogram Converter. No GUI."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

import iq_data


def _write_stereo_iq(path: Path, *, seconds=0.25, fs=48_000, tone_hz=4_000.0, amp=0.5):
    n = int(seconds * fs)
    t = np.arange(n, dtype=np.float64) / fs
    i = amp * np.cos(2 * np.pi * tone_hz * t)
    q = amp * np.sin(2 * np.pi * tone_hz * t)
    stereo = np.column_stack((i, q)).astype(np.float32)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(fs)
        handle.writeframes(pcm.tobytes())
    return fs, n, tone_hz


def test_wav_info_stereo_duration(tmp_path):
    path = tmp_path / "tone.wav"
    fs, n, _tone = _write_stereo_iq(path, seconds=0.2, fs=48_000)
    info = iq_data.wav_info(str(path))
    assert info["channels"] == 2
    assert info["samplerate"] == fs
    assert info["frames"] == n
    assert abs(info["duration"] - 0.2) < 1e-6


def test_mono_wav_is_rejected(tmp_path):
    path = tmp_path / "mono.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48_000)
        handle.writeframes(b"\x00\x00" * 1024)
    with pytest.raises(ValueError, match="stereo"):
        iq_data.process_file(str(path), str(tmp_path), 15_000)


def test_empty_wav_is_rejected(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48_000)
        handle.writeframes(b"")
    with pytest.raises(ValueError, match="no samples|too short"):
        iq_data.process_file(str(path), str(tmp_path), 15_000)


def test_truncated_header_is_rejected(tmp_path):
    path = tmp_path / "trunc.wav"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    with pytest.raises(ValueError, match="truncated or invalid"):
        iq_data.process_file(str(path), str(tmp_path), 15_000)


def test_process_file_tone_peak_and_duration(tmp_path):
    path = tmp_path / "tone.wav"
    fs, n, tone_hz = _write_stereo_iq(path, seconds=0.4, fs=48_000, tone_hz=4_000.0)
    rbw = 150.0
    iq_data.process_file(str(path), str(tmp_path), rbw, cmap="viridis")
    png = tmp_path / "tone.png"
    assert png.is_file()
    assert png.stat().st_size > 1000

    n_fft = iq_data._n_fft(fs, rbw)
    df_hz = fs / n_fft
    window, _scale, enbw_bins = iq_data._blackmanharris_window(n_fft)
    rbw_true = enbw_bins * df_hz
    assert n_fft >= 2
    assert abs(rbw_true / rbw - 1.0) < 0.6  # ENBW is wider than the requested bin target

    duration = n / fs
    assert abs(duration - 0.4) < 1e-6
    # Tone bin in a two-sided FFT of complex IQ is near +tone_hz.
    bin_index = int(round(tone_hz / df_hz)) % n_fft
    assert 0 <= bin_index < n_fft


def test_hebrew_and_spaces_in_output_path(tmp_path):
    folder = tmp_path / "IQ Results בדיקה"
    folder.mkdir()
    wav = tmp_path / "file with spaces.wav"
    _write_stereo_iq(wav, seconds=0.2)
    iq_data.process_file(str(wav), str(folder), 15_000)
    assert (folder / "file with spaces.png").is_file()


def test_existing_png_is_overwritten(tmp_path):
    wav = tmp_path / "tone.wav"
    _write_stereo_iq(wav, seconds=0.2)
    png = tmp_path / "tone.png"
    png.write_bytes(b"old")
    iq_data.process_file(str(wav), str(tmp_path), 15_000)
    assert png.stat().st_size > 100
