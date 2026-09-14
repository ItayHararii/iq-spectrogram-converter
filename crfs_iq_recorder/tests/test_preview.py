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
