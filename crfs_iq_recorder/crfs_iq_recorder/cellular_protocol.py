"""Cellular collection protocol: import the allocation list from an Excel workbook.

The workbook is the source of truth for *what* to record. This module reads
it by column headers, parses frequencies with their units, validates every
row and keeps the imported plan as a versioned snapshot. It never invents
allocations: nothing is widened to a standard band, no nominal name is used
as a centre frequency, and rows that fail validation are reported, not
skipped. Rows that already carry a recording result are results, not
instructions, and are listed as such.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .collection_catalog import HEADER_IQ
from .frequency import UNIT_TO_HZ

SNAPSHOT_SCHEMA = 1

DUPLEX_FDD = "FDD"
DUPLEX_TDD = "TDD"
DUPLEX_UNKNOWN = ""

DIR_DL = "DL"
DIR_UL = "UL"
DIR_SHARED = "TDD / shared UL-DL"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"

DUPLICATES_ONCE = "once"
DUPLICATES_EACH = "each"

# Consecutive blank rows after which the scan of a worksheet stops (a used
# range dragged to the bottom of Excel must not cost a million-row walk).
BLANK_ROW_RUN_LIMIT = 500
HEADER_SEARCH_ROWS = 12

# Column roles and the header texts (normalised) that mean them.
_ALIASES: dict[str, tuple[str, ...]] = {
    "band": (
        "band", "band name", "band number", "band no", "band id", "lte band", "nr band",
        "3gpp band", "frequency band", "band family", "family", "cellular band",
    ),
    "nominal": (
        "nominal frequency", "nominal", "nominal freq", "band frequency", "frequency range name",
        "nominal band", "nominal center", "nominal centre", "frequency", "freq",
    ),
    "duplex": ("duplex", "duplex mode", "fdd/tdd", "fdd tdd", "tdd/fdd", "mode", "duplexing"),
    "direction": (
        "direction", "link", "link direction", "ul/dl", "dl/ul", "uplink/downlink", "downlink/uplink",
        "path", "dir", "side",
    ),
    "start": (
        "start", "start frequency", "start freq", "freq start", "frequency start", "f start", "fstart",
        "from", "low", "lower", "lower edge", "f low", "flow", "fmin", "f min", "min frequency",
        "start frequency mhz", "lower frequency",
    ),
    "stop": (
        "stop", "end", "stop frequency", "end frequency", "stop freq", "end freq", "freq stop", "freq end",
        "frequency stop", "frequency end", "f stop", "f end", "fstop", "fend", "to", "high", "upper",
        "upper edge", "f high", "fhigh", "fmax", "f max", "max frequency", "upper frequency",
    ),
    "unit": ("unit", "units", "frequency unit", "freq unit"),
    "duration": (
        "duration", "recording duration", "record duration", "recording time", "record time",
        "capture duration", "capture time", "capture length", "duration s", "duration sec",
        "duration seconds", "time s", "seconds", "recording length", "length", "iq duration",
        "iq recording duration", "iq time", "iq length",
    ),
    "network": ("network", "operator", "carrier", "mno", "provider", "company", "licensee", "ran", "plmn"),
    "technology": ("technology", "tech", "rat", "system", "generation", "standard", "technologies"),
    "target_class": ("class", "target class", "signal class", "category", "target"),
    "channel_bw": ("channel bandwidth", "channel bw", "carrier bandwidth", "bw", "bandwidth", "ch bw"),
    "notes": ("notes", "note", "comment", "comments", "remarks", "remark", "description"),
    "repeat": ("repeat", "repeats", "repetitions", "repetition", "times", "count"),
    "dl_start": (
        "dl start", "downlink start", "dl low", "dl from", "dl start frequency", "downlink low", "dl lower",
        "dl fstart", "dl f start", "dl min", "dl start freq", "downlink from", "downlink lower",
    ),
    "dl_stop": (
        "dl stop", "dl end", "downlink stop", "downlink end", "dl high", "dl to", "dl upper", "dl max",
        "dl stop frequency", "dl end frequency", "dl fstop", "dl f stop", "dl stop freq", "downlink to",
        "downlink upper",
    ),
    "ul_start": (
        "ul start", "uplink start", "ul low", "ul from", "ul lower", "ul min", "ul start frequency",
        "uplink low", "ul fstart", "ul f start", "ul start freq", "uplink from", "uplink lower",
    ),
    "ul_stop": (
        "ul stop", "ul end", "uplink stop", "uplink end", "ul high", "ul to", "ul upper", "ul max",
        "ul stop frequency", "ul end frequency", "ul fstop", "ul f stop", "ul stop freq", "uplink to",
        "uplink upper",
    ),
    "result": (
        HEADER_IQ.casefold(), "iq recording", "iq file", "recording file", "capture id", "recording id",
        "recorded file", "result", "iq filename",
    ),
    "status": ("status", "state", "collection status", "progress"),
}

_ROLE_ORDER = (
    "band", "nominal", "duplex", "direction", "start", "stop", "unit", "duration", "network",
    "technology", "target_class", "channel_bw", "notes", "repeat",
    "dl_start", "dl_stop", "ul_start", "ul_stop", "result", "status",
)

# Status texts that mean "this row is a finished result", not an instruction.
_DONE_STATUS = {"collected", "done", "recorded", "complete", "completed", "captured", "finished", "ok"}

_UNIT_IN_HEADER = re.compile(r"[\(\[]\s*(hz|khz|mhz|ghz)\s*[\)\]]|\b(khz|mhz|ghz|hz)\b", re.IGNORECASE)
_UNIT_CANON = {name.casefold(): name for name in UNIT_TO_HZ}
_FREQ_CELL = re.compile(r"^\s*([+-]?[\d][\d\s.,]*|[+-]?[.,]\d+)\s*(hz|khz|mhz|ghz)?\s*$", re.IGNORECASE)
_DURATION_CELL = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(ms|msec|milliseconds?|s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hours?)?\s*\.?\s*$",
    re.IGNORECASE,
)
_BAND_NUMBER = re.compile(r"^\s*(?:band|b|n|lte|nr)?\s*[-_ ]?\s*(\d{1,3})\s*$", re.IGNORECASE)

_DIRECTION_WORDS = {
    DIR_DL: {"dl", "downlink", "down link", "down", "d", "forward", "bts tx", "base tx", "tx"},
    DIR_UL: {"ul", "uplink", "up link", "up", "u", "reverse", "bts rx", "base rx", "rx"},
    DIR_SHARED: {
        "shared", "tdd", "ul/dl", "dl/ul", "ul+dl", "dl+ul", "both", "ul dl", "dl ul", "shared ul-dl",
        "tdd / shared ul-dl", "tdd/shared ul-dl", "shared ul/dl", "ul-dl", "dl-ul", "bidirectional",
    },
}


class ProtocolError(ValueError):
    """The workbook or worksheet cannot be read."""


def _normalise_header(text: str) -> tuple[str, str | None]:
    """Lower-case, punctuation-free header text and the unit it names, if any."""
    raw = str(text or "").strip()
    unit = None
    match = _UNIT_IN_HEADER.search(raw)
    if match:
        token = (match.group(1) or match.group(2) or "").casefold()
        unit = _UNIT_CANON.get(token)
    cleaned = _UNIT_IN_HEADER.sub(" ", raw)
    cleaned = re.sub(r"[\(\)\[\]\{\}]", " ", cleaned)
    cleaned = cleaned.replace("_", " ").replace("-", " ").replace(".", " ").replace(":", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().casefold()
    return cleaned, unit


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str
    row: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "row": self.row}

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        return cls(
            level=str(data.get("level") or LEVEL_INFO),
            code=str(data.get("code") or ""),
            message=str(data.get("message") or ""),
            row=int(data["row"]) if data.get("row") is not None else None,
        )


@dataclass(frozen=True)
class ProtocolEntry:
    """One allocation to record, exactly as the workbook describes it."""

    entry_id: str
    row: int
    band: str
    nominal_hz: int | None
    duplex: str
    direction: str
    start_hz: int | None
    stop_hz: int | None
    duration_s: float | None = None
    network: str = ""
    technology: str = ""
    target_class: str = ""
    channel_bw_hz: int | None = None
    notes: str = ""
    repeat: int = 1
    extra: dict[str, str] = field(default_factory=dict)
    issues: tuple[Issue, ...] = ()
    is_result: bool = False
    duplicate_of: int | None = None

    @property
    def ok(self) -> bool:
        return not self.is_result and not any(issue.level == LEVEL_ERROR for issue in self.issues)

    @property
    def bandwidth_hz(self) -> int | None:
        if self.start_hz is None or self.stop_hz is None:
            return None
        return self.stop_hz - self.start_hz

    @property
    def band_key(self) -> str:
        """Groups the DL and UL rows of one band."""
        return f"{self.band.strip().casefold()}|{self.nominal_hz or ''}"

    def band_label(self) -> str:
        """'700 MHz', 'Band 8 (900 MHz)', 'n78 (3500 MHz)' or the band text as written."""
        text = (self.band or "").strip()
        nominal = f"{format_mhz(self.nominal_hz)} MHz" if self.nominal_hz else ""
        if not text:
            return nominal or "band"
        if self.nominal_hz and _FREQ_CELL.match(text):
            number = _decimal_from_number_text(_FREQ_CELL.match(text).group(1))
            if number is not None and (
                format_mhz(self.nominal_hz) == format(number.normalize(), "f") or _FREQ_CELL.match(text).group(2)
            ):
                # The band text is itself the nominal frequency ("700", "3.5 GHz").
                return nominal
        if _BAND_NUMBER.match(text) and not re.search(r"[a-z]", text, re.IGNORECASE):
            text = f"Band {text}"
        if nominal and nominal.casefold() != text.casefold():
            return f"{text} ({nominal})"
        return text

    def label(self) -> str:
        window = ""
        if self.start_hz is not None and self.stop_hz is not None:
            window = f" {format_mhz(self.start_hz)}–{format_mhz(self.stop_hz)} MHz"
        return f"{self.band_label()} {self.direction}{window}".strip()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issues"] = [issue.to_dict() for issue in self.issues]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProtocolEntry":
        issues = tuple(Issue.from_dict(item) for item in data.get("issues") or [])
        return cls(
            entry_id=str(data.get("entry_id") or ""),
            row=int(data.get("row") or 0),
            band=str(data.get("band") or ""),
            nominal_hz=_opt_int(data.get("nominal_hz")),
            duplex=str(data.get("duplex") or ""),
            direction=str(data.get("direction") or ""),
            start_hz=_opt_int(data.get("start_hz")),
            stop_hz=_opt_int(data.get("stop_hz")),
            duration_s=_opt_float(data.get("duration_s")),
            network=str(data.get("network") or ""),
            technology=str(data.get("technology") or ""),
            target_class=str(data.get("target_class") or ""),
            channel_bw_hz=_opt_int(data.get("channel_bw_hz")),
            notes=str(data.get("notes") or ""),
            repeat=max(int(data.get("repeat") or 1), 1),
            extra={str(k): str(v) for k, v in (data.get("extra") or {}).items()},
            issues=issues,
            is_result=bool(data.get("is_result", False)),
            duplicate_of=_opt_int(data.get("duplicate_of")),
        )


def _opt_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_mhz(hz: int) -> str:
    value = Decimal(int(hz)) / Decimal(1_000_000)
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class ColumnMapping:
    """Which worksheet column plays which role. Values are header texts."""

    band: str | None = None
    nominal: str | None = None
    duplex: str | None = None
    direction: str | None = None
    start: str | None = None
    stop: str | None = None
    unit: str | None = None
    duration: str | None = None
    network: str | None = None
    technology: str | None = None
    target_class: str | None = None
    channel_bw: str | None = None
    notes: str | None = None
    repeat: str | None = None
    dl_start: str | None = None
    dl_stop: str | None = None
    ul_start: str | None = None
    ul_stop: str | None = None
    result: str | None = None
    status: str | None = None
    # Unit for bare numbers when neither the cell nor the header names one.
    default_unit: str | None = None
    # Units named in the headers themselves, by role.
    header_units: dict[str, str] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)

    def layout(self) -> str:
        """'rows' (one allocation per row), 'paired' (DL and UL columns) or 'incomplete'."""
        if self.start and self.stop:
            return "rows"
        if (self.dl_start and self.dl_stop) or (self.ul_start and self.ul_stop):
            return "paired"
        return "incomplete"

    def missing(self) -> list[str]:
        """Roles that must be mapped before the sheet can be imported."""
        missing: list[str] = []
        layout = self.layout()
        if layout == "incomplete":
            hint = (
                "start/stop frequency columns (or DL/UL start/stop columns) with the exact allocation edges"
            )
            if self.nominal or self.band:
                hint += (
                    "; a band name or nominal frequency alone does not define an allocation and is never "
                    "used as a centre frequency or widened to the standard band"
                )
            missing.append(hint)
        if not self.band and not self.nominal:
            missing.append("band name or nominal frequency")
        if layout == "rows" and not self.direction and not self.duplex:
            missing.append("direction (DL/UL) or duplex column")
        return missing

    def roles(self) -> dict[str, str]:
        return {role: getattr(self, role) for role in _ROLE_ORDER if getattr(self, role)}

    def to_dict(self) -> dict[str, Any]:
        return {
            **{role: getattr(self, role) for role in _ROLE_ORDER},
            "default_unit": self.default_unit,
            "header_units": dict(self.header_units),
            "extra": list(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ColumnMapping":
        mapping = cls()
        for role in _ROLE_ORDER:
            value = data.get(role)
            setattr(mapping, role, str(value) if value else None)
        unit = data.get("default_unit")
        mapping.default_unit = _UNIT_CANON.get(str(unit).casefold()) if unit else None
        mapping.header_units = {str(k): str(v) for k, v in (data.get("header_units") or {}).items()}
        mapping.extra = [str(item) for item in data.get("extra") or []]
        return mapping


def suggest_mapping(headers: Iterable[str]) -> ColumnMapping:
    """Guess the column roles from the header texts. Unmatched headers become metadata."""
    mapping = ColumnMapping()
    taken: set[str] = set()
    normalised = []
    for header in headers:
        text = str(header or "").strip()
        if not text:
            continue
        key, unit = _normalise_header(text)
        normalised.append((text, key, unit))
    # Paired DL/UL columns first: "DL start" must not be read as a plain "start".
    for role in ("dl_start", "dl_stop", "ul_start", "ul_stop"):
        for text, key, unit in normalised:
            if text in taken:
                continue
            if key in _ALIASES[role]:
                setattr(mapping, role, text)
                taken.add(text)
                if unit:
                    mapping.header_units[role] = unit
                break
    for role in _ROLE_ORDER:
        if role in ("dl_start", "dl_stop", "ul_start", "ul_stop"):
            continue
        for text, key, unit in normalised:
            if text in taken:
                continue
            if key == "band" and unit and role == "nominal" and not mapping.nominal:
                # "Band (MHz)" is the nominal frequency, not a band name.
                mapping.nominal = text
                mapping.header_units["nominal"] = unit
                taken.add(text)
                break
            if key in _ALIASES[role]:
                if role == "band" and unit:
                    continue
                setattr(mapping, role, text)
                taken.add(text)
                if unit:
                    mapping.header_units[role] = unit
                break
    mapping.extra = [text for text, _key, _unit in normalised if text not in taken]
    units = {unit for role, unit in mapping.header_units.items() if role in {
        "start", "stop", "dl_start", "dl_stop", "ul_start", "ul_stop"}}
    if len(units) == 1:
        mapping.default_unit = units.pop()
    return mapping


# ----------------------------------------------------------------------------- cell parsing
def _decimal_from_number_text(text: str) -> Decimal | None:
    raw = text.strip().replace(" ", "").replace("\u00a0", "")
    if not raw:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", raw):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def parse_frequency_cell(value: Any, *, unit_hint: str | None, field_name: str) -> tuple[int | None, Issue | None]:
    """Hz from a cell such as 713, '713 MHz', '0.713GHz', '713,000 kHz' or '1 805.5 MHz'.

    `unit_hint` applies to bare numbers (unit column, header unit or import
    default). A bare number without any hint is an error: units are never
    guessed from the magnitude.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, Issue(LEVEL_ERROR, "missing", f"{field_name} is empty.")
    if isinstance(value, bool):
        return None, Issue(LEVEL_ERROR, "invalid", f"{field_name} is not a number: {value!r}.")
    unit = None
    number: Decimal | None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            number = None
    else:
        match = _FREQ_CELL.match(str(value))
        if not match:
            return None, Issue(LEVEL_ERROR, "invalid", f"{field_name} is not a frequency: {value!r}.")
        number = _decimal_from_number_text(match.group(1))
        if match.group(2):
            unit = _UNIT_CANON[match.group(2).casefold()]
    if number is None:
        return None, Issue(LEVEL_ERROR, "invalid", f"{field_name} is not a number: {value!r}.")
    if unit is None:
        unit = unit_hint
    if unit is None:
        return None, Issue(
            LEVEL_ERROR,
            "no_unit",
            f"{field_name} has no unit ({value!r}) and no unit column or header unit applies. "
            "Add a unit in the workbook or choose one in the column mapping.",
        )
    hz = number * UNIT_TO_HZ[unit]
    if hz <= 0:
        return None, Issue(LEVEL_ERROR, "invalid", f"{field_name} must be positive (got {value!r}).")
    if hz != hz.to_integral_value():
        return None, Issue(
            LEVEL_ERROR,
            "fraction_hz",
            f"{field_name} {value!r} is not a whole number of Hz; the sensor tunes in 1 Hz steps and "
            "the application does not round frequencies.",
        )
    return int(hz), None


def parse_duration_cell(value: Any, *, field_name: str = "Recording duration") -> tuple[float | None, Issue | None]:
    """Seconds from a cell such as 2, '2 s', '30 sec', '1.5 min' or '500 ms'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, Issue(LEVEL_ERROR, "invalid_duration", f"{field_name} is not a number: {value!r}.")
    if isinstance(value, (int, float, Decimal)):
        seconds = float(value)
    else:
        match = _DURATION_CELL.match(str(value))
        if not match:
            return None, Issue(LEVEL_ERROR, "invalid_duration", f"{field_name} is not a duration: {value!r}.")
        number = _decimal_from_number_text(match.group(1))
        if number is None:
            return None, Issue(LEVEL_ERROR, "invalid_duration", f"{field_name} is not a duration: {value!r}.")
        unit = (match.group(2) or "s").casefold()
        scale = 1.0
        if unit.startswith("ms") or unit.startswith("milli"):
            scale = 0.001
        elif unit in {"m", "min", "mins"} or unit.startswith("minute"):
            scale = 60.0
        elif unit in {"h", "hr"} or unit.startswith("hour"):
            scale = 3600.0
        seconds = float(number) * scale
    if seconds != seconds or seconds <= 0:
        return None, Issue(LEVEL_ERROR, "invalid_duration", f"{field_name} must be positive (got {value!r}).")
    return seconds, None


def parse_duplex_cell(value: Any) -> tuple[str, Issue | None]:
    text = str(value or "").strip().casefold()
    if not text:
        return DUPLEX_UNKNOWN, None
    if text in {"fdd", "f", "frequency division duplex", "fdd (paired)", "paired"}:
        return DUPLEX_FDD, None
    if text in {"tdd", "t", "time division duplex", "tdd (unpaired)", "unpaired"}:
        return DUPLEX_TDD, None
    return DUPLEX_UNKNOWN, Issue(LEVEL_WARNING, "duplex_unknown", f"Duplex value {value!r} not recognised (FDD or TDD).")


def parse_direction_cell(value: Any) -> tuple[str, Issue | None]:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "", None
    for direction, words in _DIRECTION_WORDS.items():
        if text in words:
            return direction, None
    return "", Issue(LEVEL_ERROR, "direction_unknown", f"Direction value {value!r} not recognised (DL, UL or shared).")


def parse_band_cell(value: Any, *, unit_hint: str | None = None) -> tuple[str, int | None]:
    """Band text and, when the text itself is a frequency ('700', '3.5 GHz'), its nominal Hz."""
    if value is None:
        return "", None
    if isinstance(value, bool):
        return str(value), None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        text = format(number.normalize(), "f")
        # Bare numbers >= 300 are frequencies in MHz (no 3GPP band number is that
        # high); smaller numbers are band numbers and carry no nominal frequency.
        if number >= 300 and number * UNIT_TO_HZ["MHz"] == (number * UNIT_TO_HZ["MHz"]).to_integral_value():
            return text, int(number * UNIT_TO_HZ["MHz"])
        return text, None
    text = str(value).strip()
    if not text:
        return "", None
    match = _FREQ_CELL.match(text)
    if match:
        number = _decimal_from_number_text(match.group(1))
        unit = _UNIT_CANON.get((match.group(2) or "").casefold()) if match.group(2) else None
        if number is not None:
            if unit is None and number >= 300:
                unit = unit_hint or "MHz"
            if unit is not None:
                hz = number * UNIT_TO_HZ[unit]
                if hz == hz.to_integral_value() and hz > 0:
                    return text, int(hz)
    return text, None


def parse_repeat_cell(value: Any) -> tuple[int, Issue | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1, None
    try:
        count = int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        return 1, Issue(LEVEL_WARNING, "repeat_invalid", f"Repeat value {value!r} ignored; recording once.")
    if count < 1:
        return 1, Issue(LEVEL_WARNING, "repeat_invalid", f"Repeat value {value!r} ignored; recording once.")
    return count, None


# ----------------------------------------------------------------------------- import
@dataclass
class ProtocolImport:
    """The result of reading one worksheet: entries, issues and provenance."""

    source: str
    worksheet: str
    mapping: ColumnMapping
    headers: list[str]
    header_row: int
    entries: list[ProtocolEntry]
    issues: list[Issue]
    imported_at: datetime
    workbook_sha256: str
    rows_scanned: int
    blank_rows: int
    duplicate_policy: str = DUPLICATES_ONCE
    schema: int = SNAPSHOT_SCHEMA

    @property
    def errors(self) -> list[Issue]:
        found = [issue for issue in self.issues if issue.level == LEVEL_ERROR]
        for entry in self.entries:
            found.extend(issue for issue in entry.issues if issue.level == LEVEL_ERROR)
        return found

    @property
    def warnings(self) -> list[Issue]:
        found = [issue for issue in self.issues if issue.level == LEVEL_WARNING]
        for entry in self.entries:
            found.extend(issue for issue in entry.issues if issue.level == LEVEL_WARNING)
        return found

    def valid_entries(self) -> list[ProtocolEntry]:
        """Entries that can be recorded (no errors, not results, not collapsed duplicates)."""
        return [
            entry for entry in self.entries
            if entry.ok and not (entry.duplicate_of is not None and self.duplicate_policy == DUPLICATES_ONCE)
        ]

    def result_rows(self) -> list[ProtocolEntry]:
        return [entry for entry in self.entries if entry.is_result]

    def invalid_entries(self) -> list[ProtocolEntry]:
        return [entry for entry in self.entries if not entry.ok and not entry.is_result]

    @property
    def ready(self) -> bool:
        return bool(self.valid_entries()) and not self.errors

    def protocol_version(self) -> str:
        """Short, stable identifier of this exact plan (file, sheet, mapping and rows)."""
        body = json.dumps(
            {
                "sha": self.workbook_sha256,
                "sheet": self.worksheet,
                "mapping": self.mapping.to_dict(),
                "policy": self.duplicate_policy,
                "entries": [
                    {k: v for k, v in entry.to_dict().items() if k not in {"issues"}}
                    for entry in self.entries
                ],
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "protocol_version": self.protocol_version(),
            "source": self.source,
            "worksheet": self.worksheet,
            "mapping": self.mapping.to_dict(),
            "headers": list(self.headers),
            "header_row": self.header_row,
            "imported_at": self.imported_at.isoformat(),
            "workbook_sha256": self.workbook_sha256,
            "rows_scanned": self.rows_scanned,
            "blank_rows": self.blank_rows,
            "duplicate_policy": self.duplicate_policy,
            "issues": [issue.to_dict() for issue in self.issues],
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProtocolImport":
        imported = data.get("imported_at")
        try:
            imported_at = datetime.fromisoformat(str(imported)) if imported else datetime.now()
        except ValueError:
            imported_at = datetime.now()
        policy = str(data.get("duplicate_policy") or DUPLICATES_ONCE)
        return cls(
            source=str(data.get("source") or ""),
            worksheet=str(data.get("worksheet") or ""),
            mapping=ColumnMapping.from_dict(data.get("mapping") or {}),
            headers=[str(item) for item in data.get("headers") or []],
            header_row=int(data.get("header_row") or 1),
            entries=[ProtocolEntry.from_dict(item) for item in data.get("entries") or []],
            issues=[Issue.from_dict(item) for item in data.get("issues") or []],
            imported_at=imported_at,
            workbook_sha256=str(data.get("workbook_sha256") or ""),
            rows_scanned=int(data.get("rows_scanned") or 0),
            blank_rows=int(data.get("blank_rows") or 0),
            duplicate_policy=policy if policy in {DUPLICATES_ONCE, DUPLICATES_EACH} else DUPLICATES_ONCE,
            schema=int(data.get("schema") or SNAPSHOT_SCHEMA),
        )


def _load_workbook(path: Path):
    from openpyxl import load_workbook

    try:
        return load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    except FileNotFoundError as exc:
        raise ProtocolError(f"Workbook not found: {path}") from exc
    except PermissionError as exc:
        raise ProtocolError(f"Could not open {path.name}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for bad files
        raise ProtocolError(f"Could not read {path.name}: {exc}") from exc


def workbook_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def list_worksheets(path: str | Path) -> list[tuple[str, bool]]:
    """(name, visible) for every worksheet in the workbook."""
    workbook = _load_workbook(Path(path))
    try:
        out = []
        for ws in workbook.worksheets:
            out.append((ws.title, getattr(ws, "sheet_state", "visible") == "visible"))
        return out
    finally:
        workbook.close()


def _row_values(ws, max_rows: int) -> list[tuple[int, list[Any]]]:
    rows: list[tuple[int, list[Any]]] = []
    blank_run = 0
    for index, row in enumerate(ws.iter_rows(min_row=1, max_row=max_rows, values_only=True), start=1):
        values = list(row)
        if all(value is None or (isinstance(value, str) and not value.strip()) for value in values):
            blank_run += 1
            if blank_run >= BLANK_ROW_RUN_LIMIT:
                break
            rows.append((index, values))
            continue
        blank_run = 0
        rows.append((index, values))
    return rows


def _find_header_row(rows: list[tuple[int, list[Any]]]) -> int:
    """First of the top rows whose texts match at least two known column roles."""
    known = {alias for aliases in _ALIASES.values() for alias in aliases}
    best_row, best_hits = 1, -1
    for index, values in rows[:HEADER_SEARCH_ROWS]:
        hits = 0
        for value in values:
            if value is None:
                continue
            key, _unit = _normalise_header(str(value))
            if key in known:
                hits += 1
        if hits > best_hits:
            best_row, best_hits = index, hits
        if hits >= 2:
            return index
    return best_row if best_hits > 0 else 1


def read_headers(path: str | Path, worksheet: str) -> tuple[list[str], int]:
    """Header texts of a worksheet and the row they were found in."""
    workbook = _load_workbook(Path(path))
    try:
        if worksheet not in workbook.sheetnames:
            raise ProtocolError(f"Worksheet {worksheet!r} is not in {Path(path).name}.")
        ws = workbook[worksheet]
        rows = _row_values(ws, HEADER_SEARCH_ROWS)
        header_row = _find_header_row(rows)
        values = next((vals for idx, vals in rows if idx == header_row), [])
        return [str(value).strip() if value is not None else "" for value in values], header_row
    finally:
        workbook.close()


def _cell(values: list[Any], columns: dict[str, int], header: str | None) -> Any:
    if not header:
        return None
    index = columns.get(header)
    if index is None or index >= len(values):
        return None
    value = values[index]
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_protocol(
    path: str | Path,
    worksheet: str,
    mapping: ColumnMapping | None = None,
    *,
    duplicate_policy: str = DUPLICATES_ONCE,
) -> ProtocolImport:
    """Read every allocation row of `worksheet`. Never raises for bad rows: see `issues`."""
    file = Path(path)
    sha = workbook_sha256(file)
    workbook = _load_workbook(file)
    try:
        if worksheet not in workbook.sheetnames:
            raise ProtocolError(f"Worksheet {worksheet!r} is not in {file.name}.")
        ws = workbook[worksheet]
        hidden = getattr(ws, "sheet_state", "visible") != "visible"
        rows = _row_values(ws, max(ws.max_row or 1, 1))
    finally:
        workbook.close()
    header_row = _find_header_row(rows)
    header_values = next((vals for idx, vals in rows if idx == header_row), [])
    headers = [str(value).strip() if value is not None else "" for value in header_values]
    if mapping is None:
        mapping = suggest_mapping(headers)
    columns: dict[str, int] = {}
    for index, header in enumerate(headers):
        if header and header not in columns:
            columns[header] = index
    issues: list[Issue] = []
    if hidden:
        issues.append(Issue(LEVEL_WARNING, "hidden_sheet", f"Worksheet {worksheet!r} is hidden in the workbook."))
    for role, header in mapping.roles().items():
        if header not in columns:
            issues.append(Issue(LEVEL_ERROR, "column_missing", f"Column {header!r} (for {role.replace('_', ' ')}) is not in the worksheet."))
    missing = mapping.missing()
    for item in missing:
        issues.append(Issue(LEVEL_ERROR, "mapping_incomplete", f"Column mapping is incomplete: {item}."))
    entries: list[ProtocolEntry] = []
    blank = 0
    scanned = 0
    if not any(issue.level == LEVEL_ERROR for issue in issues):
        layout = mapping.layout()
        for index, values in rows:
            if index <= header_row:
                continue
            scanned += 1
            if all(value is None or (isinstance(value, str) and not value.strip()) for value in values):
                blank += 1
                continue
            entries.extend(_entries_from_row(worksheet, index, values, columns, mapping, layout))
    _flag_duplicates(entries)
    return ProtocolImport(
        source=str(file),
        worksheet=worksheet,
        mapping=mapping,
        headers=headers,
        header_row=header_row,
        entries=entries,
        issues=issues,
        imported_at=datetime.now(),
        workbook_sha256=sha,
        rows_scanned=scanned,
        blank_rows=blank,
        duplicate_policy=duplicate_policy if duplicate_policy in {DUPLICATES_ONCE, DUPLICATES_EACH} else DUPLICATES_ONCE,
    )


def _unit_hint(mapping: ColumnMapping, role: str, values: list[Any], columns: dict[str, int]) -> str | None:
    unit_cell = _cell(values, columns, mapping.unit)
    if unit_cell is not None:
        unit = _UNIT_CANON.get(str(unit_cell).strip().casefold())
        if unit:
            return unit
    header_unit = mapping.header_units.get(role)
    if header_unit:
        return header_unit
    return mapping.default_unit


def _entries_from_row(
    worksheet: str,
    row: int,
    values: list[Any],
    columns: dict[str, int],
    mapping: ColumnMapping,
    layout: str,
) -> list[ProtocolEntry]:
    issues: list[Issue] = []
    band_text, nominal_from_band = parse_band_cell(
        _cell(values, columns, mapping.band),
        unit_hint=mapping.header_units.get("band") or mapping.header_units.get("nominal"),
    )
    nominal_hz = None
    if mapping.nominal:
        nominal_cell = _cell(values, columns, mapping.nominal)
        if nominal_cell is not None:
            hint = mapping.header_units.get("nominal") or mapping.default_unit or "MHz"
            nominal_hz, issue = parse_frequency_cell(nominal_cell, unit_hint=hint, field_name="Nominal frequency")
            if issue is not None:
                issues.append(replace(issue, level=LEVEL_WARNING, row=row))
                nominal_hz = None
    if nominal_hz is None:
        nominal_hz = nominal_from_band
    if not band_text and nominal_hz is not None:
        band_text = f"{format_mhz(nominal_hz)} MHz"
    if not band_text and nominal_hz is None:
        issues.append(Issue(LEVEL_ERROR, "band_missing", "No band name or nominal frequency.", row=row))
    duplex, duplex_issue = parse_duplex_cell(_cell(values, columns, mapping.duplex))
    if duplex_issue is not None:
        issues.append(replace(duplex_issue, row=row))
    duration_s, duration_issue = parse_duration_cell(_cell(values, columns, mapping.duration))
    if duration_issue is not None:
        issues.append(replace(duration_issue, row=row))
    repeat, repeat_issue = parse_repeat_cell(_cell(values, columns, mapping.repeat))
    if repeat_issue is not None:
        issues.append(replace(repeat_issue, row=row))
    channel_bw_hz = None
    channel_cell = _cell(values, columns, mapping.channel_bw)
    if channel_cell is not None:
        channel_bw_hz, bw_issue = parse_frequency_cell(
            channel_cell,
            unit_hint=mapping.header_units.get("channel_bw") or mapping.default_unit or "MHz",
            field_name="Channel bandwidth",
        )
        if bw_issue is not None:
            issues.append(replace(bw_issue, level=LEVEL_WARNING, row=row))
            channel_bw_hz = None
    result_value = _cell(values, columns, mapping.result)
    status_value = _text(_cell(values, columns, mapping.status))
    is_result = result_value is not None or status_value.casefold() in _DONE_STATUS
    if is_result:
        what = _text(result_value)[:40] if result_value is not None else f"status {status_value!r}"
        issues.append(Issue(
            LEVEL_INFO,
            "result_row",
            f"Row {row} already holds a recording result ({what}); results are never re-imported as instructions.",
            row=row,
        ))
    common = dict(
        row=row,
        band=band_text,
        nominal_hz=nominal_hz,
        duration_s=duration_s,
        network=_text(_cell(values, columns, mapping.network)),
        technology=_text(_cell(values, columns, mapping.technology)),
        target_class=_text(_cell(values, columns, mapping.target_class)),
        channel_bw_hz=channel_bw_hz,
        notes=_text(_cell(values, columns, mapping.notes)),
        repeat=repeat,
        extra={header: _text(_cell(values, columns, header)) for header in mapping.extra if _cell(values, columns, header) is not None},
        is_result=is_result,
    )

    def make(direction: str, start: int | None, stop: int | None, extra_issues: list[Issue], duplex_value: str) -> ProtocolEntry:
        all_issues = list(issues) + list(extra_issues)
        if start is not None and stop is not None:
            if start >= stop:
                all_issues.append(Issue(
                    LEVEL_ERROR, "reversed",
                    f"Start {format_mhz(start)} MHz is not below stop {format_mhz(stop)} MHz.", row=row,
                ))
            elif (stop - start) < 1_000:
                all_issues.append(Issue(LEVEL_WARNING, "narrow", f"Allocation is only {stop - start} Hz wide.", row=row))
        return ProtocolEntry(
            entry_id=f"{worksheet}!R{row}:{direction or 'none'}",
            direction=direction,
            duplex=duplex_value,
            start_hz=start,
            stop_hz=stop,
            issues=tuple(all_issues),
            **common,
        )

    if layout == "rows":
        start_hz, start_issue = parse_frequency_cell(
            _cell(values, columns, mapping.start),
            unit_hint=_unit_hint(mapping, "start", values, columns),
            field_name="Start frequency",
        )
        stop_hz, stop_issue = parse_frequency_cell(
            _cell(values, columns, mapping.stop),
            unit_hint=_unit_hint(mapping, "stop", values, columns),
            field_name="Stop frequency",
        )
        extra = [replace(issue, row=row) for issue in (start_issue, stop_issue) if issue is not None]
        direction, direction_issue = parse_direction_cell(_cell(values, columns, mapping.direction))
        if direction_issue is not None:
            extra.append(replace(direction_issue, row=row))
        direction, duplex_value, more = _reconcile(direction, duplex, row)
        extra.extend(more)
        return [make(direction, start_hz, stop_hz, extra, duplex_value)]

    # Paired layout: DL and UL columns on one row.
    dl_present = _cell(values, columns, mapping.dl_start) is not None or _cell(values, columns, mapping.dl_stop) is not None
    ul_present = _cell(values, columns, mapping.ul_start) is not None or _cell(values, columns, mapping.ul_stop) is not None
    out: list[ProtocolEntry] = []
    ranges: dict[str, tuple[int | None, int | None, list[Issue]]] = {}
    for side, start_role, stop_role in ((DIR_DL, "dl_start", "dl_stop"), (DIR_UL, "ul_start", "ul_stop")):
        present = dl_present if side == DIR_DL else ul_present
        if not present:
            continue
        start_hz, start_issue = parse_frequency_cell(
            _cell(values, columns, getattr(mapping, start_role)),
            unit_hint=_unit_hint(mapping, start_role, values, columns),
            field_name=f"{side} start frequency",
        )
        stop_hz, stop_issue = parse_frequency_cell(
            _cell(values, columns, getattr(mapping, stop_role)),
            unit_hint=_unit_hint(mapping, stop_role, values, columns),
            field_name=f"{side} stop frequency",
        )
        ranges[side] = (start_hz, stop_hz, [replace(i, row=row) for i in (start_issue, stop_issue) if i is not None])
    if not ranges:
        return [make("", None, None, [Issue(LEVEL_ERROR, "missing", "No DL or UL frequencies on this row.", row=row)], duplex)]
    if duplex == DUPLEX_TDD:
        if len(ranges) == 2 and ranges[DIR_DL][:2] != ranges[DIR_UL][:2]:
            extra = ranges[DIR_DL][2] + ranges[DIR_UL][2] + [Issue(
                LEVEL_ERROR, "tdd_two_ranges",
                "TDD row lists different DL and UL ranges; a TDD band uses one shared allocation.", row=row,
            )]
            return [make(DIR_SHARED, ranges[DIR_DL][0], ranges[DIR_DL][1], extra, DUPLEX_TDD)]
        side = DIR_DL if DIR_DL in ranges else DIR_UL
        start_hz, stop_hz, extra = ranges[side]
        extra = list(extra) + [Issue(LEVEL_INFO, "tdd_shared", "TDD: recorded once as a shared UL/DL allocation.", row=row)]
        return [make(DIR_SHARED, start_hz, stop_hz, extra, DUPLEX_TDD)]
    duplex_value = duplex or DUPLEX_FDD
    inferred: list[Issue] = []
    if not duplex:
        inferred.append(Issue(LEVEL_INFO, "duplex_inferred", "Duplex not given; paired DL/UL columns read as FDD.", row=row))
    if len(ranges) == 1:
        inferred.append(Issue(LEVEL_WARNING, "one_side_only", f"Only the {next(iter(ranges))} range is given for this FDD row.", row=row))
    for side in (DIR_DL, DIR_UL):
        if side in ranges:
            start_hz, stop_hz, extra = ranges[side]
            out.append(make(side, start_hz, stop_hz, list(extra) + inferred, duplex_value))
    return out


def _reconcile(direction: str, duplex: str, row: int) -> tuple[str, str, list[Issue]]:
    """Direction and duplex must agree; fill one from the other when only one is given."""
    issues: list[Issue] = []
    if duplex == DUPLEX_TDD:
        if direction in {DIR_DL, DIR_UL}:
            issues.append(Issue(
                LEVEL_ERROR, "tdd_direction",
                f"TDD allocation marked {direction}; TDD uses one shared UL/DL allocation.", row=row,
            ))
            return direction, duplex, issues
        if not direction:
            issues.append(Issue(LEVEL_INFO, "tdd_shared", "TDD: recorded once as a shared UL/DL allocation.", row=row))
        return DIR_SHARED, DUPLEX_TDD, issues
    if duplex == DUPLEX_FDD:
        if direction == DIR_SHARED:
            issues.append(Issue(LEVEL_ERROR, "fdd_shared", "FDD allocation marked shared; FDD rows need DL or UL.", row=row))
            return direction, duplex, issues
        if not direction:
            issues.append(Issue(LEVEL_ERROR, "direction_missing", "FDD row without a DL/UL direction.", row=row))
        return direction, duplex, issues
    # Duplex unknown: infer from the direction.
    if direction == DIR_SHARED:
        issues.append(Issue(LEVEL_INFO, "duplex_inferred", "Duplex not given; shared UL/DL read as TDD.", row=row))
        return direction, DUPLEX_TDD, issues
    if direction in {DIR_DL, DIR_UL}:
        issues.append(Issue(LEVEL_INFO, "duplex_inferred", f"Duplex not given; {direction} row read as FDD.", row=row))
        return direction, DUPLEX_FDD, issues
    if not any(issue.code == "direction_unknown" for issue in issues):
        issues.append(Issue(LEVEL_ERROR, "direction_missing", "No direction (DL/UL/shared) and no duplex on this row.", row=row))
    return direction, DUPLEX_UNKNOWN, issues


def _flag_duplicates(entries: list[ProtocolEntry]) -> None:
    """Mark exact duplicates and overlapping allocations of the same direction."""
    seen: dict[tuple[str, int, int], int] = {}
    ordered = [entry for entry in entries if entry.ok and entry.start_hz is not None and entry.stop_hz is not None]
    for index, entry in enumerate(entries):
        if not entry.ok or entry.start_hz is None or entry.stop_hz is None:
            continue
        key = (entry.direction, entry.start_hz, entry.stop_hz)
        extra: list[Issue] = []
        duplicate_of = None
        if key in seen:
            duplicate_of = seen[key]
            extra.append(Issue(
                LEVEL_WARNING, "duplicate",
                f"Same {entry.direction} range as row {duplicate_of}. Treated as a duplicated template row and "
                "recorded once unless the import option says to record each; use a Repeat column for "
                "intentional repetitions.",
                row=entry.row,
            ))
        else:
            seen[key] = entry.row
            for other in ordered:
                if other is entry or other.direction != entry.direction or other.row >= entry.row:
                    continue
                if other.start_hz < entry.stop_hz and entry.start_hz < other.stop_hz and (other.start_hz, other.stop_hz) != key:
                    extra.append(Issue(
                        LEVEL_WARNING, "overlap",
                        f"Overlaps the {other.direction} range of row {other.row} "
                        f"({format_mhz(other.start_hz)}–{format_mhz(other.stop_hz)} MHz). Both are recorded as listed.",
                        row=entry.row,
                    ))
        if extra or duplicate_of is not None:
            entries[index] = replace(entry, issues=entry.issues + tuple(extra), duplicate_of=duplicate_of)


# ----------------------------------------------------------------------------- snapshots
def snapshot_folder(config_dir: Path) -> Path:
    folder = Path(config_dir) / "cellular" / "protocols"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_snapshot(imported: ProtocolImport, config_dir: Path) -> Path:
    """Write the imported plan as a versioned JSON snapshot; returns its path."""
    from .json_store import write_json

    version = imported.protocol_version()
    stamp = imported.imported_at.strftime("%Y%m%d_%H%M%S")
    path = snapshot_folder(config_dir) / f"protocol_{stamp}_{version}.json"
    write_json(path, imported.to_dict(), backup=False)
    return path


def load_snapshot(path: str | Path) -> ProtocolImport:
    from .json_store import read_json

    data = read_json(Path(path), backup=False)
    if not isinstance(data, dict):
        raise ProtocolError(f"Snapshot {Path(path).name} is not readable.")
    return ProtocolImport.from_dict(data)
