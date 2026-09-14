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


def write_stereo_iq_tone_wav(path, **kwargs) -> int:
    payload = stereo_iq_tone_wav_bytes(**kwargs)
    path.write_bytes(payload)
    return len(payload)
