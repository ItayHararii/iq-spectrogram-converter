"""What a recording actually holds, read from the sensor's own files.

The WAVE header gives the number of samples and the sample rate (so the
seconds recorded); the CRFS `meta` trailer gives the start time, centre
frequency, bandwidth and sample rate the sensor used. An XDAT part is read
the same way from the `.xhdr` the sensor writes beside it (see
`xdat_header`). Nothing here is estimated from file sizes or from the time
that elapsed: a value that cannot be read stays unknown.

Result classes:

* completed  - every file was read and the files hold at least the requested
  duration less `PARTIAL_TOLERANCE`;
* partial    - files were read but hold less than that, a file the sensor
  named is missing or cut short, or the sensor reported an error after it had
  started writing;
* unconfirmed - files were found but their duration could not be read (e.g.
  a format other than WAVE), or no file could be confirmed for a request the
  sensor may have run;
* failed     - the request was not sent or the sensor rejected it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

RESULT_COMPLETED = "completed"
RESULT_PARTIAL = "partial"
RESULT_UNCONFIRMED = "unconfirmed"
RESULT_FAILED = "failed"

# A recording is complete when its files hold at least 98 % of the requested
# seconds. The Node writes slightly more than requested (1.0056 s for 1 s on a
# Node 100-18), so a complete recording clears this easily.
PARTIAL_TOLERANCE = 0.02

# Headers are read again when the sensor has not finished them yet.
HEADER_RETRIES = 4
HEADER_RETRY_S = 3.0


@dataclass
class PartMeasurement:
    path: str
    size: int = 0
    duration_s: float | None = None
    sample_rate_hz: float | None = None
    centre_hz: float | None = None
    bandwidth_hz: float | None = None
    signal_time: datetime | None = None
    truncated: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def readable(self) -> bool:
        return self.duration_s is not None

    @property
    def final(self) -> bool:
        """Header and metadata look finished (not still being written by the sensor)."""
        return self.readable and self.signal_time is not None and not any("still being written" in p for p in self.problems)

    @property
    def header_pending(self) -> bool:
        """The sensor has not finished the header yet (worth reading again shortly)."""
        return (self.readable and not self.final) or any("still being written" in p for p in self.problems)

    @property
    def being_written(self) -> bool:
        """The header does not describe the file yet, so the sensor is still writing it.

        While a Node writes a WAVE part, its RIFF and data sizes hold a
        placeholder (ASCII "size") and the CRFS metadata trailer is missing; a
        .xdat part has no .xhdr yet. A finished file without metadata is not
        counted: its sizes match the file.
        """
        if not self.header_pending:
            return False
        return self.truncated or any(
            "still being written" in problem
            or problem.startswith("RIFF size says")
            or "trailer chunk is cut short" in problem
            for problem in self.problems
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "duration_s": self.duration_s,
            "sample_rate_hz": self.sample_rate_hz,
            "centre_hz": self.centre_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "signal_time": self.signal_time.isoformat() if self.signal_time else None,
            "truncated": self.truncated,
            "problems": list(self.problems),
        }


@dataclass
class CaptureMeasurement:
    parts: list[PartMeasurement] = field(default_factory=list)

    @property
    def all_readable(self) -> bool:
        return bool(self.parts) and all(part.readable for part in self.parts)

    @property
    def duration_s(self) -> float | None:
        """Seconds of IQ in all parts together; None when any part could not be read."""
        if not self.all_readable:
            return None
        return sum(float(part.duration_s or 0.0) for part in self.parts)

    def _agreed(self, attr: str) -> float | None:
        values = {getattr(part, attr) for part in self.parts}
        if len(values) == 1:
            value = next(iter(values))
            return float(value) if value is not None else None
        return None

    @property
    def bandwidth_hz(self) -> float | None:
        return self._agreed("bandwidth_hz")

    @property
    def centre_hz(self) -> float | None:
        return self._agreed("centre_hz")

    @property
    def sample_rate_hz(self) -> float | None:
        return self._agreed("sample_rate_hz")

    @property
    def signal_time(self) -> datetime | None:
        times = [part.signal_time for part in self.parts if part.signal_time is not None]
        return min(times) if times else None

    @property
    def truncated(self) -> bool:
        return any(part.truncated for part in self.parts)

    def problems(self) -> list[str]:
        out: list[str] = []
        for part in self.parts:
            out.extend(f"{part.name}: {problem}" for problem in part.problems)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "bandwidth_hz": self.bandwidth_hz,
            "centre_hz": self.centre_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "signal_time": self.signal_time.isoformat() if self.signal_time else None,
            "parts": [part.to_dict() for part in self.parts],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "CaptureMeasurement":
        out = cls()
        for item in (data or {}).get("parts") or []:
            when = item.get("signal_time")
            try:
                stamp = datetime.fromisoformat(str(when)) if when else None
            except ValueError:
                stamp = None
            out.parts.append(PartMeasurement(
                path=str(item.get("path") or ""),
                size=int(item.get("size") or 0),
                duration_s=_opt_float(item.get("duration_s")),
                sample_rate_hz=_opt_float(item.get("sample_rate_hz")),
                centre_hz=_opt_float(item.get("centre_hz")),
                bandwidth_hz=_opt_float(item.get("bandwidth_hz")),
                signal_time=stamp,
                truncated=bool(item.get("truncated", False)),
                problems=[str(p) for p in item.get("problems") or []],
            ))
        return out


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def measure_part(browser, path: str, size: int = 0) -> PartMeasurement:
    """Read one file's header and metadata on the sensor (or a local path when `browser` is None).

    WAVE parts are read from their own header and CRFS ``meta`` trailer; XDAT
    parts from the ``.xhdr`` the sensor writes beside them (sample count, rate,
    centre, bandwidth and ``start_capture``). The sensor writes the ``.xhdr``
    after the samples, so a ``.xdat`` without one is "still being written" and
    is read again by the caller; it is never measured from its size.
    """
    from .iq_merge import MergeError, inspect_part
    from .xdat_header import XdatHeaderMissing, inspect_xdat, inspect_xdat_remote, is_xdat_file

    out = PartMeasurement(path=path, size=int(size or 0))
    try:
        if is_xdat_file(path):
            info = inspect_xdat(path) if browser is None else inspect_xdat_remote(browser, path, out.size)
        else:
            info = inspect_part(path) if browser is None else browser.inspect_wave(path)
    except XdatHeaderMissing as exc:
        detail = str(exc).replace(f"{out.name}: ", "")
        out.problems.append(f"header still being written ({detail.rstrip('.')})")
        return out
    except MergeError as exc:
        out.problems.append(str(exc).replace(f"{out.name}: ", "").replace(f"{out.name} ", ""))
        return out
    except Exception as exc:  # noqa: BLE001 - an unreadable file stays unknown, never guessed
        out.problems.append(f"header not read ({type(exc).__name__})")
        return out
    out.size = int(info.size)
    rate = float(info.fmt.sample_rate or 0)
    if rate > 0 and info.fmt.block_align:
        out.duration_s = info.frames / rate
        out.sample_rate_hz = rate
    out.truncated = any("truncated" in problem for problem in info.problems)
    out.problems.extend(info.problems)
    if info.meta is not None:
        out.centre_hz = info.meta.rf_frequency_hz
        out.bandwidth_hz = info.meta.bandwidth_hz
        out.signal_time = info.meta.signal_time
        if info.meta.sample_rate_hz and out.sample_rate_hz and abs(info.meta.sample_rate_hz - out.sample_rate_hz) > 1:
            out.problems.append(
                f"metadata sample rate {info.meta.sample_rate_hz:g} differs from the header's {out.sample_rate_hz:g}"
            )
    return out


def measure_parts(browser, paths: Iterable[str], sizes: dict[str, int] | None = None) -> CaptureMeasurement:
    """Measure the sample files; XDAT sidecars (.xhdr/.xmrk) are not parts and are left out."""
    from .xdat_header import is_xdat_sidecar

    sizes = sizes or {}
    return CaptureMeasurement(parts=[measure_part(browser, path, sizes.get(path, 0)) for path in paths if not is_xdat_sidecar(path)])


@dataclass(frozen=True)
class Verdict:
    result: str
    reason: str = ""


def classify(
    *,
    requested_s: float,
    measurement: CaptureMeasurement | None,
    named_parts: list[str] | None = None,
    sensor_error: str = "",
    request_sent: bool = True,
) -> Verdict:
    """Completed / partial / unconfirmed / failed from what the files and the sensor said."""
    if not request_sent:
        return Verdict(RESULT_FAILED, "not sent")
    parts = measurement.parts if measurement is not None else []
    if not parts:
        if sensor_error and not named_parts:
            return Verdict(RESULT_FAILED, "rejected by the sensor")
        return Verdict(RESULT_UNCONFIRMED, "no file confirmed")
    found = {part.name for part in parts}
    missing = [name for name in (named_parts or []) if name not in found]
    measured = measurement.duration_s
    if measured is None:
        if missing or sensor_error or measurement.truncated:
            return Verdict(RESULT_PARTIAL, "files incomplete; duration unknown")
        return Verdict(RESULT_UNCONFIRMED, "duration unknown")
    short = requested_s > 0 and measured < requested_s * (1.0 - PARTIAL_TOLERANCE)
    if missing:
        return Verdict(RESULT_PARTIAL, f"missing {', '.join(missing)}")
    if measurement.truncated:
        return Verdict(RESULT_PARTIAL, "a file is cut short")
    if sensor_error:
        return Verdict(RESULT_PARTIAL, "sensor error during the recording")
    if short:
        return Verdict(RESULT_PARTIAL, "shorter than requested")
    return Verdict(RESULT_COMPLETED, "")


def local_time(value: datetime | None) -> datetime | None:
    """A sensor UTC time as computer-local wall-clock time without tzinfo (for the workbook)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().replace(tzinfo=None)


def format_mhz_value(hz: float | None) -> str:
    if hz is None:
        return "unknown"
    value = float(hz) / 1e6
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def received_text(measurement: CaptureMeasurement | None, files: int) -> str:
    """'2 files, 1.4 s' (what came back), duration 'unknown' when it could not be read."""
    count = f"{files} file" + ("" if files == 1 else "s")
    seconds = measurement.duration_s if measurement is not None else None
    return f"{count}, {seconds:.2f} s" if seconds is not None else f"{count}, duration unknown"


def coverage_text(measurement: CaptureMeasurement | None) -> str:
    """'3500-3600 MHz (100 MHz)' from the file metadata, else 'unknown'."""
    if measurement is None or not measurement.parts:
        return "unknown"
    centre, bandwidth = measurement.centre_hz, measurement.bandwidth_hz
    if centre is None or bandwidth is None or bandwidth <= 0:
        return "unknown"
    low, high = centre - bandwidth / 2, centre + bandwidth / 2
    return f"{format_mhz_value(low)}-{format_mhz_value(high)} MHz ({format_mhz_value(bandwidth)} MHz)"


def coverage_differs(measurement: CaptureMeasurement | None, start_hz: int, stop_hz: int) -> bool:
    """True when the metadata shows a range other than the one requested (beyond 1 kHz)."""
    if measurement is None:
        return False
    centre, bandwidth = measurement.centre_hz, measurement.bandwidth_hz
    if centre is None or bandwidth is None:
        return False
    return abs((centre - bandwidth / 2) - start_hz) > 1000 or abs((centre + bandwidth / 2) - stop_hz) > 1000
