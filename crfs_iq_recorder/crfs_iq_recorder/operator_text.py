"""Small formatting helpers for the short messages shown to the operator."""

from __future__ import annotations

from decimal import Decimal


def mhz_text(hz: float | int | None) -> str:
    """'3300', '1805.5', '0.009' (MHz without trailing zeros)."""
    if hz is None:
        return "?"
    value = Decimal(str(hz)) / Decimal(1_000_000)
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def range_text(start_hz: float | int, stop_hz: float | int) -> str:
    """'3300-3400 MHz'."""
    return f"{mhz_text(start_hz)}-{mhz_text(stop_hz)} MHz"


def seconds_text(seconds: float | None) -> str:
    """'2 s', '1.5 s', '0.25 s'."""
    if seconds is None:
        return "unknown"
    return f"{float(seconds):g} s"


def measured_text(seconds: float | None) -> str:
    """Measured seconds to the hundredth: '1.98 s'; 'unknown' when not read."""
    if seconds is None:
        return "unknown"
    return f"{float(seconds):.2f} s"


def files_text(count: int) -> str:
    return f"{int(count)} file" + ("" if int(count) == 1 else "s")


def size_text(count: int | float | None) -> str:
    """Bytes in decimal units like the size estimate (1 MB = 1,000,000 bytes): '125.7 MB', '2.51 GB'."""
    value = float(max(int(count or 0), 0))
    if value < 1000:
        return f"{int(value)} B"
    for unit, scale, places in (("kB", 1e3, 1), ("MB", 1e6, 1), ("GB", 1e9, 2), ("TB", 1e12, 2)):
        if value < scale * 1000 or unit == "TB":
            text = f"{value / scale:.{places}f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{int(value)} B"


def size_detail(count: int | float | None) -> str:
    """'125,706,816 bytes (125.7 MB, 119.9 MiB)' for tooltips: both conventions, nothing to guess."""
    value = max(int(count or 0), 0)
    mib = value / (1024 * 1024)
    return f"{value:,} bytes ({size_text(value)}, {mib:,.1f} MiB)"


def requested_text(bandwidth_hz: float | int, seconds: float) -> str:
    """'100 MHz for 2 s'."""
    return f"{mhz_text(bandwidth_hz)} MHz for {seconds_text(seconds)}"


def clip(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def sensor_notice(text: str) -> str:
    """The once-per-session Activity line for a sensor WARNING message."""
    lowered = (text or "").casefold()
    if "internal clock" in lowered:
        # Node 100-18: its ncpd.conf sets the real-time and reference clocks to
        # "internal"; repeated ~40 times a second, the recording is complete.
        return "CRFS warning: the sensor uses its internal clock, not GPS. Shown once."
    return f"CRFS warning: {clip(text)}"
