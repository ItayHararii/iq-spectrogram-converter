"""File-size estimates.

Empirical estimate uses the user-supplied reference (61.051 MB at 10 MHz × 1 s).
Technical estimate is optional and never assumes RF bandwidth equals sample rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .constants import (
    DATASHEET_BYTES_PER_COMPLEX_SAMPLE,
    DATASHEET_CHANNELS,
    EMPIRICAL_REF_BW_HZ,
    EMPIRICAL_REF_SIZE_MB,
    EMPIRICAL_REF_TIME_S,
)

REF_SIZE = Decimal(EMPIRICAL_REF_SIZE_MB)
REF_BW = Decimal(EMPIRICAL_REF_BW_HZ)
REF_TIME = Decimal(str(EMPIRICAL_REF_TIME_S))


class SizeEstimateError(ValueError):
    """Invalid inputs for a size estimate."""


def empirical_size_mb(bandwidth_hz: int | Decimal, recorded_time_seconds: Decimal | float | str) -> Decimal:
    """61.051 × (bandwidth_Hz / 10_000_000) × recorded_time_seconds."""
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
