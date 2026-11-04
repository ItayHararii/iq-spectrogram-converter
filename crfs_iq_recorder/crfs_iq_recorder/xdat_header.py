"""CRFS XDAT recordings: the ``.xhdr`` header the sensor writes next to each ``.xdat`` part.

An XDAT recording is three files with one stem: the sample file ``.xdat``,
an XML header ``.xhdr`` and a marker file ``.xmrk``. The header says, in the
sensor's own words, how the samples are laid out (``data_encoding``,
``sample_resolution``, ``iq_interleave``, ``little_endian``, ``signed_type``,
``samples``) and what was captured (``center_frequency``,
``acquisition_bandwidth``, ``sample_rate``, ``start_capture``,
``stop_capture``, ``timestamp_source``).

Everything here is read from that header. The sample file is decoded only
when the header describes it as 16-bit signed little-endian interleaved I/Q;
any other layout is reported as unsupported, never guessed. A ``.xdat`` whose
size differs from ``samples`` x bytes-per-sample is reported as cut short (or
longer than the header says), exactly as a WAVE part would be.

Seen on an RFeye Node R40-8 (firmware 2.25-325): 1.0 s at 10 MHz gives
``samples="15713248"`` at ``sample_rate="15625000"`` and a ``.xdat`` of exactly
samples x 4 bytes; ``start_capture`` is ``YYYY:DDD:HH:MM:SS.fffffffff`` (UTC,
day of year, nanoseconds) and matches the EMP reply's timestamp.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .iq_merge import MergeError, PartInfo, SignalInfo, WaveFormat, part_number, part_stem

XDAT_SUFFIX = ".xdat"
XHDR_SUFFIX = ".xhdr"
XMRK_SUFFIX = ".xmrk"
SIDECAR_SUFFIXES = frozenset({XHDR_SUFFIX, XMRK_SUFFIX})

# The one layout decoded here; the header must state every part of it.
SUPPORTED_ENCODING = "int16"
SUPPORTED_BITS = 16
BYTES_PER_IQ_SAMPLE = 4  # int16 I + int16 Q

_CAPTURE_TIME = re.compile(r"^\s*(\d{4}):(\d{1,3}):(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d{1,9}))?\s*$")


def is_xdat_file(name: str | Path) -> bool:
    return Path(str(name)).suffix.casefold() == XDAT_SUFFIX


def is_xdat_sidecar(name: str | Path) -> bool:
    """``.xhdr`` / ``.xmrk``: kept with the recording, not sample data."""
    return Path(str(name)).suffix.casefold() in SIDECAR_SUFFIXES


def header_path_for(xdat_path: str | Path) -> str:
    """The ``.xhdr`` beside a ``.xdat`` (remote ``/`` paths and local paths alike)."""
    text = str(xdat_path)
    stem, _dot, _ext = text.rpartition(".")
    return (stem or text) + XHDR_SUFFIX


class XdatHeaderMissing(MergeError):
    """No ``.xhdr`` beside the ``.xdat`` (the sensor writes it after the samples)."""


def parse_capture_time(text: str | None) -> datetime | None:
    """``2026:308:19:25:18.001002104`` (UTC, day of year, up to ns) -> aware UTC datetime."""
    match = _CAPTURE_TIME.match(str(text or ""))
    if not match:
        return None
    year, doy, hour, minute, second, frac = match.groups()
    try:
        base = datetime(int(year), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(doy) - 1)
        micro = int((frac or "0").ljust(9, "0")[:6])
        return base.replace(hour=int(hour), minute=int(minute), second=int(second), microsecond=micro)
    except (ValueError, OverflowError):
        return None


def _truthy(value: Any) -> bool | None:
    text = str(value if value is not None else "").strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _num(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class XdatHeader:
    """What one ``.xhdr`` says. Unknown values stay None; nothing is filled in."""

    name: str = ""
    device: str = ""
    capture_name: str = ""
    sample_rate_hz: float | None = None
    center_hz: float | None = None
    bandwidth_hz: float | None = None
    start_utc: datetime | None = None
    stop_utc: datetime | None = None
    timestamp_source: str = ""
    samples: int | None = None
    channel_count: int | None = None
    sample_resolution_bits: int | None = None
    iq_interleave: bool | None = None
    little_endian: bool | None = None
    signed_type: bool | None = None
    data_encoding: str = ""
    data_file: str = ""
    xml: str = ""

    @property
    def layout_problem(self) -> str:
        """Why the samples cannot be decoded here ('' when the header states the supported layout)."""
        if self.data_encoding.casefold() != SUPPORTED_ENCODING or self.sample_resolution_bits != SUPPORTED_BITS:
            return f"sample layout {self.data_encoding or 'unknown'}/{self.sample_resolution_bits or '?'} bit is not decoded here (int16 only)"
        if self.iq_interleave is not True:
            return "the header does not state interleaved I/Q"
        if self.little_endian is not True:
            return "the header does not state little-endian samples"
        if self.signed_type is False:
            return "the header states unsigned samples"
        if (self.channel_count or 1) != 1:
            return f"{self.channel_count} channels in the header; one I/Q channel is expected"
        return ""

    @property
    def expected_bytes(self) -> int | None:
        if self.samples is None or self.layout_problem:
            return None
        return int(self.samples) * BYTES_PER_IQ_SAMPLE

    @property
    def duration_s(self) -> float | None:
        if self.samples is None or not self.sample_rate_hz:
            return None
        return float(self.samples) / float(self.sample_rate_hz)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "device": self.device,
            "capture_name": self.capture_name,
            "sample_rate_hz": self.sample_rate_hz,
            "center_hz": self.center_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "start_utc": self.start_utc.isoformat() if self.start_utc else None,
            "stop_utc": self.stop_utc.isoformat() if self.stop_utc else None,
            "timestamp_source": self.timestamp_source,
            "samples": self.samples,
            "data_encoding": self.data_encoding,
            "sample_resolution_bits": self.sample_resolution_bits,
            "layout_problem": self.layout_problem,
        }


def parse_xhdr(text: str | bytes, *, name: str = "") -> XdatHeader:
    """Parse one ``.xhdr`` document; raises MergeError when it is not an xcom header."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise MergeError(f"{name or 'header'}: the .xhdr is not well-formed XML ({exc}).") from exc
    if root.tag != "xcom_header":
        raise MergeError(f"{name or 'header'}: the .xhdr is not an xcom header (root <{root.tag}>).")
    device = root.find("device")
    capture = root.find("captures/capture")
    data = root.find("data_files/data")
    cap = capture.attrib if capture is not None else {}
    dat = data.attrib if data is not None else {}

    def as_int(value: Any) -> int | None:
        number = _num(value)
        return int(number) if number is not None else None

    return XdatHeader(
        name=str(root.attrib.get("name") or name or ""),
        device=str(device.attrib.get("type", "") if device is not None else ""),
        capture_name=str(cap.get("name", "")),
        sample_rate_hz=_num(cap.get("sample_rate")),
        center_hz=_num(cap.get("center_frequency")),
        bandwidth_hz=_num(cap.get("acquisition_bandwidth")),
        start_utc=parse_capture_time(cap.get("start_capture")),
        stop_utc=parse_capture_time(cap.get("stop_capture")),
        timestamp_source=str(cap.get("timestamp_source", "")),
        samples=as_int(dat.get("samples")),
        channel_count=as_int(dat.get("channel_count")),
        sample_resolution_bits=as_int(dat.get("sample_resolution")),
        iq_interleave=_truthy(dat.get("iq_interleave")),
        little_endian=_truthy(dat.get("little_endian")),
        signed_type=_truthy(dat.get("signed_type")),
        data_encoding=str(dat.get("data_encoding", "")),
        data_file=str(dat.get("name", "")),
        xml=text,
    )


def part_info_from_header(xdat_path: str | Path, size: int, header: XdatHeader) -> PartInfo:
    """A PartInfo for the ``.xdat`` so the WAVE tooling (blocks, previews, axis) can read it.

    ``frames`` is what the file holds; a file shorter than the header's sample
    count is reported as cut short, a longer one as holding more than the header
    says. Raises MergeError when the header's layout is not the one decoded here.
    """
    problem = header.layout_problem
    if problem:
        raise MergeError(f"{Path(str(xdat_path)).name}: {problem}.")
    rate = int(round(header.sample_rate_hz or 0))
    if rate <= 0:
        raise MergeError(f"{Path(str(xdat_path)).name}: the .xhdr gives no sample rate.")
    size = int(size or 0)
    problems: list[str] = []
    if size % BYTES_PER_IQ_SAMPLE:
        problems.append(f"the file ends in the middle of a sample ({size:,} bytes, {BYTES_PER_IQ_SAMPLE} per I/Q sample)")
    frames = size // BYTES_PER_IQ_SAMPLE
    expected = header.expected_bytes
    if expected is not None:
        if size < expected:
            problems.append(f"truncated: the file holds {frames:,} of the {header.samples:,} samples the .xhdr lists")
        elif size > expected:
            problems.append(f"the file holds {frames:,} samples, the .xhdr lists {header.samples:,}")
    if frames <= 0:
        problems.append("no samples")
    path = Path(str(xdat_path).replace("\\", "/"))
    fmt = WaveFormat(1, 2, rate, SUPPORTED_BITS, BYTES_PER_IQ_SAMPLE, rate * BYTES_PER_IQ_SAMPLE)
    meta = SignalInfo(
        signal_time=header.start_utc,
        rf_frequency_hz=header.center_hz,
        bandwidth_hz=header.bandwidth_hz,
        sample_rate_hz=header.sample_rate_hz,
        data_type="int16 I/Q (XDAT)",
        receiver=header.device,
        antenna="",
        xml=header.xml.encode("utf-8", errors="replace"),
    )
    return PartInfo(
        path=path,
        name=path.name,
        size=size,
        stem=part_stem(path.name),
        part=part_number(path.name),
        container="XDAT",
        fmt=fmt,
        data_offset=0,
        data_size=size,
        frames=frames,
        trailer=(),
        meta=meta,
        problems=tuple(problems),
    )


def inspect_xdat(path: str | Path) -> PartInfo:
    """Local ``.xdat`` with its ``.xhdr`` beside it."""
    xdat = Path(path)
    try:
        size = xdat.stat().st_size
    except OSError as exc:
        raise MergeError(f"{xdat.name}: cannot read ({exc.strerror or exc}).") from exc
    header_file = Path(header_path_for(xdat))
    try:
        text = header_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise XdatHeaderMissing(f"{xdat.name}: no {header_file.name} beside it.") from exc
    except OSError as exc:
        raise MergeError(f"{xdat.name}: cannot read {header_file.name} ({exc.strerror or exc}).") from exc
    return part_info_from_header(xdat, size, parse_xhdr(text, name=header_file.name))


def inspect_xdat_remote(browser, xdat_path: str, size: int = 0) -> PartInfo:
    """``.xdat`` on the sensor: reads only the small ``.xhdr`` beside it through `browser.open_read`."""
    from .sftp_client import SftpNotFoundError

    header_path = header_path_for(xdat_path)
    xdat_name = xdat_path.rsplit("/", 1)[-1]
    try:
        handle, _header_size = browser.open_read(header_path)
    except SftpNotFoundError as exc:
        raise XdatHeaderMissing(f"{xdat_name}: no .xhdr beside it on the sensor yet.") from exc
    try:
        text = handle.read()
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass
    if size <= 0:
        try:
            _data, size = browser.open_read(xdat_path)
            try:
                _data.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - the listing size is used when the file cannot be opened
            size = 0
    try:
        header = parse_xhdr(text, name=header_path.rsplit("/", 1)[-1])
    except MergeError as exc:
        # The sensor creates the .xhdr and fills it over a few seconds: an
        # empty or half-written header is read again, not reported as bad.
        raise XdatHeaderMissing(f"{xdat_name}: its .xhdr is not complete on the sensor yet ({exc}).") from exc
    return part_info_from_header(xdat_path, int(size or 0), header)


def inspect_recording(path: str | Path) -> PartInfo:
    """WAVE or XDAT (with its header) part on disk; MergeError when neither can be read."""
    from .iq_merge import inspect_part

    if is_xdat_file(path):
        return inspect_xdat(path)
    return inspect_part(path)
