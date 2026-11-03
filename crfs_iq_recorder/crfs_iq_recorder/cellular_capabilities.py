"""What the connected sensor's model number says, for information only.

Nothing here changes what is sent to the sensor. Each protocol row is sent as
one request with the row's own start/stop frequencies and the selected
duration; the sensor accepts, rejects or executes it by its own rules and the
recorded files are measured afterwards.

From the model number (an RFeye Node "40-8" has 40 MHz instantaneous
bandwidth and tunes to 8 GHz; a "100-18" has 100 MHz and tunes to 18 GHz):

* the tuning range and instantaneous bandwidth, used for a warning before the
  start (the row is still sent unchanged), and
* the sample rate the sensor uses for a bandwidth (125 MS/s divided by a power
  of two, at least 1.25 x the bandwidth; the 40-8 stops at 62.5 MS/s), used
  only for the size estimate.

Observed on an RFeye Node 100-18 (rfeye100400, firmware 2.25-325, 3 Nov
2026): a 500 MHz request (3300-3800 MHz) was accepted and written as one
100 MHz recording centred on 3550 MHz; the sensor's reply and the file
metadata both said 100 MHz.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .size_estimate import MAX_IQ_SAMPLE_RATE_HZ, iq_sample_rate_hz

MHZ = 1_000_000
GHZ = 1_000_000_000
MB = 1_000_000

# Typical lower tuning limit of the RFeye Node series (datasheet value; the
# application does not verify it).
DATASHEET_MIN_HZ = 9_000
# 16-bit I + 16-bit Q per complex sample in the Node's WAVE recordings.
BYTES_PER_SAMPLE = 4
NODE_100_MAX_RATE_HZ = 125_000_000
NODE_40_MAX_RATE_HZ = MAX_IQ_SAMPLE_RATE_HZ

_MODEL_RE = re.compile(r"(?:node\s*)?R?\s*(\d{2,3})\s*-\s*(\d{1,2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class SensorCapability:
    model: str
    family: str
    min_hz: int | None
    max_hz: int | None
    ibw_hz: int | None
    # Highest complex sample rate this model records at (size estimate only).
    max_sample_rate_hz: int
    source: str

    def sample_rate_for(self, bandwidth_hz: int) -> int:
        return iq_sample_rate_hz(bandwidth_hz, max_rate_hz=self.max_sample_rate_hz)

    def capture_bytes(self, bandwidth_hz: int, duration_s: float) -> int:
        return int(self.sample_rate_for(bandwidth_hz) * max(float(duration_s), 0.0) * BYTES_PER_SAMPLE)

    def tuning_note(self, start_hz: int, stop_hz: int) -> str | None:
        """A warning when the range lies outside the model's listed tuning range (sent anyway)."""
        if self.min_hz is not None and start_hz < self.min_hz:
            return f"starts below the {self.min_hz / 1000:g} kHz tuning range listed for this model"
        if self.max_hz is not None and stop_hz > self.max_hz:
            return f"ends above the {self.max_hz / GHZ:g} GHz tuning range listed for this model"
        return None

    def wider_than_receiver(self, bandwidth_hz: int) -> bool:
        return bool(self.ibw_hz) and int(bandwidth_hz) > int(self.ibw_hz or 0)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "family": self.family,
            "min_hz": self.min_hz,
            "max_hz": self.max_hz,
            "ibw_hz": self.ibw_hz,
            "max_sample_rate_hz": self.max_sample_rate_hz,
            "source": self.source,
        }


def capability_for(model: str) -> SensorCapability:
    """Model facts from the model string the sensor reports (e.g. 'R40-8', 'R100-18')."""
    text = (model or "").strip()
    match = _MODEL_RE.search(text)
    if not match:
        return SensorCapability(
            model=text or "unknown",
            family="unknown model",
            min_hz=None,
            max_hz=None,
            ibw_hz=None,
            max_sample_rate_hz=NODE_40_MAX_RATE_HZ,
            source="none",
        )
    ibw_mhz = int(match.group(1))
    max_ghz = int(match.group(2))
    return SensorCapability(
        model=text,
        family=f"RFeye Node {ibw_mhz}-{max_ghz}",
        min_hz=DATASHEET_MIN_HZ,
        max_hz=max_ghz * GHZ,
        ibw_hz=ibw_mhz * MHZ,
        max_sample_rate_hz=NODE_100_MAX_RATE_HZ if ibw_mhz >= 100 else NODE_40_MAX_RATE_HZ,
        source="CRFS datasheet (model number).",
    )


def readable_sensor_name(model: str, fallback: str = "") -> str:
    """'R100-18' -> 'CRFS 100-18' for the workbook's Sensor column."""
    text = (model or "").strip()
    match = _MODEL_RE.search(text)
    if not match:
        return text or fallback
    return f"CRFS {int(match.group(1))}-{int(match.group(2))}"
