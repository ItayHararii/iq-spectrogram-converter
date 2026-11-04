import pytest

from crfs_iq_recorder.iq_wav import stereo_iq_tone_wav_bytes, write_stereo_iq_tone_wav
from crfs_iq_recorder.spectrogram_preview import PreviewError, build_preview_from_path, preview_dependencies


def test_preview_from_stereo_iq_wav_is_sampled(tmp_path):
    ok, reason = preview_dependencies()
    if not ok:
        raise AssertionError(f"Preview dependencies missing in this environment: {reason}")
    path = tmp_path / "tone.wav"
    write_stereo_iq_tone_wav(path, seconds=0.12, sample_rate=48000)
    result = build_preview_from_path(path, center_hz=800_000_000, bandwidth_hz=12_500_000)
    assert result.png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.caption.startswith("First")
    assert "800" in result.caption
    assert len(result.caption) < 80
    assert "whole recording contains no signal" not in result.caption.lower()
    assert result.layout == "stereo I/Q WAVE"
    assert result.sample_rate == 48000
    assert result.sampled_seconds > 0


def test_preview_rejects_mono_wav(tmp_path):
    import wave

    path = tmp_path / "mono.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 200)
    try:
        build_preview_from_path(path)
        raise AssertionError("expected PreviewError")
    except PreviewError as exc:
        assert "stereo" in str(exc).lower()


def test_preview_uses_low_resolution_settings():
    from crfs_iq_recorder.spectrogram_preview import (
        PREVIEW_DPI,
        PREVIEW_MAX_BYTES,
        PREVIEW_N_FFT,
        PREVIEW_TIME_BINS,
    )

    assert PREVIEW_N_FFT <= 256
    assert PREVIEW_TIME_BINS <= 128
    assert PREVIEW_DPI <= 90
    assert PREVIEW_MAX_BYTES <= 1_048_576


def test_demo_wav_bytes_are_valid_wave():
    payload = stereo_iq_tone_wav_bytes()
    assert payload[:4] == b"RIFF"
    assert b"WAVE" in payload[:16]


def test_preview_array_for_the_interactive_view(tmp_path):
    """render_png=False returns the waterfall array with an absolute axis and the requested range."""
    from datetime import datetime, timezone

    import numpy as np

    from crfs_iq_recorder.capture_measure import PartMeasurement
    from crfs_iq_recorder.spectrogram_preview import VIEW_TIME_BINS

    path = tmp_path / "tone.wav"
    write_stereo_iq_tone_wav(path, seconds=0.12, sample_rate=48000)
    part = PartMeasurement(
        path=str(path),
        size=path.stat().st_size,
        sample_rate_hz=48000.0,
        centre_hz=800_000_000.0,
        bandwidth_hz=40_000.0,
        signal_time=datetime(2026, 11, 4, 22, 48, 50, tzinfo=timezone.utc),
    )
    result = build_preview_from_path(
        path,
        cache_id=("t", 1, 2, 799_990_000, 800_030_000),
        metadata=part,
        requested_start_hz=799_990_000,
        requested_stop_hz=800_030_000,
        render_png=False,
    )
    assert result.png_bytes == b""
    assert result.cache_key == ("t", 1, 2, 799_990_000, 800_030_000)
    data = result.data
    assert data is not None
    assert data.power_db.dtype == np.float32
    assert data.power_db.shape[0] == data.n_freq and data.power_db.shape[1] == data.n_time
    assert data.n_time <= VIEW_TIME_BINS
    assert data.rgb.shape == (data.n_time, data.n_freq, 3) and data.rgb.dtype == np.uint8
    assert data.colorbar_rgb.shape == (256, 3)
    assert data.vmin < data.vmax
    assert data.duration_s == pytest.approx(result.sampled_seconds)
    assert data.absolute
    assert data.freq_lo == pytest.approx(800.0 - 0.024) and data.freq_hi == pytest.approx(800.0 + 0.024)
    assert data.requested_mhz == (799.99, 800.03)
    assert data.capture_start == part.signal_time
    assert np.isfinite(data.power_db).all()


def test_preview_array_relative_axis_without_metadata(tmp_path):
    path = tmp_path / "tone.wav"
    write_stereo_iq_tone_wav(path, seconds=0.05, sample_rate=48000)
    result = build_preview_from_path(path, render_png=False)
    data = result.data
    assert data is not None
    assert not data.absolute
    assert data.requested_mhz is None and data.capture_start is None
    assert data.freq_lo < 0 < data.freq_hi
    assert result.png_bytes == b""


def test_preview_decodes_headerless_xdat_prefix(tmp_path):
    """The sensor's .xdat prefix has no header: int16 I/Q pairs at the .xhdr sample rate."""
    import numpy as np

    rate = 1_000_000.0
    n = 20_000
    t = np.arange(n) / rate
    tone = np.exp(2j * np.pi * 100_000.0 * t) * 12000.0
    raw = np.empty(2 * n, dtype="<i2")
    raw[0::2] = np.round(tone.real).astype(np.int16)
    raw[1::2] = np.round(tone.imag).astype(np.int16)
    path = tmp_path / "prefix.wav"  # the worker's temp file keeps a .wav suffix
    path.write_bytes(raw.tobytes())
    from crfs_iq_recorder.capture_measure import PartMeasurement

    header = PartMeasurement(path="/remote/prefix.xdat", size=len(raw.tobytes()), sample_rate_hz=rate, centre_hz=708_000_000.0)
    # Without the .xhdr measurement the centre is unknown: a planned centre alone never places the axis.
    relative = build_preview_from_path(path, xdat_sample_rate_hz=rate, center_hz=708_000_000, render_png=False)
    assert relative.data is not None and not relative.data.absolute
    assert "centre not confirmed" in relative.caption
    result = build_preview_from_path(path, xdat_sample_rate_hz=rate, metadata=header, render_png=False)
    data = result.data
    assert data is not None
    assert result.sample_rate == rate
    assert data.duration_s == pytest.approx(n / rate, rel=1e-3)
    assert data.absolute and data.freq_lo == pytest.approx(707.5) and data.freq_hi == pytest.approx(708.5)
    # The tone sits at +100 kHz: the strongest frequency row is above the centre.
    peak_row = int(np.argmax(data.power_db.max(axis=1)))
    assert peak_row > data.n_freq // 2
    assert abs(data.freq_lo + peak_row / data.n_freq * (data.freq_hi - data.freq_lo) - 708.1) < 0.02
