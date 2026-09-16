"""Frequency conversion and start/center/end relationships.

Values are handled in exact Decimal Hz. The EMP example uses integer Hz, and
the Node 100-18 datasheet lists 1 Hz tuning resolution. Fractional Hz is
rejected rather than rounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

UNIT_TO_HZ: Mapping[str, Decimal] = {
    "Hz": Decimal(1),
    "kHz": Decimal(1_000),
    "MHz": Decimal(1_000_000),
    "GHz": Decimal(1_000_000_000),
}


class FrequencyError(ValueError):
    """Invalid frequency, unit, or relationship."""


def parse_decimal(text: str, *, field: str) -> Decimal:
    raw = (text or "").strip().replace(",", "")
    if raw == "":
        raise FrequencyError(f"{field} is empty.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise FrequencyError(f"{field} is not a valid number: {text!r}.") from exc
    if not value.is_finite():
        raise FrequencyError(f"{field} must be a finite number.")
    return value


def to_hz(value: Decimal, unit: str, *, field: str) -> Decimal:
    try:
        scale = UNIT_TO_HZ[unit]
    except KeyError as exc:
        raise FrequencyError(f"Unsupported frequency unit: {unit!r}.") from exc
    return value * scale


def require_integer_hz(hz: Decimal, *, field: str) -> int:
    """Reject values that are not an integer number of Hz (no silent rounding)."""
    if hz < 0:
        raise FrequencyError(f"{field} must be ≥ 0 Hz (got {hz} Hz).")
    integral = hz.to_integral_value()
    if hz != integral:
        raise FrequencyError(
            f"{field} is {hz} Hz, which is not an integer number of Hz. "
            "This application does not round frequency or bandwidth. "
            "Enter a value that converts to a whole number of Hz "
            "(Node 100-18 datasheet tuning resolution: 1 Hz)."
        )
    return int(integral)


def from_hz(hz: int | Decimal, unit: str) -> Decimal:
    try:
        scale = UNIT_TO_HZ[unit]
    except KeyError as exc:
        raise FrequencyError(f"Unsupported frequency unit: {unit!r}.") from exc
    return Decimal(hz) / scale


def format_decimal(value: Decimal) -> str:
    """Plain decimal string without scientific notation or extra zeros."""
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def display_from_hz(hz: int, unit: str) -> str:
    return format_decimal(from_hz(hz, unit))


def parse_to_integer_hz(text: str, unit: str, *, field: str) -> int:
    return require_integer_hz(to_hz(parse_decimal(text, field=field), unit, field=field), field=field)


_FREQ_WITH_UNIT = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(Hz|kHz|MHz|GHz)\s*$",
    re.IGNORECASE,
)

_UNIT_CANON = {name.casefold(): name for name in UNIT_TO_HZ}


@dataclass(frozen=True)
class FreqTextStyle:
    unit: str
    space_before_unit: bool


def parse_freq_text(text: str, *, field: str = "Frequency") -> int:
    """Parse values such as `1626MHz`, `1626.5 MHz`, or `2.9 GHz` to integer Hz."""
    raw = (text or "").strip().replace(",", "")
    match = _FREQ_WITH_UNIT.match(raw)
    if not match:
        raise FrequencyError(f"{field} is not a frequency with a unit: {text!r}.")
    unit = _UNIT_CANON[match.group(2).casefold()]
    return parse_to_integer_hz(match.group(1), unit, field=field)


def freq_text_style(text: str) -> FreqTextStyle | None:
    raw = (text or "").strip().replace(",", "")
    match = _FREQ_WITH_UNIT.match(raw)
    if not match:
        return None
    unit = _UNIT_CANON[match.group(2).casefold()]
    between = raw[match.start(1) + len(match.group(1)) : match.start(2)]
    return FreqTextStyle(unit=unit, space_before_unit=(" " in between))


def format_freq_text(hz: int, style: FreqTextStyle) -> str:
    number = display_from_hz(int(hz), style.unit)
    gap = " " if style.space_before_unit else ""
    return f"{number}{gap}{style.unit}"


@dataclass(frozen=True)
class FrequencyPlan:
    start_hz: int
    end_hz: int
    center_hz: int
    bandwidth_hz: int


def from_start_end(start_hz: int, end_hz: int) -> FrequencyPlan:
    if start_hz >= end_hz:
        raise FrequencyError(
            f"Start frequency ({start_hz} Hz) must be below end frequency ({end_hz} Hz)."
        )
    bandwidth = end_hz - start_hz
    if bandwidth <= 0:
        raise FrequencyError("Bandwidth must be positive.")
    total = start_hz + end_hz
    if total % 2:
        raise FrequencyError(
            f"Center frequency would be {total / 2} Hz, which is not an integer number of Hz. "
            "Adjust start/end so (start + end) is even, or use center/bandwidth mode."
        )
    center = total // 2
    return FrequencyPlan(
        start_hz=start_hz,
        end_hz=end_hz,
        center_hz=center,
        bandwidth_hz=bandwidth,
    )


def from_center_bandwidth(center_hz: int, bandwidth_hz: int) -> FrequencyPlan:
    if bandwidth_hz <= 0:
        raise FrequencyError("Bandwidth must be positive.")
    if bandwidth_hz % 2:
        raise FrequencyError(
            f"Bandwidth {bandwidth_hz} Hz is odd, so start/end would not be integer Hz. "
            "Enter an even bandwidth in Hz, or switch units so the converted Hz value is even."
        )
    half = bandwidth_hz // 2
    start = center_hz - half
    end = center_hz + half
    if start < 0:
        raise FrequencyError(
            f"Start frequency would be {start} Hz. Center and bandwidth must yield start ≥ 0 Hz."
        )
    if start >= end:
        raise FrequencyError("Start frequency must be below end frequency.")
    return FrequencyPlan(
        start_hz=start,
        end_hz=end,
        center_hz=center_hz,
        bandwidth_hz=bandwidth_hz,
    )
