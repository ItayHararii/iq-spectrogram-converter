"""File-size estimates.

The WAVE estimate follows the sensor's sample-rate steps measured on an RFeye
Node 40-8 (firmware 2.25-325, 31 Oct 2026, 13 recordings from 1 MHz to 60 MHz):
the Node records 16-bit I/Q at 125 MS/s divided by a power of two, choosing the
smallest rate that is at least 1.25 × the requested bandwidth, capped at
62.5 MS/s. The old linear reference (61.051 MB at 10 MHz × 1 s) is kept for
comparison; it was 17-46 % off whenever the bandwidth was not 5, 10, 20 or
40 MHz. The technical estimate never assumes RF bandwidth equals sample rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .constants import (
    DATASHEET_BYTES_PER_COMPLEX_SAMPLE,
    DATASHEET_CHANNELS,
    DATASHEET_FULLRATE_COMPLEX_SPS,
    EMPIRICAL_REF_BW_HZ,
    EMPIRICAL_REF_SIZE_MB,
    EMPIRICAL_REF_TIME_S,
)

REF_SIZE = Decimal(EMPIRICAL_REF_SIZE_MB)
REF_BW = Decimal(EMPIRICAL_REF_BW_HZ)
REF_TIME = Decimal(str(EMPIRICAL_REF_TIME_S))

# Measured WAVE layout on the Node 40-8: a 44-byte header plus a ~648-byte
# trailer chunk per part, parts split at this many payload bytes, and about
# 0.6 % more samples than the nominal time (0.06-0.6 % across 1-40 s).
WAVE_HEADER_BYTES = 44 + 648
WAVE_PART_PAYLOAD_BYTES = 523_776_000
WAVE_EXTRA_TIME_RATIO = Decimal("0.006")
MAX_IQ_SAMPLE_RATE_HZ = 62_500_000
MIN_RATE_OVER_BANDWIDTH = Decimal("1.25")


class SizeEstimateError(ValueError):
    """Invalid inputs for a size estimate."""


def _positive_decimal(value, *, name: str) -> Decimal:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise SizeEstimateError(f"{name} must be positive for a size estimate.")
    return number


def iq_sample_rate_hz(bandwidth_hz: int | Decimal, *, max_rate_hz: int | None = None) -> int:
    """Complex sample rate the Node uses for a requested bandwidth.

    125 MS/s / 2^k, the smallest value that is >= 1.25 × bandwidth, capped at
    `max_rate_hz`: 62.5 MS/s on the Node 40-8 (so anything above 50 MHz is
    recorded at 62.5 MS/s), 125 MS/s on the Node 100-x.
    Verified on hardware: 1 MHz -> 1.953125, 5 and 6 MHz -> 7.8125,
    8/10/12.5 MHz -> 15.625, 20/21 MHz -> 31.25, 30-60 MHz -> 62.5 MS/s on the
    Node 40-8; 20/25 MHz -> 31.25, 50 MHz -> 62.5, 100 MHz -> 125 MS/s on the
    Node 100-18.
    """
    bw = _positive_decimal(bandwidth_hz, name="Bandwidth")
    needed = bw * MIN_RATE_OVER_BANDWIDTH
    rate = int(max_rate_hz or MAX_IQ_SAMPLE_RATE_HZ)
    while rate % 2 == 0 and Decimal(rate // 2) >= needed:
        rate //= 2
    return int(rate)


@dataclass(frozen=True)
class WaveSizeModel:
    bandwidth_hz: int
    duration_s: Decimal
    sample_rate_hz: int
    payload_bytes: int
    parts: int
    total_bytes: int

    @property
    def megabytes(self) -> Decimal:
        return Decimal(self.total_bytes) / Decimal(1_000_000)


def wave_size_model(
    bandwidth_hz: int | Decimal,
    recorded_time_seconds: Decimal | float | str,
    *,
    max_rate_hz: int | None = None,
) -> WaveSizeModel:
    """Expected on-disk size of a WAVE recording, all split parts combined."""
    bw = _positive_decimal(bandwidth_hz, name="Bandwidth")
    seconds = _positive_decimal(recorded_time_seconds, name="Recorded time")
    rate = iq_sample_rate_hz(bw, max_rate_hz=max_rate_hz)
    frames = (Decimal(rate) * seconds * (Decimal(1) + WAVE_EXTRA_TIME_RATIO)).to_integral_value(rounding=ROUND_CEILING)
    payload = int(frames) * DATASHEET_BYTES_PER_COMPLEX_SAMPLE
    parts = max(1, -(-payload // WAVE_PART_PAYLOAD_BYTES))
    return WaveSizeModel(
        bandwidth_hz=int(bw),
        duration_s=seconds,
        sample_rate_hz=rate,
        payload_bytes=payload,
        parts=parts,
        total_bytes=payload + parts * WAVE_HEADER_BYTES,
    )


def estimated_size_mb(bandwidth_hz: int | Decimal, recorded_time_seconds: Decimal | float | str) -> Decimal:
    """Decimal megabytes for the GUI estimate (sample-rate model)."""
    return wave_size_model(bandwidth_hz, recorded_time_seconds).megabytes


def recorded_seconds_from_bytes(
    total_bytes: int,
    bandwidth_hz: int | Decimal,
    parts: int = 1,
    *,
    max_rate_hz: int | None = None,
) -> float:
    """How many seconds of IQ a set of WAVE parts holds, from their combined size."""
    rate = iq_sample_rate_hz(bandwidth_hz, max_rate_hz=max_rate_hz)
    payload = max(int(total_bytes) - max(int(parts), 1) * WAVE_HEADER_BYTES, 0)
    return payload / float(DATASHEET_BYTES_PER_COMPLEX_SAMPLE * rate)


def empirical_size_mb(bandwidth_hz: int | Decimal, recorded_time_seconds: Decimal | float | str) -> Decimal:
    """Legacy linear reference: 61.051 × (bandwidth_Hz / 10_000_000) × recorded_time_seconds."""
    bw = Decimal(bandwidth_hz)
    t = recorded_time_seconds if isinstance(recorded_time_seconds, Decimal) else Decimal(str(recorded_time_seconds))
    if bw <= 0:
        raise SizeEstimateError("Bandwidth must be positive for a size estimate.")
    if t <= 0:
        raise SizeEstimateError("Recorded time must be positive for a size estimate.")
    return REF_SIZE * (bw / REF_BW) * (t / REF_TIME)


def format_mb(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_estimate(mb: Decimal | float | int | str) -> str:
    """Display MB until 1,000 MB, then GB, then TB. Calculation stays in MB."""
    value = mb if isinstance(mb, Decimal) else Decimal(str(mb))
    if value >= Decimal(1_000_000):
        tb = (value / Decimal(1_000_000)).quantize(Decimal("0.01"))
        return f"{format(tb, 'f')} TB"
    if value >= Decimal(1000):
        gb = (value / Decimal(1000)).quantize(Decimal("0.01"))
        return f"{format(gb, 'f')} GB"
    return f"{format_mb(value)} MB"


@dataclass(frozen=True)
class TechnicalEstimate:
    payload_bytes: Decimal
    megabytes_decimal: Decimal  # bytes / 1e6
    mebibytes: Decimal  # bytes / 2^20
    notes: str


def technical_payload_bytes(
    complex_sample_rate_hz: Decimal | float | int,
    recorded_time_seconds: Decimal | float | str,
    bytes_per_complex_sample: int = DATASHEET_BYTES_PER_COMPLEX_SAMPLE,
    number_of_recorded_channels: int = DATASHEET_CHANNELS,
) -> TechnicalEstimate:
    fs = (
        complex_sample_rate_hz
        if isinstance(complex_sample_rate_hz, Decimal)
        else Decimal(str(complex_sample_rate_hz))
    )
    t = recorded_time_seconds if isinstance(recorded_time_seconds, Decimal) else Decimal(str(recorded_time_seconds))
    if fs <= 0:
        raise SizeEstimateError("Complex sample rate must be positive.")
    if t <= 0:
        raise SizeEstimateError("Recorded time must be positive.")
    if bytes_per_complex_sample <= 0:
        raise SizeEstimateError("Bytes per complex sample must be positive.")
    if number_of_recorded_channels <= 0:
        raise SizeEstimateError("Channel count must be positive.")
    payload = fs * t * Decimal(bytes_per_complex_sample) * Decimal(number_of_recorded_channels)
    notes = (
        "Payload only: documented WAVE headers / extra metadata are not included. "
        "RF bandwidth is not used as the complex sample rate. "
        "Default 4 bytes/complex sample is 16-bit I + 16-bit Q from CRFS Node datasheets; "
        "confirm for the selected recording_format."
    )
    return TechnicalEstimate(
        payload_bytes=payload,
        megabytes_decimal=payload / Decimal(1_000_000),
        mebibytes=payload / Decimal(1_048_576),
        notes=notes,
    )
