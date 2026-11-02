"""What the connected sensor can record, kept apart from what the workbook asks for.

Three different limits matter and must not be confused:

* the instantaneous bandwidth (IBW) of the receiver, from the model number
  (an RFeye Node "40-8" has 40 MHz IBW and tunes to 8 GHz; a "100-18" has
  100 MHz IBW and tunes to 18 GHz),
* the sample rate the sensor uses for a requested bandwidth (125 MS/s divided
  by a power of two, at least 1.25 x the bandwidth; the 40-8 stops at
  62.5 MS/s, the 100-x reach 125 MS/s), and
* how much of a capture reaches the disk complete. The Node buffers the IQ in
  RAM and writes it out slowly; a capture that outgrows the buffer loses
  samples. This is a size limit per capture, not a bandwidth limit.

Verified figures (this application's hardware QA):

* RFeye Node 40-8 (release 1.8.9/1.8.10): 20 MHz captures at 31.25 MS/s were
  complete for 40 s and 10 MHz captures for 120 s; 40 MHz captures at
  62.5 MS/s lost 29-92 % of their samples. 31.25 MS/s is sustained.
* RFeye Node 100-18, serial rfeye100400, firmware 2.25-325 (release 1.10.0,
  2 Nov 2026, 13 disposable captures): 100 MHz is recorded at 125 MS/s
  (WAVE header). Complete captures: 20 and 25 MHz x 3 s (377 MB), 50 MHz x
  3 s (750 MB), 100 MHz x 2 s (1.0 GB, once), and at the 625 MB budget back
  to back with 10 s between them: 25 MHz x 5 s (three times), 50 MHz x
  2.5 s, 12.5 MHz x 10 s, plus the field run's five 20 MHz x 5 s files.
  Incomplete: 100 MHz x 3 s held 2.20 s, 50 MHz x 10 s held 3.75 s (0.94 GB),
  25 MHz x 15 s held 10.36 s (1.29 GB). The Node buffers the IQ in RAM and
  its disk took the data at only 15-30 MB/s, so the datasheet's "25 MHz
  sustained" was not reached on this unit; what is verified is a per-capture
  budget of 625 MB and the bandwidth tiers that follow from it:
  <= 1.25 s -> 100 MHz, <= 2.5 s -> 50 MHz, <= 5 s -> 25 MHz, <= 10 s ->
  12.5 MHz, <= 20 s -> 6.25 MHz, <= 40 s -> 3.125 MHz.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .size_estimate import MAX_IQ_SAMPLE_RATE_HZ, iq_sample_rate_hz

MHZ = 1_000_000
GHZ = 1_000_000_000
MB = 1_000_000

# Default for models without a verified figure: the window the Node 40-8 was
# shown to record gapless (conservative, and said so in the plan).
DEFAULT_WINDOW_HZ = 20 * MHZ
# Typical lower tuning limit of the RFeye Node series (datasheet value; the
# application does not verify it).
DATASHEET_MIN_HZ = 9_000
# 16-bit I + 16-bit Q per complex sample in the Node's WAVE recordings.
BYTES_PER_SAMPLE = 4
# Complete captures on the Node 100-18 (rfeye100400) were verified up to this
# many bytes of IQ in one capture, back to back with 10 s between captures.
NODE_100_18_BUDGET_BYTES = 625 * MB
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
    # Highest complex sample rate this model records at.
    max_sample_rate_hz: int
    # Sample rate the model keeps up indefinitely (None: not verified).
    sustained_rate_hz: int | None
    # Window verified complete at the sustained rate (0: none verified).
    verified_window_hz: int
    # Bytes of IQ one capture may hold and still be complete (None: not verified).
    capture_budget_bytes: int | None
    verified_note: str
    source: str

    @property
    def verified(self) -> bool:
        return bool(self.verified_window_hz or self.capture_budget_bytes)

    @property
    def default_window_hz(self) -> int:
        return self.verified_window_hz or DEFAULT_WINDOW_HZ

    def sample_rate_for(self, bandwidth_hz: int) -> int:
        return iq_sample_rate_hz(bandwidth_hz, max_rate_hz=self.max_sample_rate_hz)

    def capture_bytes(self, bandwidth_hz: int, duration_s: float) -> int:
        return int(self.sample_rate_for(bandwidth_hz) * max(float(duration_s), 0.0) * BYTES_PER_SAMPLE)

    def bandwidth_tiers_hz(self) -> list[int]:
        """Widest bandwidth of each sample-rate step, widest first (100, 50, 25, 12.5 ... MHz)."""
        tiers: list[int] = []
        rate = self.max_sample_rate_hz
        while rate >= 1_000_000:
            # The Node picks the smallest rate >= 1.25 x bandwidth: a tier's widest
            # bandwidth is rate / 1.25, rounded down to whole kHz.
            bandwidth = int(rate / 1.25) // 1000 * 1000
            if self.ibw_hz:
                bandwidth = min(bandwidth, self.ibw_hz)
            if bandwidth > 0 and bandwidth not in tiers:
                tiers.append(bandwidth)
            rate //= 2
        return tiers

    def window_for(self, duration_s: float) -> int:
        """Widest capture window verified complete for one capture of `duration_s`.

        With a verified per-capture byte budget, the widest bandwidth tier whose
        IQ for `duration_s` fits the budget. The sustained window (when verified)
        is always available whatever the duration. Unverified models get the
        conservative default.
        """
        duration = max(float(duration_s), 0.0)
        best = self.verified_window_hz if self.sustained_rate_hz else 0
        if self.capture_budget_bytes and duration > 0:
            for tier in self.bandwidth_tiers_hz():
                if self.capture_bytes(tier, duration) <= self.capture_budget_bytes:
                    best = max(best, tier)
                    break
        if best <= 0:
            best = DEFAULT_WINDOW_HZ
        if self.ibw_hz:
            best = min(best, self.ibw_hz)
        return best - (best % 2)

    def max_duration_for(self, bandwidth_hz: int) -> float | None:
        """Longest complete capture of `bandwidth_hz` (None: unlimited or unknown)."""
        rate = self.sample_rate_for(bandwidth_hz)
        if self.sustained_rate_hz and rate <= self.sustained_rate_hz:
            return None
        if not self.capture_budget_bytes:
            return None
        return self.capture_budget_bytes / float(rate * BYTES_PER_SAMPLE)

    def duration_table(self, durations_s: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 10.0, 20.0)) -> list[tuple[float, int]]:
        """(duration, widest complete window) pairs for the explanation before Start."""
        return [(d, self.window_for(d)) for d in durations_s]

    def window_note(self, window_hz: int, duration_s: float | None = None) -> str:
        """Why a capture window is (or is not) trusted."""
        rate = self.sample_rate_for(window_hz)
        if self.ibw_hz and window_hz > self.ibw_hz:
            return f"{window_hz / MHZ:g} MHz exceeds the {self.ibw_hz // MHZ} MHz instantaneous bandwidth of this model."
        if self.sustained_rate_hz and self.verified_window_hz and window_hz <= self.verified_window_hz:
            return f"Within the {self.verified_window_hz / MHZ:g} MHz window verified gapless on this model."
        if self.capture_budget_bytes and duration_s is not None:
            size = self.capture_bytes(window_hz, duration_s)
            if size <= self.capture_budget_bytes:
                return (
                    f"{window_hz / MHZ:g} MHz x {duration_s:g} s is about {size / MB:,.0f} MB at {rate / MHZ:g} MS/s, "
                    f"within the {self.capture_budget_bytes / MB:,.0f} MB verified complete on this sensor."
                )
            limit = self.max_duration_for(window_hz)
            return (
                f"{window_hz / MHZ:g} MHz x {duration_s:g} s is about {size / MB:,.0f} MB at {rate / MHZ:g} MS/s, above the "
                f"{self.capture_budget_bytes / MB:,.0f} MB verified complete on this sensor"
                + (f" (about {limit:.1f} s at this bandwidth)." if limit else ".")
                + " Captures may be incomplete and are measured afterwards."
            )
        if self.sustained_rate_hz and rate > self.sustained_rate_hz:
            return (
                f"{window_hz / MHZ:g} MHz is recorded at {rate / MHZ:g} MS/s; this model dropped samples at "
                "that rate in testing. Captures may be incomplete and are measured afterwards."
            )
        if not self.verified:
            return (
                f"No complete-capture figure has been verified for {self.family}; "
                f"{window_hz / MHZ:g} MHz is used as a conservative setting, not a verified capability."
            )
        return (
            f"{window_hz / MHZ:g} MHz is above the {self.verified_window_hz / MHZ:g} MHz verified on this model "
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
            "max_sample_rate_hz": self.max_sample_rate_hz,
            "sustained_rate_hz": self.sustained_rate_hz,
            "verified_window_hz": self.verified_window_hz,
            "capture_budget_bytes": self.capture_budget_bytes,
            "verified_note": self.verified_note,
            "source": self.source,
        }


def capability_for(model: str) -> SensorCapability:
    """Capability from the model string the sensor reports (e.g. 'R40-8', 'R100-18')."""
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
            sustained_rate_hz=None,
            verified_window_hz=0,
            capture_budget_bytes=None,
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
            max_sample_rate_hz=NODE_40_MAX_RATE_HZ,
            sustained_rate_hz=31_250_000,
            verified_window_hz=20 * MHZ,
            capture_budget_bytes=None,
            verified_note=(
                "Gapless IQ to the sensor's disk verified for 20 MHz x 40 s and 10 MHz x 120 s (31.25 and "
                "15.625 MS/s); 40 MHz captures at 62.5 MS/s lost 29-92 % of their samples."
            ),
            source="CRFS datasheet (IBW, tuning range) and this application's hardware QA (recording limit).",
        )
    if ibw_mhz == 100 and max_ghz == 18:
        return SensorCapability(
            model=text,
            family=family,
            min_hz=DATASHEET_MIN_HZ,
            max_hz=max_ghz * GHZ,
            ibw_hz=ibw_mhz * MHZ,
            max_sample_rate_hz=NODE_100_MAX_RATE_HZ,
            sustained_rate_hz=None,
            verified_window_hz=0,
            capture_budget_bytes=NODE_100_18_BUDGET_BYTES,
            verified_note=(
                "Verified on rfeye100400 (firmware 2.25-325, 2 Nov 2026): complete captures up to 625 MB of IQ per "
                "capture, back to back with 10 s between them (25 MHz x 5 s, 50 MHz x 2.5 s, 12.5 MHz x 10 s, "
                "20 MHz x 5 s); 100 MHz x 3 s (held 2.2 s), 50 MHz x 10 s (held 3.8 s) and 25 MHz x 15 s (held 10.4 s) "
                "were incomplete. 100 MHz is recorded at 125 MS/s. The datasheet's 25 MHz sustained figure was not "
                "reached on this unit: the IQ is buffered in RAM and the disk takes it at 15-30 MB/s."
            ),
            source=(
                "CRFS datasheet DS-RFEYE-098 (9 kHz-18 GHz, 100 MHz IBW, 125 MS/s, 25 MHz sustained local I/Q, "
                "100 MHz for a few seconds) and this application's hardware QA on the connected unit."
            ),
        )
    if ibw_mhz == 100:
        return SensorCapability(
            model=text,
            family=family,
            min_hz=DATASHEET_MIN_HZ,
            max_hz=max_ghz * GHZ,
            ibw_hz=ibw_mhz * MHZ,
            max_sample_rate_hz=NODE_100_MAX_RATE_HZ,
            sustained_rate_hz=None,
            verified_window_hz=0,
            capture_budget_bytes=None,
            verified_note=(
                f"IBW, tuning range and 125 MS/s sampling read from the datasheet for {family}; no complete-capture "
                "figure has been verified for this model."
            ),
            source="CRFS datasheet (model number).",
        )
    return SensorCapability(
        model=text,
        family=family,
        min_hz=DATASHEET_MIN_HZ,
        max_hz=max_ghz * GHZ,
        ibw_hz=ibw_mhz * MHZ,
        max_sample_rate_hz=NODE_40_MAX_RATE_HZ,
        sustained_rate_hz=None,
        verified_window_hz=0,
        capture_budget_bytes=None,
        verified_note=(
            f"IBW and tuning range read from the model number ({family}); no complete-capture figure has "
            "been verified for this model."
        ),
        source="Model number only.",
    )


def readable_sensor_name(model: str, fallback: str = "") -> str:
    """'R100-18' -> 'CRFS 100-18' for the workbook's Sensor column."""
    text = (model or "").strip()
    match = _MODEL_RE.search(text)
    if not match:
        return text or fallback
    return f"CRFS {int(match.group(1))}-{int(match.group(2))}"


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
    rate = capability.sample_rate_for(window)
    if rate > capability.max_sample_rate_hz:
        notes.append("Capture window exceeds the sensor's maximum sample rate.")
    return window, notes
