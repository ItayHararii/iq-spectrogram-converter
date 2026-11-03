"""Small stereo I/Q WAVE helpers (stdlib). Used by demo mode and tests."""

from __future__ import annotations

import io
import math
import wave
from array import array


def stereo_iq_tone_wav_bytes(
    *,
    seconds: float = 0.08,
    sample_rate: int = 48_000,
    tone_hz: float = 4_000.0,
    amplitude: int = 12_000,
) -> bytes:
    """PCM 16-bit stereo WAVE: cosine on I (left), sine on Q (right)."""
    n = max(32, int(float(seconds) * int(sample_rate)))
    samples = array("h")
    amp = max(1, min(int(amplitude), 32767))
    two_pi = 2.0 * math.pi * float(tone_hz) / float(sample_rate)
    for i in range(n):
        phase = two_pi * i
        samples.append(int(amp * math.cos(phase)))
        samples.append(int(amp * math.sin(phase)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(samples.tobytes())
    return buf.getvalue()


def crfs_style_wav_bytes(
    *,
    frames: int,
    sample_rate: int,
    center_hz: float,
    bandwidth_hz: float,
    signal_time: str,
    receiver: str = "demo",
) -> bytes:
    """16-bit stereo I/Q WAVE laid out like a CRFS Node file: RIFF, fmt, data, then a `meta` signalinfo chunk.

    The samples are a tone at a tenth of the sample rate (I cosine, Q sine), so
    previews and level checks see a signal rather than silence.
    """
    import struct

    frames = max(int(frames), 0)
    step = 2.0 * math.pi / 10.0
    samples = array("h")
    for i in range(frames):
        samples.append(int(8000 * math.cos(step * i)))
        samples.append(int(8000 * math.sin(step * i)))
    data = samples.tobytes()
    meta = (
        '<?xml version="1.0" encoding="utf-8"?><signalinfo><base>'
        f"<signaltime>{signal_time}</signaltime><rf_frequency>{float(center_hz):g}</rf_frequency>"
        f"<bandwidth>{float(bandwidth_hz):g}</bandwidth><data_type>complex</data_type>"
        f"<sample_rate>{float(sample_rate):g}</sample_rate></base><extended><receiver>{receiver}</receiver>"
        "</extended></signalinfo>"
    ).encode("utf-8")
    meta += b"\x00" * (len(meta) & 1)
    fmt = struct.pack("<HHIIHH", 1, 2, int(sample_rate), int(sample_rate) * 4, 4, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    body += b"meta" + struct.pack("<I", len(meta)) + meta
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write_stereo_iq_tone_wav(path, **kwargs) -> int:
    payload = stereo_iq_tone_wav_bytes(**kwargs)
    path.write_bytes(payload)
    return len(payload)


def wav_duration_seconds(data: bytes | None) -> float | None:
    """Duration from a WAVE header. Returns None when the header cannot be verified."""
    if not data or len(data) < 44:
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            rate = int(handle.getframerate() or 0)
            frames = int(handle.getnframes() or 0)
    except (wave.Error, EOFError, OSError, ValueError):
        return None
    if rate <= 0 or frames < 0:
        return None
    return frames / float(rate)


def probe_remote_wav_duration(browser, remote_path: str, *, max_bytes: int = 512) -> float | None:
    """Read a WAVE header from the sensor. Does not invent a duration."""
    if browser is None or not remote_path:
        return None
    import os
    import tempfile
    from pathlib import Path

    fd, name = tempfile.mkstemp(prefix="crfs-iq-dur-", suffix=".wav")
    os.close(fd)
    path = Path(name)
    try:
        browser.read_prefix(remote_path, path, int(max_bytes))
        return wav_duration_seconds(path.read_bytes())
    except Exception:
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
