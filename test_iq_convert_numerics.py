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


# ---------------------------------------------------------------------------
# Numerical checks against independently computed expectations.
#
# Geometry used throughout: fs = 48 kHz, RBW request 100 Hz -> n_fft = 1024,
# bin width df = 46.875 Hz. A tone on bin k sits at exactly k * df.
# Scale: 0 dBFS/bin is a complex tone exp(j w n) with |a| = 1 (WAV full scale).
# ---------------------------------------------------------------------------

FS = 48_000
RBW = 100.0
N_FFT = 1024
DF = FS / N_FFT
AMP_TOL_DB = 0.05  # on-bin amplitude tolerance (float32 I/Q, PCM rounding excluded)
BH4_SCALLOP_DB = 0.83  # Blackman-Harris 4-term worst-case scalloping loss


def _write_iq_float(path: Path, i: np.ndarray, q: np.ndarray, fs: int = FS, subtype: str = "FLOAT") -> None:
    import soundfile as sf

    sf.write(str(path), np.column_stack((i, q)).astype(np.float32), fs, subtype=subtype)


def _tone(freq_hz: float, seconds: float, amp: float = 1.0, fs: int = FS, phase: float = 0.0):
    n = int(round(seconds * fs))
    t = np.arange(n, dtype=np.float64) / fs
    i = amp * np.cos(2 * np.pi * freq_hz * t + phase)
    q = amp * np.sin(2 * np.pi * freq_hz * t + phase)
    return i, q


def _convert(tmp_path: Path, name: str, i, q, rbw: float = RBW, fs: int = FS, **kwargs):
    wav = tmp_path / f"{name}.wav"
    _write_iq_float(wav, i, q, fs=fs)
    stats: dict = {}
    iq_data.process_file(str(wav), str(tmp_path), rbw, cmap="gray", stats=stats, **kwargs)
    assert (tmp_path / f"{name}.png").is_file()
    return stats


def test_geometry_assumptions_hold():
    assert iq_data._n_fft(FS, RBW) == N_FFT
    _window, _scale, enbw = iq_data._blackmanharris_window(N_FFT)
    # Periodic 4-term Blackman-Harris: ENBW 2.0044 bins (Harris 1978).
    assert abs(enbw - 2.0044) < 0.002


def test_full_scale_on_bin_tone_reads_0_dbfs_at_the_right_frequency(tmp_path):
    k = 40
    f0 = k * DF  # 1875 Hz, exactly on a bin centre
    i, q = _tone(f0, 0.5)
    stats = _convert(tmp_path, "onbin", i, q)
    assert stats["n_fft"] == N_FFT
    assert abs(stats["peak_db"]) < AMP_TOL_DB
    assert abs(stats["peak_freq_hz"] - f0) < 1e-6
    # Steady tone: the Welch mean at the tone bin equals the max-hold value.
    idx = int(np.argmin(np.abs(stats["freqs_hz"] - f0)))
    assert abs(stats["spectrum_mean_db"][idx]) < AMP_TOL_DB
    assert abs(stats["spectrum_peak_db"][idx]) < AMP_TOL_DB
    # Nothing comparable at the image frequency (-f0): > 60 dB down.
    mirror = int(np.argmin(np.abs(stats["freqs_hz"] + f0)))
    assert stats["spectrum_peak_db"][mirror] < -60.0


@pytest.mark.parametrize("amp, expected_db", [(0.5, -6.0206), (0.1, -20.0), (0.01, -40.0)])
def test_amplitude_scaling_follows_20log10(tmp_path, amp, expected_db):
    f0 = 40 * DF
    i, q = _tone(f0, 0.3, amp=amp)
    stats = _convert(tmp_path, f"amp_{amp}", i, q)
    assert abs(stats["peak_db"] - expected_db) < AMP_TOL_DB


def test_peak_matches_independent_windowed_fft(tmp_path):
    """Compare the engine's bin power with a plain numpy FFT of one frame."""
    f0 = 40 * DF + 0.3 * DF  # off-bin, so window shape matters
    i, q = _tone(f0, 0.3, amp=0.7)
    stats = _convert(tmp_path, "indep", i, q)
    from scipy.signal import get_window

    w = get_window("blackmanharris", N_FFT, fftbins=True)
    x = (i[:N_FFT] + 1j * q[:N_FFT]).astype(np.complex128)
    spec = np.fft.fftshift(np.fft.fft(x * w) / w.sum())
    expected_db = 10 * np.log10(np.abs(spec) ** 2 + 1e-12)
    got = stats["spectrum_peak_db"]
    # Steady tone: every frame has the same magnitude spectrum, so max-hold
    # equals the single-frame result. Compare the 9 bins around the peak.
    centre = int(np.argmax(expected_db))
    sl = slice(centre - 4, centre + 5)
    assert np.allclose(got[sl], expected_db[sl], atol=0.02)
    assert abs(float(got.max()) - float(expected_db.max())) < 0.02


def test_off_bin_tone_loss_is_within_blackman_harris_scalloping(tmp_path):
    f0 = (40 + 0.5) * DF  # exactly between two bins: worst case
    i, q = _tone(f0, 0.3)
    stats = _convert(tmp_path, "offbin", i, q)
    assert -BH4_SCALLOP_DB - 0.1 < stats["peak_db"] < -BH4_SCALLOP_DB + 0.1
    # Reported peak frequency is one of the two neighbouring bin centres.
    assert abs(abs(stats["peak_freq_hz"] - f0) - DF / 2) < 1e-6


def test_negative_frequency_tone_and_iq_channel_order(tmp_path):
    f0 = 40 * DF
    i, q = _tone(f0, 0.3)
    # I = left = real, Q = right = imag. exp(-jwt) = cos - j sin -> negative frequency.
    stats = _convert(tmp_path, "negfreq", i, -q)
    assert abs(stats["peak_freq_hz"] + f0) < 1e-6
    assert abs(stats["peak_db"]) < AMP_TOL_DB
    # Swapping the channels of a +f0 tone gives sin + j cos = j exp(-jwt): also -f0.
    stats_swapped = _convert(tmp_path, "swapped", q, i)
    assert abs(stats_swapped["peak_freq_hz"] + f0) < 1e-6


def test_two_tones_keep_independent_levels_and_rbw_resolves_them(tmp_path):
    f1, f2 = 40 * DF, 60 * DF  # 937.5 Hz apart, far more than one RBW
    i1, q1 = _tone(f1, 0.3, amp=1.0)
    i2, q2 = _tone(f2, 0.3, amp=0.1)
    stats = _convert(tmp_path, "twotone", i1 + i2, q1 + q2)
    spec = stats["spectrum_peak_db"]
    freqs = stats["freqs_hz"]
    b1 = int(np.argmin(np.abs(freqs - f1)))
    b2 = int(np.argmin(np.abs(freqs - f2)))
    assert abs(spec[b1] - 0.0) < AMP_TOL_DB
    assert abs(spec[b2] - (-20.0)) < AMP_TOL_DB
    valley = spec[b1 + 5 : b2 - 5].max()
    assert valley < -60.0


def test_reported_rbw_is_enbw_times_bin_width(tmp_path):
    i, q = _tone(40 * DF, 0.2)
    stats = _convert(tmp_path, "rbw", i, q)
    assert abs(stats["df_hz"] - DF) < 1e-9
    assert abs(stats["rbw_true_hz"] - stats["enbw_bins"] * DF) < 1e-9
    assert abs(stats["rbw_true_hz"] - 2.0044 * DF) < 0.2
    assert stats["rbw_requested_hz"] == RBW


def test_chunked_processing_equals_single_block_and_covers_all_frames(tmp_path, monkeypatch):
    """Frames must line up across chunk boundaries (leftover handling)."""
    f0 = 40 * DF
    i, q = _tone(f0, 0.6)
    n = len(i)
    hop = N_FFT // 2
    expected_frames = 1 + (n - N_FFT) // hop
    whole = _convert(tmp_path, "whole", i, q)
    assert whole["frames_done"] == whole["n_frames"] == expected_frames

    # Force blocks of 3 frames so the loop concatenates leftovers many times.
    monkeypatch.setattr(iq_data, "_block_sample_count", lambda n_fft, hop_: 2 * hop_ + n_fft)
    chunked = _convert(tmp_path, "chunked", i, q)
    assert chunked["frames_done"] == expected_frames
    assert np.allclose(chunked["spectrum_mean_db"], whole["spectrum_mean_db"], atol=1e-3)
    assert np.allclose(chunked["spectrum_peak_db"], whole["spectrum_peak_db"], atol=1e-3)
    assert chunked["waterfall_db"].shape == whole["waterfall_db"].shape
    # Below -100 dBFS/bin the synthetic signal is pure float32 rounding noise
    # sitting on the 1e-12 epsilon floor; compare only meaningful cells.
    a, b = chunked["waterfall_db"], whole["waterfall_db"]
    meaningful = (a > -100.0) | (b > -100.0)
    assert meaningful.sum() > 50
    assert np.allclose(a[meaningful], b[meaningful], atol=1e-3)
    assert (a[~meaningful] < -100.0).all() and (b[~meaningful] < -100.0).all()


def test_time_axis_puts_a_late_tone_in_the_second_half(tmp_path):
    seconds = 1.0
    i, q = _tone(40 * DF, seconds)
    n = len(i)
    half = n // 2
    rng = np.random.default_rng(1)
    noise = 1e-4 * rng.standard_normal(half)
    i[:half] = noise
    q[:half] = noise
    stats = _convert(tmp_path, "late", i, q)
    wf = stats["waterfall_db"]  # (freq bins, time bins), time index 0 = t = 0
    assert wf.shape[0] == N_FFT  # 1024 <= MAX_FREQ_BINS: no frequency pooling
    bin_idx = 40 + N_FFT // 2  # fftshifted position of +40 bins
    n_time = wf.shape[1]
    early = wf[bin_idx, : n_time // 2 - 2]
    late = wf[bin_idx, n_time // 2 + 2 :]
    assert early.max() < -40.0
    assert late.min() > -1.0
    assert abs(stats["duration_s"] - seconds) < 1e-9


def test_dc_offset_shows_at_zero_hz(tmp_path):
    n = int(0.3 * FS)
    i = np.full(n, 0.5)
    q = np.zeros(n)
    stats = _convert(tmp_path, "dc", i, q)
    assert stats["peak_freq_hz"] == 0.0
    assert abs(stats["peak_db"] - (-6.0206)) < AMP_TOL_DB


def test_clipped_full_scale_square_wave_converts_and_stays_finite(tmp_path):
    n = int(0.3 * FS)
    i = np.where(np.arange(n) % 48 < 24, 1.0, -1.0)
    q = np.zeros(n)
    stats = _convert(tmp_path, "clipped", i, q)
    assert np.isfinite(stats["spectrum_peak_db"]).all()
    assert stats["peak_db"] <= 0.5  # a clipped real square wave cannot exceed full scale


def test_nan_and_inf_samples_in_float_wav_do_not_poison_the_spectrum(tmp_path):
    f0 = 40 * DF
    i, q = _tone(f0, 0.5)
    i[1000] = np.nan
    q[2000] = np.inf
    i[3000] = -np.inf
    stats = _convert(tmp_path, "nonfinite", i, q)
    assert np.isfinite(stats["spectrum_peak_db"]).all()
    assert np.isfinite(stats["spectrum_mean_db"]).all()
    assert abs(stats["peak_freq_hz"] - f0) < 1e-6
    assert abs(stats["peak_db"]) < 0.2  # three zeroed samples barely dent the tone


def test_too_short_file_fails_before_any_window_is_allocated(tmp_path, monkeypatch):
    i, q = _tone(40 * DF, 0.01)  # 480 samples < n_fft

    def _no_alloc(_n_fft):
        raise AssertionError("window allocated before validation")

    monkeypatch.setattr(iq_data, "_blackmanharris_window", _no_alloc)
    wav = tmp_path / "short.wav"
    _write_iq_float(wav, i, q)
    with pytest.raises(ValueError, match="too short"):
        iq_data.process_file(str(wav), str(tmp_path), RBW)


def test_rbw_finer_than_the_fft_cap_is_rejected_with_guidance(tmp_path, monkeypatch):
    i, q = _tone(40 * DF, 0.2)
    wav = tmp_path / "fine.wav"
    _write_iq_float(wav, i, q)
    monkeypatch.setattr(iq_data, "MAX_N_FFT", 512)
    with pytest.raises(ValueError, match="too fine.*at least"):
        iq_data.process_file(str(wav), str(tmp_path), RBW)  # needs 1024 > 512


def test_pcm16_and_float32_encodings_give_the_same_spectrum(tmp_path):
    f0 = 40 * DF
    i, q = _tone(f0, 0.3, amp=0.5)
    float_stats = _convert(tmp_path, "f32", i, q)
    wav16 = tmp_path / "pcm16.wav"
    _write_iq_float(wav16, i, q, subtype="PCM_16")
    pcm_stats: dict = {}
    iq_data.process_file(str(wav16), str(tmp_path), RBW, cmap="gray", stats=pcm_stats)
    assert abs(pcm_stats["peak_db"] - float_stats["peak_db"]) < 0.01
    assert pcm_stats["peak_freq_hz"] == float_stats["peak_freq_hz"]
    assert pcm_stats["n_samples"] == float_stats["n_samples"]
