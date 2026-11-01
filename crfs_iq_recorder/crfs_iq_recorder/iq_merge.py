"""Join the split WAVE parts of one CRFS recording into a single file.

The Node writes a recording as ``file_<hhmmss>_0001.wav``, ``_0002.wav``, ...
and rolls to the next part when the file reaches the ``storage.filesize`` set in
its ``ncpd.conf`` (500 MB on the Node 40-8 this application was built against).
Every part is a complete RIFF/WAVE: a 44-byte header, PCM 16-bit stereo (I, Q)
samples, then a CRFS ``meta`` chunk holding ``<signalinfo>`` XML with the part's
start time (UTC, 1 ms), centre frequency, bandwidth and sample rate.

This module parses those containers, checks that the parts belong together
and follow each other without a gap, and copies the sample payloads back to
back under a fresh header (RIFF, or RF64 once the output passes 4 GiB). The
samples are copied byte for byte: nothing is resampled, scaled, or filled in.
Pure Python, no Qt, so it can be tested and run headless.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from . import __version__

_PART_RE = re.compile(r"^(?P<stem>.+)_(?P<part>\d{4})(?P<copy> \(\d+\))?(?P<ext>\.[^.]+)$", re.IGNORECASE)
"""Sensor part names (`file_063929_0002.wav`), also as a second local download
of the same file (`file_063929_0002 (2).wav`)."""
_XML_FIELD = re.compile(r"<(?P<tag>[a-z_]+)>(?P<value>[^<]*)</(?P=tag)>")

RIFF_MAX_BYTES = 0xFFFFFFFF
"""Largest file a plain RIFF header can describe. Beyond this the output is RF64."""

DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 64 * 1024 * 1024
CONTINUITY_TOLERANCE_S = 0.0015
"""``signaltime`` carries milliseconds, so two parts that follow each other
exactly can still differ from the computed boundary by up to 1 ms of rounding."""
JOIN_CHECK_BYTES = 4096
MERGED_SUFFIX = "_merged"
PARTIAL_SUFFIX = ".partial"
SIDECAR_SUFFIX = ".merge.json"

STATUS_VERIFIED = "verified"
STATUS_UNKNOWN = "unknown"
STATUS_GAP = "gap"
STATUS_OVERLAP = "overlap"
STATUS_BROKEN = "broken"


class MergeError(Exception):
    """The parts cannot be merged (or the merge failed). The message says why."""


class MergeCancelled(MergeError):
    """The operator stopped the merge; no output was left behind."""


ProgressFn = Callable[[int, int], None]


@dataclass(frozen=True)
class WaveFormat:
    format_tag: int
    channels: int
    sample_rate: int
    bits: int
    block_align: int
    byte_rate: int
    raw: bytes = field(default=b"", compare=False, repr=False)

    @property
    def pcm(self) -> bool:
        return self.format_tag in (1, 0xFFFE)

    def describe(self) -> str:
        return f"PCM {self.bits}-bit, {self.channels} ch, {self.sample_rate:,} S/s"


@dataclass(frozen=True)
class SignalInfo:
    """What the CRFS ``meta`` trailer says about a part."""

    signal_time: datetime | None
    rf_frequency_hz: float | None
    bandwidth_hz: float | None
    sample_rate_hz: float | None
    data_type: str
    receiver: str
    antenna: str
    xml: bytes = field(default=b"", compare=False, repr=False)

    @classmethod
    def parse(cls, body: bytes) -> "SignalInfo":
        text = body.decode("utf-8", errors="replace")
        fields = {m.group("tag"): m.group("value").strip() for m in _XML_FIELD.finditer(text)}

        def num(key: str) -> float | None:
            try:
                return float(fields[key])
            except (KeyError, ValueError):
                return None

        return cls(
            signal_time=_parse_signal_time(fields.get("signaltime", "")),
            rf_frequency_hz=num("rf_frequency"),
            bandwidth_hz=num("bandwidth"),
            sample_rate_hz=num("sample_rate"),
            data_type=fields.get("data_type", ""),
            receiver=fields.get("receiver", ""),
            antenna=fields.get("antenna", ""),
            xml=body,
        )

    def tuning(self) -> tuple[float | None, float | None, float | None]:
        return self.rf_frequency_hz, self.bandwidth_hz, self.sample_rate_hz


def _parse_signal_time(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class PartInfo:
    path: Path
    name: str
    size: int
    stem: str
    part: int | None
    container: str
    fmt: WaveFormat
    data_offset: int
    data_size: int
    frames: int
    trailer: tuple[tuple[bytes, bytes], ...]
    meta: SignalInfo | None
    problems: tuple[str, ...]

    @property
    def duration_s(self) -> float:
        return self.frames / self.fmt.sample_rate if self.fmt.sample_rate else 0.0

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> dict:
        return {
            "name": self.name,
            "bytes": self.size,
            "frames": self.frames,
            "duration_s": round(self.duration_s, 9),
            "signal_time": self.meta.signal_time.isoformat() if self.meta and self.meta.signal_time else None,
            "problems": list(self.problems),
        }


def part_number(name: str) -> int | None:
    match = _PART_RE.match(name or "")
    return int(match.group("part")) if match else None


def part_stem(name: str) -> str:
    match = _PART_RE.match(name or "")
    if match:
        return match.group("stem")
    return Path(name).stem


def canonical_part_name(name: str) -> str:
    """The sensor's name for a part: ``file_063929_0002 (2).wav`` -> ``file_063929_0002.wav``."""
    match = _PART_RE.match(name or "")
    if match:
        return f"{match.group('stem')}_{match.group('part')}{match.group('ext')}"
    return name or ""


_MERGED_RE = re.compile(re.escape(MERGED_SUFFIX) + r"( \(\d+\))?$")


def is_merged_name(name: str) -> bool:
    """``<stem>_merged.wav`` or ``<stem>_merged (2).wav``: an output of this module."""
    return _MERGED_RE.search(Path(name or "").stem) is not None


# ----------------------------------------------------------------------------- parsing


def _read_exact(fh, count: int) -> bytes:
    data = fh.read(count)
    if len(data) != count:
        raise MergeError("The file ends inside its header.")
    return data


def inspect_part(path: os.PathLike | str) -> PartInfo:
    """Read the container structure of one WAVE/RF64 file (header and trailer only)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MergeError(f"{path.name}: cannot read ({exc.strerror or exc}).") from exc
    problems: list[str] = []
    with path.open("rb") as fh:
        head = _read_exact(fh, 12)
        magic, riff_size32, wave = head[:4], struct.unpack("<I", head[4:8])[0], head[8:12]
        if wave != b"WAVE" or magic not in (b"RIFF", b"RF64"):
            raise MergeError(f"{path.name} is not a WAVE file.")
        container = "RF64" if magic == b"RF64" else "RIFF"
        riff_size: int | None = None if (container == "RF64" and riff_size32 == 0xFFFFFFFF) else riff_size32
        ds64_data_size: int | None = None
        fmt: WaveFormat | None = None
        data_offset: int | None = None
        data_size: int | None = None
        pos = 12
        # Walk the chunks up to and including "data"; the trailer is read from the end.
        while pos + 8 <= size:
            fh.seek(pos)
            header = fh.read(8)
            if len(header) < 8:
                break
            cid = header[:4]
            csize = struct.unpack("<I", header[4:8])[0]
            if cid == b"ds64":
                body = _read_exact(fh, min(csize, 28))
                if len(body) >= 24:
                    riff_lo, riff_hi, data_lo, data_hi = struct.unpack("<IIII", body[:16])
                    riff_size = riff_lo | (riff_hi << 32)
                    ds64_data_size = data_lo | (data_hi << 32)
            elif cid == b"fmt ":
                body = _read_exact(fh, min(csize, 64))
                if len(body) < 16:
                    raise MergeError(f"{path.name}: the fmt chunk is too short.")
                tag, channels, rate, byte_rate, block_align, bits = struct.unpack("<HHIIHH", body[:16])
                fmt = WaveFormat(tag, channels, rate, bits, block_align, byte_rate, raw=body)
            elif cid == b"data":
                data_offset = pos + 8
                if csize == 0xFFFFFFFF and ds64_data_size is not None:
                    data_size = ds64_data_size
                else:
                    data_size = csize
                break
            pos += 8 + csize + (csize & 1)
        if fmt is None or data_offset is None or data_size is None:
            raise MergeError(f"{path.name}: no fmt/data chunks; not a usable WAVE file.")
        if not fmt.pcm or fmt.bits % 8 or fmt.channels < 1:
            problems.append(f"unsupported encoding (format tag {fmt.format_tag}, {fmt.bits} bit)")
        if fmt.block_align != fmt.channels * (fmt.bits // 8):
            problems.append("fmt block alignment does not match channels x bits")
        available = max(0, size - data_offset)
        if data_size > available:
            problems.append(
                f"truncated: header promises {data_size:,} sample bytes, the file holds {available:,}"
            )
            frames = available // fmt.block_align if fmt.block_align else 0
        else:
            frames = data_size // fmt.block_align if fmt.block_align else 0
        if fmt.block_align and data_size % fmt.block_align:
            problems.append("the data chunk ends in the middle of a sample")
        if data_size == 0:
            problems.append("no samples (still being written?)")
        trailer: list[tuple[bytes, bytes]] = []
        meta: SignalInfo | None = None
        tail_start = data_offset + data_size + (data_size & 1)
        if data_size <= available and tail_start < size:
            fh.seek(tail_start)
            tail = fh.read(min(size - tail_start, 1 << 20))
            tpos = 0
            while tpos + 8 <= len(tail):
                cid = tail[tpos:tpos + 4]
                csize = struct.unpack("<I", tail[tpos + 4:tpos + 8])[0]
                body = tail[tpos + 8:tpos + 8 + csize]
                if len(body) < csize:
                    problems.append(f"the {cid.decode('latin-1')!r} trailer chunk is cut short")
                    break
                trailer.append((cid, body))
                if cid == b"meta":
                    meta = SignalInfo.parse(body)
                tpos += 8 + csize + (csize & 1)
        if riff_size is not None and riff_size + 8 != size and not problems:
            # Real Node files match exactly; a copy that grew or was clipped does not.
            problems.append(f"RIFF size says {riff_size + 8:,} bytes, the file is {size:,}")
    return PartInfo(
        path=path,
        name=path.name,
        size=size,
        stem=part_stem(path.name),
        part=part_number(path.name),
        container=container,
        fmt=fmt,
        data_offset=data_offset,
        data_size=min(data_size, available),
        frames=frames,
        trailer=tuple(trailer),
        meta=meta,
        problems=tuple(problems),
    )


# ----------------------------------------------------------------------------- planning


@dataclass
class Join:
    before: str
    after: str
    status: str
    delta_s: float | None
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MergePlan:
    stem: str
    parts: list[PartInfo]
    joins: list[Join]
    fmt: WaveFormat | None
    total_frames: int
    total_data_bytes: int
    output_bytes: int
    container: str
    duration_s: float
    continuity: str
    errors: list[str]
    warnings: list[str]
    meta: SignalInfo | None

    @property
    def ok(self) -> bool:
        return not self.errors

    def names(self) -> list[str]:
        return [part.name for part in self.parts]

    def describe(self) -> str:
        if self.errors:
            return "; ".join(self.errors)
        size = human_bytes(self.output_bytes)
        cont = {
            STATUS_VERIFIED: "continuity verified from the parts' time stamps",
            STATUS_UNKNOWN: "continuity could not be verified (no time stamps)",
        }.get(self.continuity, self.continuity)
        return f"{len(self.parts)} parts, {self.duration_s:.3f} s, {size} {self.container}; {cont}."


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1000
    return f"{count} B"


def _trailer_bytes(meta_xml: bytes, comment: str) -> bytes:
    """``meta`` copied from the first part, then a LIST/INFO block naming the merge."""
    out = bytearray()
    if meta_xml:
        out += b"meta" + struct.pack("<I", len(meta_xml)) + meta_xml
        if len(meta_xml) & 1:
            out += b"\x00"
    info = bytearray()
    for cid, text in ((b"ISFT", f"CRFS IQ Recorder {__version__}"), (b"ICMT", comment)):
        body = text.encode("utf-8") + b"\x00"
        if len(body) & 1:
            body += b"\x00"
        info += cid + struct.pack("<I", len(body)) + body
    out += b"LIST" + struct.pack("<I", 4 + len(info)) + b"INFO" + info
    return bytes(out)


def _output_bytes(total_data: int, trailer_len: int, *, rf64: bool) -> int:
    base = 12 + 8 + 16 + 8 + total_data + (total_data & 1) + trailer_len
    return base + (8 + 28 if rf64 else 0)


def plan_merge(
    paths: Sequence[os.PathLike | str],
    *,
    tolerance_s: float = CONTINUITY_TOLERANCE_S,
    force_rf64: bool = False,
) -> MergePlan:
    """Inspect the parts and decide whether (and how) they can be merged.

    Nothing is written. ``plan.errors`` lists every reason the merge must not
    run; ``plan.joins`` records, for each boundary, whether the second part
    starts exactly where the first one ends (from the ``meta`` time stamps).
    """
    errors: list[str] = []
    warnings: list[str] = []
    parts: list[PartInfo] = []
    for item in paths:
        try:
            parts.append(inspect_part(item))
        except MergeError as exc:
            errors.append(str(exc))
    if not parts:
        errors.append("No readable WAVE parts were given.")
        return MergePlan("", [], [], None, 0, 0, 0, "RIFF", 0.0, STATUS_UNKNOWN, errors, warnings, None)

    stems = sorted({part.stem for part in parts})
    stem = stems[0]
    if len(stems) > 1:
        errors.append(f"The files belong to different recordings: {', '.join(stems)}.")
    if any(part.part is None for part in parts):
        names = [part.name for part in parts if part.part is None]
        errors.append(f"Not a numbered part (expected <name>_0001.wav style): {', '.join(names)}.")
    parts.sort(key=lambda part: (part.part if part.part is not None else 10**9, part.name))

    numbers = [part.part for part in parts if part.part is not None]
    seen: set[int] = set()
    for part in parts:
        if part.part is not None and part.part in seen:
            errors.append(f"Part {part.part:04d} is present twice ({part.name}).")
        if part.part is not None:
            seen.add(part.part)
    if numbers:
        expected = list(range(1, max(numbers) + 1))
        missing = [n for n in expected if n not in seen]
        if missing:
            errors.append(
                "Missing part(s) " + ", ".join(f"{n:04d}" for n in missing) + "; download them first."
            )
    if len(parts) < 2 and not errors:
        errors.append("Only one part: nothing to merge.")

    for part in parts:
        for problem in part.problems:
            errors.append(f"{part.name}: {problem}.")

    fmt = parts[0].fmt
    for part in parts[1:]:
        if part.fmt != fmt:
            errors.append(
                f"{part.name} is {part.fmt.describe()}, but {parts[0].name} is {fmt.describe()}."
            )
    metas = [part.meta for part in parts]
    first_meta = metas[0]
    if all(meta is not None for meta in metas):
        tunings = {meta.tuning() for meta in metas if meta is not None}
        if len(tunings) > 1:
            errors.append(
                "The parts were recorded with different tuning (centre frequency, bandwidth or sample "
                "rate); they cannot be presented as one fixed-frequency recording."
            )
        receivers = {meta.receiver for meta in metas if meta is not None and meta.receiver}
        if len(receivers) > 1:
            errors.append(f"The parts come from different receivers: {', '.join(sorted(receivers))}.")
        if first_meta is not None and first_meta.sample_rate_hz and int(first_meta.sample_rate_hz) != fmt.sample_rate:
            warnings.append(
                f"The meta chunk says {first_meta.sample_rate_hz:g} S/s but the WAVE header says {fmt.sample_rate:,} S/s."
            )
    elif any(meta is not None for meta in metas):
        warnings.append("Some parts carry no CRFS meta chunk; continuity is checked only where both sides have one.")
    else:
        warnings.append("No CRFS meta chunks: the parts' start times are unknown, so continuity cannot be verified.")

    joins: list[Join] = []
    for before, after in zip(parts, parts[1:]):
        joins.append(_check_join(before, after, tolerance_s))
    if any(join.status in (STATUS_GAP, STATUS_OVERLAP) for join in joins):
        continuity = STATUS_BROKEN
        for join in joins:
            if join.status in (STATUS_GAP, STATUS_OVERLAP):
                errors.append(join.detail)
    elif any(join.status == STATUS_UNKNOWN for join in joins):
        continuity = STATUS_UNKNOWN
    else:
        continuity = STATUS_VERIFIED

    total_frames = sum(part.frames for part in parts)
    total_data = sum(part.frames * fmt.block_align for part in parts)
    # Upper bound for the trailer (the real comment is shorter), so the RIFF /
    # RF64 decision made here always holds when the header is written.
    trailer_len = len(_trailer_bytes(first_meta.xml if first_meta else b"", "x" * 512))
    rf64 = force_rf64 or _output_bytes(total_data, trailer_len, rf64=False) > RIFF_MAX_BYTES
    output_bytes = _output_bytes(total_data, trailer_len, rf64=rf64)
    duration = total_frames / fmt.sample_rate if fmt.sample_rate else 0.0
    return MergePlan(
        stem=stem,
        parts=parts,
        joins=joins,
        fmt=fmt,
        total_frames=total_frames,
        total_data_bytes=total_data,
        output_bytes=output_bytes,
        container="RF64" if rf64 else "RIFF",
        duration_s=duration,
        continuity=continuity,
        errors=errors,
        warnings=warnings,
        meta=first_meta,
    )


def _check_join(before: PartInfo, after: PartInfo, tolerance_s: float) -> Join:
    if before.meta is None or after.meta is None or before.meta.signal_time is None or after.meta.signal_time is None:
        return Join(before.name, after.name, STATUS_UNKNOWN, None, "no time stamp on one side of the join")
    if not before.fmt.sample_rate:
        return Join(before.name, after.name, STATUS_UNKNOWN, None, "sample rate unknown")
    expected_s = before.frames / before.fmt.sample_rate
    actual_s = (after.meta.signal_time - before.meta.signal_time).total_seconds()
    delta = actual_s - expected_s
    if abs(delta) <= tolerance_s:
        return Join(
            before.name,
            after.name,
            STATUS_VERIFIED,
            round(delta, 6),
            f"{after.name} starts {actual_s:.3f} s after {before.name}, which holds {expected_s:.6f} s (within {tolerance_s * 1000:g} ms)",
        )
    samples = int(round(abs(delta) * before.fmt.sample_rate))
    if delta > 0:
        return Join(
            before.name,
            after.name,
            STATUS_GAP,
            round(delta, 6),
            f"Gap of about {delta * 1000:.1f} ms ({samples:,} samples) between {before.name} and {after.name}; "
            "the recording is not continuous there, so these parts are not merged.",
        )
    return Join(
        before.name,
        after.name,
        STATUS_OVERLAP,
        round(delta, 6),
        f"{after.name} starts about {-delta * 1000:.1f} ms ({samples:,} samples) before {before.name} ends; "
        "overlapping parts are not merged.",
    )


# ----------------------------------------------------------------------------- output


def merged_output_path(stem: str, folder: os.PathLike | str) -> Path:
    """``<stem>_merged.wav`` in ``folder``; never an existing file."""
    folder = Path(folder)
    candidate = folder / f"{stem}{MERGED_SUFFIX}.wav"
    index = 2
    while candidate.exists() or candidate.with_name(candidate.name + PARTIAL_SUFFIX).exists():
        candidate = folder / f"{stem}{MERGED_SUFFIX} ({index}).wav"
        index += 1
    return candidate


def sidecar_path(output: os.PathLike | str) -> Path:
    output = Path(output)
    return output.with_name(output.name + SIDECAR_SUFFIX)


@dataclass
class MergeResult:
    output: Path
    sidecar: Path
    bytes_written: int
    frames: int
    container: str
    duration_s: float
    elapsed_s: float
    verified: bool
    continuity: str

    def summary(self) -> str:
        return (
            f"{self.output.name}: {self.frames:,} samples, {self.duration_s:.3f} s, "
            f"{human_bytes(self.bytes_written)} {self.container}, continuity {self.continuity}."
        )


def _riff_header(fmt: WaveFormat, total_data: int, trailer_len: int, *, rf64: bool) -> bytes:
    fmt_body = fmt.raw[:16] if len(fmt.raw) >= 16 else struct.pack(
        "<HHIIHH", fmt.format_tag, fmt.channels, fmt.sample_rate, fmt.byte_rate, fmt.block_align, fmt.bits
    )
    fmt_chunk = b"fmt " + struct.pack("<I", 16) + fmt_body
    pad = total_data & 1
    if rf64:
        riff_size = 4 + (8 + 28) + len(fmt_chunk) + 8 + total_data + pad + trailer_len
        frames = total_data // fmt.block_align if fmt.block_align else 0
        ds64 = b"ds64" + struct.pack("<I", 28) + struct.pack(
            "<IIIIIII",
            riff_size & 0xFFFFFFFF,
            riff_size >> 32,
            total_data & 0xFFFFFFFF,
            total_data >> 32,
            frames & 0xFFFFFFFF,
            frames >> 32,
            0,
        )
        return b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + ds64 + fmt_chunk + b"data" + struct.pack("<I", 0xFFFFFFFF)
    riff_size = 4 + len(fmt_chunk) + 8 + total_data + pad + trailer_len
    if riff_size > RIFF_MAX_BYTES:
        raise MergeError("The merged file is too large for a RIFF header; RF64 is required.")
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + b"data" + struct.pack("<I", total_data)


def merge_parts(
    plan: MergePlan,
    output: os.PathLike | str,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    min_free_bytes: int = MIN_FREE_BYTES,
) -> MergeResult:
    """Write the merged file described by ``plan`` to ``output`` (which must not exist).

    The data is written to ``<output>.partial`` and renamed only after the
    whole payload is in place and verified, so an interrupted or failed merge
    never leaves a file that looks complete. The originals are not touched.
    """
    if not plan.ok:
        raise MergeError("; ".join(plan.errors))
    if plan.fmt is None:
        raise MergeError("No format information.")
    output = Path(output)
    if output.exists():
        raise MergeError(f"{output.name} already exists; it is not overwritten.")
    if part_number(output.name) is not None:
        raise MergeError("The output name looks like a numbered part; choose a different name.")
    output.parent.mkdir(parents=True, exist_ok=True)
    for part in plan.parts:
        if part.path.resolve() == output.resolve():
            raise MergeError("The output path is one of the input parts.")
    comment = (
        f"Merged from {len(plan.parts)} parts ({plan.parts[0].name} .. {plan.parts[-1].name}); "
        f"continuity {plan.continuity}; details in {sidecar_path(output).name}"
    )
    trailer = _trailer_bytes(plan.meta.xml if plan.meta else b"", comment)
    fmt = plan.fmt
    total_data = plan.total_data_bytes
    rf64 = plan.container == "RF64"
    header = _riff_header(fmt, total_data, len(trailer), rf64=rf64)
    expected_size = len(header) + total_data + (total_data & 1) + len(trailer)

    usage = shutil.disk_usage(output.parent)
    if usage.free < expected_size + min_free_bytes:
        raise MergeError(
            f"Not enough free space in {output.parent}: the merged file needs {human_bytes(expected_size)}, "
            f"{human_bytes(usage.free)} is free."
        )

    partial = output.with_name(output.name + PARTIAL_SUFFIX)
    if partial.exists():
        try:
            partial.unlink()
        except OSError as exc:
            raise MergeError(f"Cannot remove the leftover {partial.name}: {exc}") from exc
    started = time.perf_counter()
    done = 0
    if progress is not None:
        progress(0, total_data)
    try:
        with partial.open("wb") as out:
            out.write(header)
            for part in plan.parts:
                remaining = part.frames * fmt.block_align
                with part.path.open("rb") as src:
                    src.seek(part.data_offset)
                    while remaining > 0:
                        if cancel is not None and cancel.is_set():
                            raise MergeCancelled("Merge cancelled.")
                        block = src.read(min(chunk_bytes, remaining))
                        if not block:
                            raise MergeError(f"{part.name} ended early while copying; was it still being written?")
                        out.write(block)
                        remaining -= len(block)
                        done += len(block)
                        if progress is not None:
                            progress(done, total_data)
            if total_data & 1:
                out.write(b"\x00")
            out.write(trailer)
            out.flush()
            os.fsync(out.fileno())
        actual = partial.stat().st_size
        if actual != expected_size:
            raise MergeError(f"The merged file is {actual:,} bytes, expected {expected_size:,}.")
        verified = _verify_output(plan, partial, header_len=len(header))
        if not verified:
            raise MergeError("Verification failed: the merged samples do not match the parts at a join.")
        os.replace(partial, output)
    except BaseException:
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass
        raise
    result = MergeResult(
        output=output,
        sidecar=sidecar_path(output),
        bytes_written=expected_size,
        frames=plan.total_frames,
        container=plan.container,
        duration_s=plan.duration_s,
        elapsed_s=time.perf_counter() - started,
        verified=True,
        continuity=plan.continuity,
    )
    write_sidecar(plan, result)
    return result


def _verify_output(plan: MergePlan, output: Path, *, header_len: int) -> bool:
    """The output must describe exactly the summed samples and match every join byte for byte."""
    info = inspect_part(output)
    if info.problems or info.frames != plan.total_frames or info.fmt != plan.fmt:
        return False
    if info.data_offset != header_len:
        return False
    with output.open("rb") as out:
        offset = header_len
        for part in plan.parts:
            length = part.frames * plan.fmt.block_align
            with part.path.open("rb") as src:
                for rel in (0, max(0, length - JOIN_CHECK_BYTES)):
                    count = min(JOIN_CHECK_BYTES, length - rel)
                    if count <= 0:
                        continue
                    src.seek(part.data_offset + rel)
                    out.seek(offset + rel)
                    if src.read(count) != out.read(count):
                        return False
            offset += length
    return True


def write_sidecar(plan: MergePlan, result: MergeResult) -> Path:
    payload = {
        "merged_file": result.output.name,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "application": f"CRFS IQ Recorder {__version__}",
        "recording": plan.stem,
        "container": result.container,
        "format": {
            "encoding": "PCM" if plan.fmt and plan.fmt.format_tag == 1 else f"tag {plan.fmt.format_tag if plan.fmt else '?'}",
            "channels": plan.fmt.channels if plan.fmt else None,
            "bits": plan.fmt.bits if plan.fmt else None,
            "sample_rate": plan.fmt.sample_rate if plan.fmt else None,
            "channel_order": "I, Q",
        },
        "tuning": {
            "rf_frequency_hz": plan.meta.rf_frequency_hz if plan.meta else None,
            "bandwidth_hz": plan.meta.bandwidth_hz if plan.meta else None,
            "receiver": plan.meta.receiver if plan.meta else None,
            "start_time_utc": plan.meta.signal_time.isoformat() if plan.meta and plan.meta.signal_time else None,
        },
        "total_frames": result.frames,
        "duration_s": round(result.duration_s, 9),
        "bytes": result.bytes_written,
        "continuity": result.continuity,
        "continuity_note": {
            STATUS_VERIFIED: "Each part starts where the previous one ends, to within 1 ms (CRFS meta signaltime).",
            STATUS_UNKNOWN: "The parts carry no usable start times; continuity across the joins is not verified.",
        }.get(result.continuity, result.continuity),
        "parts": [part.summary() for part in plan.parts],
        "joins": [join.as_dict() for join in plan.joins],
        "warnings": list(plan.warnings),
        "verification": {
            "frames_equal_sum_of_parts": True,
            "join_bytes_compared": JOIN_CHECK_BYTES,
            "samples_modified": False,
        },
    }
    path = sidecar_path(result.output)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_sidecar(output: os.PathLike | str) -> dict | None:
    try:
        return json.loads(sidecar_path(output).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
