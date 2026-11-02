"""What the connected sensor can record, kept apart from what the workbook asks for.

Two different limits matter and must not be confused:

* the instantaneous bandwidth (IBW) of the receiver, from the model number
  (an RFeye Node "40-8" has 40 MHz IBW and tunes to 8 GHz), and
* the window it can record to its own disk *without dropping samples*.

On the RFeye Node 40-8 used for this application the second limit was
measured (release 1.8.9/1.8.10 QA): 20 MHz captures at 31.25 MS/s were
complete for 40 s and 10 MHz captures for 120 s, while 40 MHz captures at
62.5 MS/s lost 29-92 % of their samples. The default capture window is
therefore the verified 20 MHz, never the IBW. Other models have no verified
figure here; they get the same conservative default and say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .size_estimate import MAX_IQ_SAMPLE_RATE_HZ, iq_sample_rate_hz

MHZ = 1_000_000
GHZ = 1_000_000_000

# Verified on hardware for this application (see QA_REPORT.md Part 0/1): the
# Node 40-8 sustains gapless IQ to disk at 31.25 MS/s (requests up to 25 MHz
# bandwidth), not at 62.5 MS/s.
DEFAULT_WINDOW_HZ = 20 * MHZ
# Requests above this bandwidth are recorded at 62.5 MS/s, which the Node
# 40-8 could not sustain in testing.
VERIFIED_SAMPLE_RATE_HZ = 31_250_000
# Typical lower tuning limit of the RFeye Node series (datasheet value; the
# application does not verify it).
DATASHEET_MIN_HZ = 9_000

_MODEL_RE = re.compile(r"(?:node\s*)?R?\s*(\d{2,3})\s*-\s*(\d{1,2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class SensorCapability:
    model: str
    family: str
    min_hz: int | None
    max_hz: int | None
    ibw_hz: int | None
    verified_window_hz: int
    verified_note: str
    source: str

    @property
    def default_window_hz(self) -> int:
        return self.verified_window_hz or DEFAULT_WINDOW_HZ

    def window_note(self, window_hz: int) -> str:
        """Why a capture window is (or is not) trusted."""
        if self.verified_window_hz and window_hz <= self.verified_window_hz:
            return f"Within the {self.verified_window_hz // MHZ} MHz window verified gapless on this model."
        rate = iq_sample_rate_hz(window_hz)
        if rate > VERIFIED_SAMPLE_RATE_HZ:
            return (
                f"{window_hz / MHZ:g} MHz is recorded at {rate / MHZ:g} MS/s; the Node 40-8 dropped samples at "
                "that rate in testing. Captures may be incomplete and are measured afterwards."
            )
        if self.ibw_hz and window_hz > self.ibw_hz:
            return f"{window_hz / MHZ:g} MHz exceeds the {self.ibw_hz // MHZ} MHz instantaneous bandwidth of this model."
        if not self.verified_window_hz:
            return (
                f"No gapless-recording figure has been verified for {self.family}; "
                f"{window_hz / MHZ:g} MHz is used as a conservative setting, not a verified capability."
            )
        return (
            f"{window_hz / MHZ:g} MHz is above the {self.verified_window_hz // MHZ} MHz verified on this model "
            f"(same {rate / MHZ:g} MS/s sample rate, but the wider window is not verified)."
        )

    def coverage_issue(self, start_hz: int, stop_hz: int) -> str | None:
        if self.min_hz is not None and start_hz < self.min_hz:
            return f"Starts at {start_hz / MHZ:g} MHz, below the sensor's {self.min_hz / 1000:g} kHz lower limit."
        if self.max_hz is not None and stop_hz > self.max_hz:
            return f"Ends at {stop_hz / MHZ:g} MHz, above the sensor's {self.max_hz / GHZ:g} GHz upper limit."
        return None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "family": self.family,
            "min_hz": self.min_hz,
            "max_hz": self.max_hz,
            "ibw_hz": self.ibw_hz,
            "verified_window_hz": self.verified_window_hz,
            "verified_note": self.verified_note,
            "source": self.source,
        }


def capability_for(model: str) -> SensorCapability:
    """Capability from the model string the sensor reports (e.g. 'R40-8')."""
    text = (model or "").strip()
    match = _MODEL_RE.search(text)
    if not match:
        return SensorCapability(
            model=text or "unknown",
            family="unknown model",
            min_hz=None,
            max_hz=None,
            ibw_hz=None,
            verified_window_hz=0,
            verified_note="Unknown model: frequency coverage and recording limits are not known.",
            source="none",
        )
    ibw_mhz = int(match.group(1))
    max_ghz = int(match.group(2))
    family = f"RFeye Node {ibw_mhz}-{max_ghz}"
    if ibw_mhz == 40 and max_ghz == 8:
        return SensorCapability(
            model=text,
            family=family,
            min_hz=DATASHEET_MIN_HZ,
            max_hz=max_ghz * GHZ,
            ibw_hz=ibw_mhz * MHZ,
            verified_window_hz=20 * MHZ,
            verified_note=(
                "Gapless IQ to the sensor's disk verified for 20 MHz x 40 s and 10 MHz x 120 s (31.25 and "
                "15.625 MS/s); 40 MHz captures at 62.5 MS/s lost 29-92 % of their samples."
            ),
            source="CRFS datasheet (IBW, tuning range) and this application's hardware QA (recording limit).",
        )
    return SensorCapability(
        model=text,
        family=family,
        min_hz=DATASHEET_MIN_HZ,
        max_hz=max_ghz * GHZ,
        ibw_hz=ibw_mhz * MHZ,
        verified_window_hz=0,
        verified_note=(
            f"IBW and tuning range read from the model number ({family}); no gapless-recording figure has "
            "been verified for this model."
        ),
        source="Model number only.",
    )


def normalise_window_hz(window_hz: int, capability: SensorCapability) -> tuple[int, list[str]]:
    """Clamp a requested capture window to what the plan can use; explain changes."""
    notes: list[str] = []
    window = int(window_hz or 0)
    if window <= 0:
        window = capability.default_window_hz
        notes.append(f"Capture window set to the default {window / MHZ:g} MHz.")
    if window % 2:
        window -= 1
        notes.append("Capture window reduced by 1 Hz so window centres are whole Hz.")
    if capability.ibw_hz and window > capability.ibw_hz:
        window = capability.ibw_hz - (capability.ibw_hz % 2)
        notes.append(f"Capture window limited to the {capability.ibw_hz // MHZ} MHz instantaneous bandwidth.")
    rate = iq_sample_rate_hz(window)
    if rate > MAX_IQ_SAMPLE_RATE_HZ:
        notes.append("Capture window exceeds the sensor's maximum sample rate.")
    return window, notes
