"""Quality checks for cellular collection: pre-run readiness, post-capture file validation,
sampled previews, storage thresholds and coverage summaries.

Everything here reads files and reports; nothing is modified, merged or deleted.
"""

from __future__ import annotations

import math
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .cellular_plan import CellularPlan, format_bytes
from .iq_merge import MergeError, PartInfo, part_number
from .sensor_clock import CLOCK_INCORRECT_S, CLOCK_SYNCED_S, offset_text
from .sensor_storage import StorageSnapshot, format_storage_bytes, needed_bytes_with_margin
from .xdat_header import XdatHeaderMissing, inspect_recording, is_xdat_file, is_xdat_sidecar

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVEL_INFO = "info"
LEVEL_UNKNOWN = "unknown"
# Files were downloaded and checksummed, but their content was not inspected:
# the sample checks and previews here read RIFF/WAVE recordings only.
LEVEL_NOT_CHECKED = "not_checked"

WAVE_SUFFIXES = {".wav", ".wave"}


def is_wave_file(name: str | Path) -> bool:
    return Path(name).suffix.casefold() in WAVE_SUFFIXES

BLOCK_FRAMES = 65_536          # frames inspected at the beginning, middle and end
CLIP_FRACTION = 0.999          # |sample| above this fraction of full scale counts as clipped
CLIP_RATIO_WARN = 1e-4         # 0.01 % clipped samples in a block
ZERO_RATIO_WARN = 0.25         # a quarter of a block exactly zero in both I and Q
DC_OFFSET_WARN_DBFS = -30.0    # mean level this close to full scale is suspicious
LEVEL_CHANGE_WARN_DB = 10.0    # RMS difference between sampled blocks
DURATION_SHORT_RATIO = 0.95
DURATION_LONG_SLACK_S = 1.0
TIME_GAP_TOLERANCE_S = 0.010   # between consecutive split parts
TUNING_TOLERANCE_HZ = 1.0
PLAUSIBLE_YEAR_MIN = 2024
PREVIEW_SUFFIX = ".preview.png"

QUALITY_SCHEMA = 1

# One preview is drawn at a time: matplotlib's FreeType text rendering is not thread-safe.
_RENDER_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- post-capture


@dataclass
class BlockStats:
    position: str
    offset_frames: int
    frames: int
    rms_dbfs: float | None
    peak_dbfs: float | None
    clip_ratio: float
    zero_ratio: float
    invalid_ratio: float
    dc_dbfs: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "offset_frames": self.offset_frames,
            "frames": self.frames,
            "rms_dbfs": _round(self.rms_dbfs),
            "peak_dbfs": _round(self.peak_dbfs),
            "clip_ratio": self.clip_ratio,
            "zero_ratio": self.zero_ratio,
            "invalid_ratio": self.invalid_ratio,
            "dc_dbfs": _round(self.dc_dbfs),
        }


@dataclass
class FileQuality:
    name: str
    path: str
    size: int = 0
    readable: bool = False
    part: int | None = None
    problems: list[str] = field(default_factory=list)
    frames: int = 0
    duration_s: float = 0.0
    sample_rate_hz: int = 0
    bits: int = 0
    channels: int = 0
    container: str = ""
    center_hz: float | None = None
    bandwidth_hz: float | None = None
    meta_sample_rate_hz: float | None = None
    signal_time: datetime | None = None
    blocks: list[BlockStats] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "readable": self.readable,
            "part": self.part,
            "problems": list(self.problems),
            "frames": self.frames,
            "duration_s": round(self.duration_s, 6),
            "sample_rate_hz": self.sample_rate_hz,
            "bits": self.bits,
            "channels": self.channels,
            "container": self.container,
            "center_hz": self.center_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "meta_sample_rate_hz": self.meta_sample_rate_hz,
            "signal_time": self.signal_time.isoformat() if self.signal_time else None,
            "blocks": [b.to_dict() for b in self.blocks],
            "flags": list(self.flags),
        }


@dataclass
class CaptureQuality:
    level: str = LEVEL_UNKNOWN
    files: list[FileQuality] = field(default_factory=list)
    expected_duration_s: float | None = None
    actual_duration_s: float = 0.0
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    preview_path: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": QUALITY_SCHEMA,
            "level": self.level,
            "checked_at": self.checked_at,
            "expected_duration_s": self.expected_duration_s,
            "actual_duration_s": round(self.actual_duration_s, 6),
            "flags": list(self.flags),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "notes": list(self.notes),
            "preview_path": self.preview_path,
            "files": [f.to_dict() for f in self.files],
        }


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _dbfs(value: float, full_scale: float) -> float | None:
    if value <= 0 or full_scale <= 0:
        return None
    return 20.0 * math.log10(value / full_scale)


def _dtype_for(fmt) -> tuple[str, float] | None:
    """numpy dtype string and full-scale value for the PCM formats the Node writes."""
    if fmt.format_tag not in (1, 0xFFFE):
        return None
    if fmt.bits == 16:
        return "<i2", 32768.0
    if fmt.bits == 32:
        return "<i4", 2147483648.0
    if fmt.bits == 8:
        return "u1", 128.0
    return None


def _block_offsets(frames: int, block: int) -> list[tuple[str, int]]:
    if frames <= 0:
        return []
    if frames <= block:
        return [("whole", 0)]
    middle = max((frames - block) // 2, 0)
    end = max(frames - block, 0)
    offsets = [("begin", 0), ("middle", middle), ("end", end)]
    unique: list[tuple[str, int]] = []
    seen: set[int] = set()
    for name, offset in offsets:
        if offset not in seen:
            unique.append((name, offset))
            seen.add(offset)
    return unique


def read_block(info: PartInfo, offset_frames: int, frames: int):
    """Return a (frames, channels) float32 array scaled to ±1, or None when numpy is missing."""
    try:
        import numpy as np
    except ImportError:
        return None
    spec = _dtype_for(info.fmt)
    if spec is None or info.fmt.block_align <= 0:
        return None
    dtype, full_scale = spec
    frames = max(0, min(frames, info.frames - offset_frames))
    if frames <= 0:
        return None
    with Path(info.path).open("rb") as fh:
        fh.seek(info.data_offset + offset_frames * info.fmt.block_align)
        raw = fh.read(frames * info.fmt.block_align)
    usable = (len(raw) // info.fmt.block_align) * info.fmt.block_align
    if usable <= 0:
        return None
    samples = np.frombuffer(raw[:usable], dtype=dtype)
    if dtype == "u1":
        samples = samples.astype(np.float32) - 128.0
    block = samples.reshape(-1, info.fmt.channels).astype(np.float32) / np.float32(full_scale)
    return block


def block_stats(info: PartInfo, position: str, offset_frames: int, frames: int = BLOCK_FRAMES) -> BlockStats | None:
    try:
        import numpy as np
    except ImportError:
        return None
    block = read_block(info, offset_frames, frames)
    if block is None or block.size == 0:
        return None
    spec = _dtype_for(info.fmt)
    full_scale = spec[1] if spec else 1.0
    clip_level = CLIP_FRACTION * (full_scale - 1.0) / full_scale
    magnitude = np.abs(block)
    clip_ratio = float(np.count_nonzero(magnitude >= clip_level)) / float(block.size)
    row_zero = np.all(block == 0.0, axis=1)
    zero_ratio = float(np.count_nonzero(row_zero)) / float(block.shape[0])
    invalid_ratio = float(np.count_nonzero(~np.isfinite(block))) / float(block.size)
    finite = block[np.isfinite(block).all(axis=1)]
    if finite.size:
        rms = float(np.sqrt(np.mean(np.square(finite, dtype=np.float64))))
        peak = float(np.max(np.abs(finite)))
        dc = float(np.sqrt(np.sum(np.square(np.mean(finite, axis=0, dtype=np.float64)))))
    else:
        rms = peak = dc = 0.0
    return BlockStats(
        position=position,
        offset_frames=int(offset_frames),
        frames=int(block.shape[0]),
        rms_dbfs=_dbfs(rms, 1.0),
        peak_dbfs=_dbfs(peak, 1.0),
        clip_ratio=clip_ratio,
        zero_ratio=zero_ratio,
        invalid_ratio=invalid_ratio,
        dc_dbfs=_dbfs(dc, 1.0),
    )


def validate_file(path: Path | str, *, block_frames: int = BLOCK_FRAMES) -> FileQuality:
    """Container checks plus sampled sample statistics for one recording part."""
    path = Path(path)
    result = FileQuality(name=path.name, path=str(path), part=part_number(path.name))
    try:
        result.size = path.stat().st_size
    except OSError as exc:
        result.flags.append("missing_file")
        result.problems.append(f"cannot read: {exc.strerror or exc}")
        return result
    if result.size == 0:
        result.flags.append("empty")
        result.problems.append("the file is empty")
        return result
    if not is_wave_file(path) and not is_xdat_file(path):
        # NCP, HDF5 (and sidecar files): the on-disk layout is not published,
        # so nothing here claims to have checked their content.
        result.container = path.suffix.lstrip(".").upper() or "unknown"
        result.flags.append("not_wave")
        result.problems.append(f"{result.container} file: content checks apply to WAVE and XDAT recordings only")
        return result
    try:
        # XDAT is decoded only as its own .xhdr describes it (int16 I/Q).
        info = inspect_recording(path)
    except XdatHeaderMissing as exc:
        result.container = "XDAT"
        result.flags.append("not_wave")
        result.problems.append(f"{exc} The .xhdr is needed to read the samples; nothing was checked.")
        return result
    except MergeError as exc:
        if is_xdat_file(path) and "not decoded here" in str(exc):
            result.container = "XDAT"
            result.flags.append("not_wave")
            result.problems.append(str(exc))
            return result
        result.flags.append("corrupt")
        result.problems.append(str(exc))
        return result
    result.readable = True
    result.problems.extend(info.problems)
    result.frames = info.frames
    result.duration_s = info.duration_s
    result.sample_rate_hz = info.fmt.sample_rate
    result.bits = info.fmt.bits
    result.channels = info.fmt.channels
    result.container = info.container
    if info.meta is not None:
        result.center_hz = info.meta.rf_frequency_hz
        result.bandwidth_hz = info.meta.bandwidth_hz
        result.meta_sample_rate_hz = info.meta.sample_rate_hz
        result.signal_time = info.meta.signal_time
    for problem in info.problems:
        lowered = problem.casefold()
        if "truncated" in lowered or "cut short" in lowered or "middle of a sample" in lowered:
            result.flags.append("truncated")
        elif "no samples" in lowered:
            result.flags.append("empty")
        elif "unsupported" in lowered or "alignment" in lowered:
            result.flags.append("corrupt")
        else:
            result.flags.append("container_warning")
    if info.frames <= 0:
        return result
    if result.meta_sample_rate_hz and abs(result.meta_sample_rate_hz - info.fmt.sample_rate) > 0.5:
        result.flags.append("rate_mismatch")
        result.problems.append(
            f"meta trailer says {result.meta_sample_rate_hz:,.0f} S/s, the fmt chunk says {info.fmt.sample_rate:,} S/s"
        )
    for position, offset in _block_offsets(info.frames, block_frames):
        stats = block_stats(info, position, offset, block_frames)
        if stats is None:
            continue
        result.blocks.append(stats)
        if stats.clip_ratio >= CLIP_RATIO_WARN:
            result.flags.append(f"clipping:{position}")
        if stats.zero_ratio >= ZERO_RATIO_WARN:
            result.flags.append(f"zero_samples:{position}")
        if stats.invalid_ratio > 0:
            result.flags.append(f"invalid_samples:{position}")
        if stats.dc_dbfs is not None and stats.dc_dbfs > DC_OFFSET_WARN_DBFS:
            result.flags.append(f"dc_offset:{position}")
    levels = [b.rms_dbfs for b in result.blocks if b.rms_dbfs is not None]
    if len(levels) >= 2 and (max(levels) - min(levels)) >= LEVEL_CHANGE_WARN_DB:
        result.flags.append("level_change")
    result.flags = list(dict.fromkeys(result.flags))
    return result


def validate_capture(
    paths: list[Path | str],
    *,
    expected_duration_s: float | None = None,
    expected_center_hz: int | None = None,
    expected_bandwidth_hz: int | None = None,
    expected_sample_rate_hz: int | None = None,
    expected_parts: list[str] | None = None,
    block_frames: int = BLOCK_FRAMES,
) -> CaptureQuality:
    """Validate every part of one logical capture and compare it with what was requested."""
    result = CaptureQuality(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    result.expected_duration_s = expected_duration_s
    sidecars = [Path(p).name for p in paths if is_xdat_sidecar(p)]
    if sidecars:
        # The XDAT header and marker files travel with the recording; they
        # hold no samples. The .xhdr is read when its .xdat is checked.
        result.notes.append("Kept with the recording (not sample data): " + ", ".join(sorted(sidecars)) + ".")
    files = [validate_file(p, block_frames=block_frames) for p in paths if not is_xdat_sidecar(p)]
    files.sort(key=lambda f: (f.part is None, f.part or 0, f.name))
    result.files = files
    if not files:
        result.errors.append("No files to check.")
        result.flags.append("no_files")
        result.level = LEVEL_ERROR
        return result

    # Missing / extra parts.
    numbers = [f.part for f in files if f.part is not None]
    if expected_parts:
        have = {f.name for f in files}
        for name in expected_parts:
            if is_xdat_sidecar(name):
                continue
            if name not in have:
                result.flags.append("missing_part")
                result.errors.append(f"Part {name} reported by the sensor is not among the checked files.")
    if numbers:
        for n in range(min(numbers), max(numbers) + 1):
            if n not in numbers:
                result.flags.append("missing_part")
                result.errors.append(f"Part number {n:04d} is missing between {min(numbers):04d} and {max(numbers):04d}.")

    total = 0.0
    unchecked = [f for f in files if "not_wave" in f.flags]
    if unchecked:
        formats = ", ".join(sorted({f.container for f in unchecked}))
        result.notes.append(
            f"Recording format {formats}: sample checks, duration measurement and previews apply to WAVE "
            "and XDAT (with .xhdr) recordings only. File sizes and SHA-256 checksums were verified by the download."
        )
    for f in files:
        total += f.duration_s
        for flag in f.flags:
            if flag == "not_wave":
                continue
            if flag in {"empty", "truncated", "corrupt", "missing_file"}:
                result.errors.append(f"{f.name}: {flag} — {'; '.join(f.problems) or 'see file problems'}")
            elif flag.startswith(("clipping", "zero_samples", "invalid_samples", "dc_offset")) or flag in {"level_change", "rate_mismatch"}:
                result.warnings.append(f"{f.name}: {flag.replace(':', ' at ').replace('_', ' ')}")
            result.flags.append(flag.split(":")[0])
        if expected_center_hz is not None and f.center_hz is not None and abs(f.center_hz - expected_center_hz) > TUNING_TOLERANCE_HZ:
            result.flags.append("tuning_mismatch")
            result.errors.append(
                f"{f.name}: recorded at {f.center_hz / 1e6:.6f} MHz, the plan asked for {expected_center_hz / 1e6:.6f} MHz."
            )
        if expected_bandwidth_hz is not None and f.bandwidth_hz is not None and abs(f.bandwidth_hz - expected_bandwidth_hz) > TUNING_TOLERANCE_HZ:
            result.flags.append("bandwidth_mismatch")
            result.warnings.append(
                f"{f.name}: bandwidth {f.bandwidth_hz / 1e6:g} MHz in the file, {expected_bandwidth_hz / 1e6:g} MHz planned."
            )
        if expected_sample_rate_hz is not None and f.sample_rate_hz and abs(f.sample_rate_hz - expected_sample_rate_hz) > 0.5:
            result.flags.append("rate_mismatch")
            result.warnings.append(
                f"{f.name}: {f.sample_rate_hz:,} S/s in the file, {expected_sample_rate_hz:,} S/s estimated from the bandwidth."
            )
    result.actual_duration_s = total

    # Timing continuity between consecutive parts (from the meta trailer timestamps).
    previous: FileQuality | None = None
    for f in files:
        if previous is not None and previous.signal_time and f.signal_time and previous.duration_s > 0:
            expected_start = previous.signal_time + timedelta(seconds=previous.duration_s)
            delta = (f.signal_time - expected_start).total_seconds()
            if delta > TIME_GAP_TOLERANCE_S:
                result.flags.append("time_gap")
                result.warnings.append(f"{previous.name} → {f.name}: {delta * 1000:.1f} ms gap between parts.")
            elif delta < -TIME_GAP_TOLERANCE_S:
                result.flags.append("time_overlap")
                result.warnings.append(f"{previous.name} → {f.name}: parts overlap by {-delta * 1000:.1f} ms.")
        previous = f
    rms_values = [b.rms_dbfs for f in files for b in f.blocks if b.rms_dbfs is not None]
    if len(rms_values) >= 2 and (max(rms_values) - min(rms_values)) >= LEVEL_CHANGE_WARN_DB and "level_change" not in result.flags:
        result.flags.append("level_change")
        result.warnings.append(
            f"Level differs by {max(rms_values) - min(rms_values):.1f} dB between sampled blocks "
            "(noise floor, gain or signal change; not diagnosed further)."
        )

    if expected_duration_s is not None and expected_duration_s > 0 and total > 0:
        if total < expected_duration_s * DURATION_SHORT_RATIO:
            result.flags.append("short_duration")
            result.warnings.append(f"Files hold {total:.3f} s of IQ; {expected_duration_s:g} s was requested.")
        elif total > expected_duration_s + DURATION_LONG_SLACK_S:
            result.flags.append("long_duration")
            result.warnings.append(f"Files hold {total:.3f} s of IQ; {expected_duration_s:g} s was requested.")
    if unchecked:
        result.flags.append("not_wave")
    result.flags = list(dict.fromkeys(result.flags))
    if result.errors:
        result.level = LEVEL_ERROR
    elif result.warnings:
        result.level = LEVEL_WARNING
    elif len(unchecked) == len(files):
        result.level = LEVEL_NOT_CHECKED
    else:
        result.level = LEVEL_OK
    return result


# ----------------------------------------------------------------------------- previews


def preview_path_for(first_file: Path | str, stem: str = "") -> Path:
    first = Path(first_file)
    base = stem or first.stem.rsplit("_", 1)[0]
    return first.with_name(base + PREVIEW_SUFFIX)


@dataclass
class PreviewAxis:
    """How a spectrogram's frequency axis is labelled, decided from the file's own metadata.

    The axis is absolute (RF MHz) only when the file carries a centre
    frequency written by the sensor (CRFS ``meta`` trailer ``rf_frequency``);
    otherwise it stays relative to an unknown centre. The displayed span is
    always the sample rate (complex IQ shows fs of spectrum); the sensor's
    stated bandwidth is a separate number and is never read off the picture.
    """

    sample_rate_hz: float
    center_hz: float | None = None  # verified from the file; None = unknown
    sensor_bandwidth_hz: float | None = None  # from the file's metadata; None = unknown
    requested_start_hz: float | None = None
    requested_stop_hz: float | None = None

    @property
    def absolute(self) -> bool:
        return self.center_hz is not None

    @property
    def span_hz(self) -> float:
        return float(self.sample_rate_hz)

    def extent_mhz(self) -> tuple[float, float]:
        half = self.span_hz / 2.0 / 1e6
        if self.absolute:
            c = float(self.center_hz) / 1e6
            return c - half, c + half
        return -half, half

    def xlabel(self) -> str:
        return "Frequency (MHz)" if self.absolute else "MHz from centre (centre unknown: no frequency metadata in the file)"

    def markers_mhz(self) -> list[tuple[float, str]]:
        """Requested start/stop as absolute MHz; only on an absolute axis (never inferred)."""
        if not self.absolute:
            return []
        out: list[tuple[float, str]] = []
        if self.requested_start_hz is not None:
            out.append((float(self.requested_start_hz) / 1e6, f"requested start {_mhz(self.requested_start_hz)} MHz"))
        if self.requested_stop_hz is not None:
            out.append((float(self.requested_stop_hz) / 1e6, f"requested stop {_mhz(self.requested_stop_hz)} MHz"))
        return out

    def requested_text(self) -> str:
        if self.requested_start_hz is None or self.requested_stop_hz is None:
            return "Requested range: unknown"
        width = (float(self.requested_stop_hz) - float(self.requested_start_hz)) / 1e6
        return f"Requested range: {_mhz(self.requested_start_hz)}-{_mhz(self.requested_stop_hz)} MHz ({width:g} MHz wide)"

    def legend_lines(self) -> list[str]:
        """What each number is, so a 125 MS/s span is never read as a verified 125 MHz bandwidth."""
        lines = [self.requested_text()]
        if self.absolute:
            lines.append(f"Centre (file metadata): {_mhz(self.center_hz)} MHz")
        else:
            lines.append("Centre: unknown (no frequency metadata in the file)")
        if self.sensor_bandwidth_hz:
            lines.append(f"Sensor-reported bandwidth (metadata): {_mhz(self.sensor_bandwidth_hz)} MHz")
        else:
            lines.append("Sensor-reported bandwidth: unknown")
        lines.append(f"Sample rate: {_mhz(self.sample_rate_hz)} MS/s")
        lines.append(f"Displayed span: {_mhz(self.span_hz)} MHz (= sample rate for complex IQ; not a measured bandwidth)")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": "absolute RF (MHz)" if self.absolute else "relative (MHz from centre), centre unknown",
            "center_hz": self.center_hz,
            "center_source": "file metadata (CRFS meta rf_frequency / XDAT header center_frequency)" if self.absolute else "unknown",
            "sensor_bandwidth_hz": self.sensor_bandwidth_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "displayed_span_hz": self.span_hz,
            "requested_start_hz": self.requested_start_hz,
            "requested_stop_hz": self.requested_stop_hz,
        }


def _mhz(value: Any) -> str:
    """Exact MHz text (no rounding to a tidy number): 1842.5, 1805, 1880.000001."""
    try:
        mhz = float(value) / 1e6
    except (TypeError, ValueError):
        return "unknown"
    text = f"{mhz:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def preview_axis(
    infos: list[PartInfo],
    *,
    requested_start_hz: int | float | None = None,
    requested_stop_hz: int | float | None = None,
) -> PreviewAxis:
    """Decide the axis from the parts' headers and CRFS metadata; nothing is inferred from the samples."""
    fs = float(infos[0].fmt.sample_rate or 0) if infos else 0.0
    centers = [float(i.meta.rf_frequency_hz) for i in infos if i.meta is not None and i.meta.rf_frequency_hz]
    bandwidths = [float(i.meta.bandwidth_hz) for i in infos if i.meta is not None and i.meta.bandwidth_hz]
    center = centers[0] if centers and all(abs(c - centers[0]) <= TUNING_TOLERANCE_HZ for c in centers) else None
    bandwidth = bandwidths[0] if bandwidths else None
    return PreviewAxis(
        sample_rate_hz=fs,
        center_hz=center,
        sensor_bandwidth_hz=bandwidth,
        requested_start_hz=float(requested_start_hz) if requested_start_hz is not None else None,
        requested_stop_hz=float(requested_stop_hz) if requested_stop_hz is not None else None,
    )


def describe_preview_axis(
    paths: list[Path | str],
    *,
    requested_start_hz: int | float | None = None,
    requested_stop_hz: int | float | None = None,
) -> dict[str, Any]:
    """The axis decision for these files as a dictionary (for manifests and quality records)."""
    infos: list[PartInfo] = []
    for p in paths:
        if is_xdat_sidecar(p):
            continue
        try:
            infos.append(inspect_recording(p))
        except (MergeError, OSError):
            continue
    if not infos:
        return {"axis": "unknown (no readable WAVE or XDAT part)", "center_hz": None, "center_source": "unknown"}
    return preview_axis(infos, requested_start_hz=requested_start_hz, requested_stop_hz=requested_stop_hz).to_dict()


def write_preview(
    paths: list[Path | str],
    out_path: Path | str,
    *,
    center_hz: int | None = None,
    label: str = "",
    block_frames: int = BLOCK_FRAMES,
    requested_start_hz: int | float | None = None,
    requested_stop_hz: int | float | None = None,
) -> Path:
    """Three small waterfalls: beginning, middle and end of the logical capture.

    Only ``block_frames`` samples are read at each point; the picture is a sample,
    not a full-file spectrogram, and says so in its title. The frequency axis is
    absolute RF only when the files carry the sensor's centre frequency; the
    requested start/stop are drawn as labelled markers on that axis. ``center_hz``
    (the planned centre) is shown as text only; it never positions the axis.
    """
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    from .spectrogram_preview import PREVIEW_N_FFT, _blackmanharris, _pool_time, _stft_power

    infos: list[PartInfo] = []
    for p in paths:
        if is_xdat_sidecar(p):
            continue
        try:
            infos.append(inspect_recording(p))
        except MergeError:
            continue
    infos = [i for i in infos if i.frames > 0 and _dtype_for(i.fmt) is not None and i.fmt.channels >= 2]
    if not infos:
        raise MergeError("No readable stereo IQ parts for a preview.")
    infos.sort(key=lambda i: (i.part is None, i.part or 0, i.name))
    total_frames = sum(i.frames for i in infos)
    fs = infos[0].fmt.sample_rate or 1
    axis = preview_axis(infos, requested_start_hz=requested_start_hz, requested_stop_hz=requested_stop_hz)

    def locate(global_offset: int) -> tuple[PartInfo, int]:
        remaining = global_offset
        for info in infos:
            if remaining < info.frames:
                return info, remaining
            remaining -= info.frames
        last = infos[-1]
        return last, max(last.frames - 1, 0)

    points = _block_offsets(total_frames, block_frames)
    n_fft = PREVIEW_N_FFT
    hop = n_fft // 2
    window, scale = _blackmanharris(n_fft)
    panels = []
    for position, offset in points:
        info, local = locate(offset)
        block = read_block(info, local, min(block_frames, info.frames - local))
        if block is None or block.shape[0] < n_fft:
            continue
        iq = np.empty(block.shape[0], dtype=np.complex64)
        iq.real = block[:, 0]
        iq.imag = block[:, 1]
        power = _stft_power(iq, n_fft, hop, window, scale)
        power = np.fft.fftshift(power, axes=0)
        power = _pool_time(power, 96)
        power_db = 10.0 * np.log10(power + 1e-12)
        panels.append((position, offset, block.shape[0], power_db))
    if not panels:
        raise MergeError("Parts are too short for a preview.")

    all_db = np.concatenate([p[3].ravel() for p in panels])
    finite = all_db[np.isfinite(all_db)]
    vmin, vmax = (float(np.percentile(finite, 2)), float(np.percentile(finite, 98))) if finite.size else (None, None)
    if vmin is not None and vmax is not None and vmax - vmin < 8:
        mid = 0.5 * (vmin + vmax)
        vmin, vmax = mid - 4, mid + 4
    x_lo, x_hi = axis.extent_mhz()
    title = "Sampled preview (not the whole file)"
    if label:
        title = f"{label} — {title}"
    if axis.absolute:
        title += f" · centre {_mhz(axis.center_hz)} MHz (file metadata)"
    elif center_hz:
        title += f" · planned centre {_mhz(center_hz)} MHz (not confirmed by the file)"
    else:
        title += " · centre unknown"
    markers = axis.markers_mhz()
    legend = axis.legend_lines()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The object-oriented Agg API keeps clear of pyplot's global figure registry;
    # the lock serialises FreeType text rendering, which is not thread-safe.
    with _RENDER_LOCK:
        fig = Figure(figsize=(2.9 * len(panels) + 1.0, 3.9), dpi=80)
        FigureCanvasAgg(fig)
        axes = fig.subplots(1, len(panels), squeeze=False)
        for ax, (position, offset, frames, power_db) in zip(axes[0], panels):
            seconds = frames / fs
            ax.imshow(
                power_db.T, cmap="jet", origin="lower", aspect="auto", interpolation="nearest",
                extent=[x_lo, x_hi, 0.0, seconds], vmin=vmin, vmax=vmax,
            )
            ax.set_xlim(x_lo, x_hi)
            for x_mhz, text in markers:
                inside = x_lo <= x_mhz <= x_hi
                ax.axvline(x_mhz, color="white", linestyle="--", linewidth=1.0, alpha=0.9)
                if not inside:
                    # Outside the displayed span: an arrow at the edge says where it is.
                    edge = x_lo if x_mhz < x_lo else x_hi
                    ax.annotate(text, xy=(edge, seconds * 0.5), xytext=(edge, seconds * 0.5), fontsize=6, color="white",
                                ha="left" if x_mhz < x_lo else "right", va="center")
                else:
                    ax.text(x_mhz, seconds * 0.97, text, fontsize=6, color="white", rotation=90, ha="right", va="top")
            ax.set_title(f"{position} @ {offset / fs:.3f} s", fontsize=9)
            ax.set_xlabel(axis.xlabel(), fontsize=7)
            ax.tick_params(labelsize=7)
        axes[0][0].set_ylabel("Time (s)", fontsize=8)
        fig.suptitle(title, fontsize=9)
        fig.text(0.01, 0.01, "\n".join(legend), fontsize=6.5, va="bottom", ha="left", family="monospace")
        fig.tight_layout(rect=(0.0, 0.2, 1.0, 1.0))
        fig.savefig(out, format="png", dpi=80)
    return out


# ----------------------------------------------------------------------------- pre-run


@dataclass
class Check:
    name: str
    level: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level, "message": self.message}


def local_free_bytes(path: Path | str) -> int | None:
    try:
        target = Path(path)
        while not target.exists() and target.parent != target:
            target = target.parent
        return int(shutil.disk_usage(str(target)).free)
    except OSError:
        return None


def plausible_time(stamp: datetime | None, *, now: datetime | None = None) -> str | None:
    """Return a problem description when a clock reading cannot be right."""
    if stamp is None:
        return None
    now = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp.year < PLAUSIBLE_YEAR_MIN:
        return f"reads {stamp.isoformat(timespec='seconds')}, before {PLAUSIBLE_YEAR_MIN}"
    if stamp > now + timedelta(days=1):
        return f"reads {stamp.isoformat(timespec='seconds')}, more than a day in the future"
    return None


CLOCK_DRIFT_WARN_S = CLOCK_SYNCED_S
CLOCK_DRIFT_ERROR_S = CLOCK_INCORRECT_S
# "Blocking" checks stop the pass; the others are reported and the pass goes on.
BLOCKING_CHECKS = {"connection", "identity_match", "sensor_storage", "local_storage", "destination", "plan"}


def local_writable(path: Path | str) -> str | None:
    """Create the folder and write a small probe file; the problem text when that fails."""
    import os
    import uuid

    folder = Path(path)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".crfs-write-check-{uuid.uuid4().hex[:8]}.tmp"
        with open(probe, "wb") as fh:
            fh.write(b"ok")
        os.replace(probe, probe)  # rename in place: the same call finalize_download makes
        probe.unlink()
    except OSError as exc:
        return exc.strerror or str(exc) or exc.__class__.__name__
    return None


def pre_run_checks(
    *,
    sensor_info: Any,
    sensor_storage: StorageSnapshot | None,
    local_dir: Path | str,
    plan: CellularPlan | None,
    local_reserve_bytes: int,
    sensor_time: datetime | None = None,
    previous_identity: dict[str, str] | None = None,
    gnss_status: str | None = None,
    now: datetime | None = None,
    expected_identity: dict[str, str] | None = None,
    clock_offset_s: float | None = None,
    clock_source: str = "",
    clock_uncertainty_s: float = 0.0,
    clock_note: str = "",
    workbook_path: str | Path | None = None,
    workbook_sheet: str = "",
    workbook_state: str | None = None,
    protocol_loaded: bool = False,
    check_writable: bool = True,
) -> list[Check]:
    """Readiness report before a pass starts. Nothing here starts a capture.

    ``expected_identity`` is the sensor the collection was planned for; a
    different model or serial blocks the pass. ``workbook_state`` is
    'open' / 'closed' / 'busy' / 'missing'; an unavailable workbook is a
    warning when the protocol is already loaded (results queue and wait),
    an error only when the plan itself cannot be read.
    """
    checks: list[Check] = []
    now = now or datetime.now(timezone.utc)

    # Connection and identity.
    connected = bool(getattr(sensor_info, "connected", False))
    model = str(getattr(sensor_info, "model", "") or "").strip()
    serial = str(getattr(sensor_info, "serial", "") or "").strip()
    firmware = str(getattr(sensor_info, "firmware", "") or "").strip()
    if not connected:
        checks.append(Check("connection", LEVEL_ERROR, f"Sensor not connected ({getattr(sensor_info, 'status', '') or 'unknown status'})."))
    else:
        known = [v for v in (model, serial) if v and v != "—"]
        if known:
            checks.append(Check("identity", LEVEL_OK, f"Sensor {model or 'unknown model'} · serial {serial or 'unknown'} · firmware {firmware or 'unknown'}."))
        else:
            checks.append(Check("identity", LEVEL_WARNING, "Sensor connected but model and serial could not be read; manifests will say unknown."))
    if expected_identity and connected:
        mismatch = []
        for key, value in (("model", model), ("serial", serial)):
            wanted = str(expected_identity.get(key) or "").strip()
            if wanted and value and wanted != value and wanted != "—" and value != "—":
                mismatch.append(f"{key} {wanted} expected, {value} answered")
        if mismatch:
            checks.append(Check("identity_match", LEVEL_ERROR, "This is not the sensor the collection was planned for: " + "; ".join(mismatch) + "."))
        else:
            checks.append(Check("identity_match", LEVEL_OK, "The sensor is the one the collection was planned for."))
    if previous_identity:
        changed = []
        for key, value in (("model", model), ("serial", serial)):
            before = str(previous_identity.get(key) or "").strip()
            if before and value and before != value and before != "—" and value != "—":
                changed.append(f"{key} {before} → {value}")
        if changed:
            checks.append(Check("identity", LEVEL_WARNING, "A different sensor than last time: " + ", ".join(changed) + "."))

    # Clocks.
    computer_problem = plausible_time(now, now=datetime.now(timezone.utc))
    if computer_problem:
        checks.append(Check("computer_clock", LEVEL_ERROR, f"Computer clock {computer_problem}."))
    else:
        checks.append(Check("computer_clock", LEVEL_OK, f"Computer clock {now.astimezone().isoformat(timespec='seconds')}."))
    if sensor_time is not None and sensor_time.tzinfo is None:
        sensor_time = sensor_time.replace(tzinfo=timezone.utc)
    if clock_offset_s is None and sensor_time is not None:
        clock_offset_s = (sensor_time.astimezone(timezone.utc) - now).total_seconds()
    if sensor_time is None and clock_offset_s is None:
        checks.append(Check("sensor_clock", LEVEL_INFO, "Sensor clock: unknown (the sensor's status page could not be read). Recording times come from the files' own timestamps; the difference to the computer is not known."))
    else:
        # Instants against the computer's clock (time zones do not matter), with the
        # reading's own uncertainty (half the round trip plus the page's 1 s).
        source = f" ({clock_source})" if clock_source else ""
        note = f" {clock_note}" if clock_note else ""
        margin = max(float(clock_uncertainty_s or 0.0), 0.0)
        if clock_offset_s is None:
            checks.append(Check("sensor_clock", LEVEL_OK, f"Sensor clock read{source}; offset not measured.{note}"))
        else:
            size = abs(float(clock_offset_s))
            direction = "ahead of" if clock_offset_s > 0 else "behind"
            if size - margin > CLOCK_DRIFT_ERROR_S:
                # Reported, not blocking: recording times follow the sensor's own timestamps either way.
                stamp = sensor_time.astimezone(timezone.utc) if sensor_time is not None else None
                reads = f" (reads {stamp:%Y-%m-%d %H:%M} UTC)" if stamp is not None and stamp.date() != now.astimezone(timezone.utc).date() else ""
                checks.append(Check("sensor_clock", LEVEL_ERROR, f"Sensor clock is {offset_text(clock_offset_s)} {direction} the computer{reads}{source}. Recording times and date folders follow the sensor's clock; check its time source.{note}"))
            elif size > CLOCK_DRIFT_WARN_S + margin:
                checks.append(Check("sensor_clock", LEVEL_WARNING, f"Sensor clock is {size:.1f} s {direction} the computer{source}.{note}"))
            else:
                within = f", within {margin:.1f} s" if margin else ""
                checks.append(Check("sensor_clock", LEVEL_OK, f"Sensor clock agrees with the computer ({clock_offset_s:+.1f} s{within}{source}).{note}"))
    checks.append(Check("gnss", LEVEL_INFO, f"GNSS status: {gnss_status or 'unknown (not reported by the sensor API used here)'}."))

    # Storage.
    estimate = plan.estimated_bytes if plan is not None else 0
    if sensor_storage is None or not sensor_storage.ok or sensor_storage.total_bytes <= 0:
        checks.append(Check("sensor_storage", LEVEL_WARNING, "Sensor storage: unknown (could not be read); captures are refused individually if space runs out."))
    else:
        needed = needed_bytes_with_margin(estimate)
        line = f"{format_storage_bytes(sensor_storage.free_bytes)} free on the sensor; one pass needs about {format_storage_bytes(needed)} (estimate with margin)."
        if sensor_storage.free_bytes < needed:
            checks.append(Check("sensor_storage", LEVEL_ERROR, "Not enough sensor storage for a complete pass. " + line + " Existing recordings are never deleted automatically."))
        elif sensor_storage.free_bytes < needed * 2:
            checks.append(Check("sensor_storage", LEVEL_WARNING, "Sensor storage is tight for repeated passes. " + line))
        else:
            checks.append(Check("sensor_storage", LEVEL_OK, line))
    free_local = local_free_bytes(local_dir)
    if free_local is None:
        checks.append(Check("local_storage", LEVEL_WARNING, f"Local destination {local_dir}: free space unknown (could not be checked)."))
    else:
        needed_local = estimate + int(local_reserve_bytes)
        line = (
            f"{format_bytes(free_local)} free at {local_dir}; one pass downloads about {format_bytes(estimate)} (estimate) "
            f"plus a {format_bytes(int(local_reserve_bytes))} reserve. Temporary .part files are written in the same folder."
        )
        if free_local < needed_local:
            checks.append(Check("local_storage", LEVEL_ERROR, "Not enough local space for a complete pass. " + line + " Nothing is deleted automatically."))
        else:
            checks.append(Check("local_storage", LEVEL_OK, line))
    if check_writable:
        problem = local_writable(local_dir)
        if problem:
            checks.append(Check("destination", LEVEL_ERROR, f"Local destination {local_dir} is not writable: {problem}."))
        else:
            checks.append(Check("destination", LEVEL_OK, f"Local destination {local_dir} is writable."))

    # Protocol workbook (results). Its absence never blocks a loaded plan: results queue.
    if workbook_path:
        name = Path(str(workbook_path)).name
        sheet = f", worksheet {workbook_sheet!r}" if workbook_sheet else ""
        state = "missing" if not Path(str(workbook_path)).is_file() else (workbook_state or "closed")
        if state == "missing":
            level = LEVEL_WARNING if protocol_loaded else LEVEL_ERROR
            text = f"Protocol workbook {name} not found{sheet}."
            text += " The plan is loaded; Excel updates wait in the queue until the file is back." if protocol_loaded else " Load it first."
            checks.append(Check("workbook", level, text))
        elif state == "busy":
            checks.append(Check("workbook", LEVEL_WARNING, f"Excel is busy (a cell being edited or a dialog open); updates to {name}{sheet} are queued and written when Excel is free."))
        elif state == "open":
            checks.append(Check("workbook", LEVEL_OK, f"{name}{sheet} is open in Excel; results are written into the open workbook."))
        else:
            checks.append(Check("workbook", LEVEL_OK, f"{name}{sheet} is available (closed; written directly)."))

    # Plan.
    if plan is None:
        checks.append(Check("plan", LEVEL_ERROR, "No plan loaded."))
    elif not plan.ready:
        checks.append(Check("plan", LEVEL_ERROR, f"The plan has {len(plan.errors)} error(s); fix the workbook or settings first."))
    else:
        bad = []
        for task in plan.tasks:
            try:
                start = float(getattr(task, "window_start_hz", 0) or 0)
                stop = float(getattr(task, "window_stop_hz", 0) or 0)
                seconds = float(getattr(task, "duration_s", 0) or 0)
            except (TypeError, ValueError):
                bad.append(f"{task.label()}: unreadable numbers")
                continue
            if not (0 < start < stop):
                bad.append(f"{task.label()}: start {start / 1e6:g} MHz must be below stop {stop / 1e6:g} MHz")
            if seconds <= 0:
                bad.append(f"{task.label()}: duration {seconds:g} s")
        if bad:
            checks.append(Check("plan", LEVEL_ERROR, "Rows with invalid values (not sent as they are): " + "; ".join(bad[:5]) + ("…" if len(bad) > 5 else "")))
        else:
            checks.append(Check("plan", LEVEL_OK, f"{len(plan.tasks)} capture(s) per pass, {plan.capture_seconds:g} s of IQ, about {format_bytes(plan.estimated_bytes)} (estimate). Ranges and durations are sent exactly as listed."))
    return checks


def blocking_checks(checks: list[Check]) -> list[Check]:
    """The errors that stop a pass (Excel being unavailable never does)."""
    return [c for c in checks if c.level == LEVEL_ERROR and c.name in BLOCKING_CHECKS]


def worst_level(checks: list[Check]) -> str:
    order = {LEVEL_ERROR: 3, LEVEL_WARNING: 2, LEVEL_OK: 1, LEVEL_INFO: 0, LEVEL_UNKNOWN: 0}
    worst = LEVEL_OK
    for check in checks:
        if order.get(check.level, 0) > order.get(worst, 0):
            worst = check.level
    return worst


# ----------------------------------------------------------------------------- coverage


def coverage_rows(session: Any) -> list[dict[str, Any]]:
    """One row per task state with the fields the summary groups and filters by."""
    rows: list[dict[str, Any]] = []
    settings = getattr(session, "settings", None)
    site = getattr(settings, "site", "") if settings is not None else ""
    for round_record in getattr(session, "rounds", []):
        for state in session.tasks.get(round_record.round_id, []):
            planned = session.planned(state.task_key)
            files = state.files or []
            quality = state.quality or {}
            rows.append(
                {
                    "round_id": round_record.round_id,
                    "round_status": round_record.status,
                    "scheduled_local": round_record.scheduled_local.isoformat(timespec="minutes") if round_record.scheduled_local else "",
                    "site": site,
                    "band": planned.band_label if planned else "",
                    "nominal_hz": planned.nominal_hz if planned else None,
                    "direction": planned.direction if planned else "",
                    "network": planned.network if planned else "",
                    "window": planned.range_text if planned else "",
                    "partial": bool(state.short),
                    "result": getattr(state, "result", "") or "",
                    "status": state.status,
                    "confirmed": state.status == "confirmed",
                    "files": len(files),
                    "files_verified": sum(1 for f in files if f.get("download_status") == "verified"),
                    "images": 1 if quality.get("preview_path") else 0,
                    "merged": 1 if state.merged_output else 0,
                    "held_s": (state.summary or {}).get("held_s"),
                    "quality_level": quality.get("level") or "",
                    "flags": list(quality.get("flags") or []),
                    "short": bool(state.short),
                }
            )
    return rows


def filter_rows(rows: list[dict[str, Any]], **criteria: Any) -> list[dict[str, Any]]:
    """Keep rows whose fields equal the given values (``None``/"" criteria are ignored)."""
    active = {k: v for k, v in criteria.items() if v not in (None, "")}
    return [r for r in rows if all(r.get(k) == v for k, v in active.items())]


def coverage_summary(rows: list[dict[str, Any]], group_by: tuple[str, ...] = ("round_id", "site", "band", "direction")) -> list[dict[str, Any]]:
    """Counts per group: planned, confirmed logical captures, files, images, uncertain, failed."""
    groups: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(k) for k in group_by)
        g = groups.setdefault(
            key,
            {**{k: row.get(k) for k in group_by}, "planned": 0, "confirmed": 0, "uncertain": 0, "failed": 0, "skipped": 0,
             "files": 0, "files_verified": 0, "images": 0, "merged": 0, "held_s": 0.0, "quality_warnings": 0},
        )
        g["planned"] += 1
        status = row.get("status")
        if status == "confirmed":
            g["confirmed"] += 1
            g["files"] += int(row.get("files") or 0)
            g["files_verified"] += int(row.get("files_verified") or 0)
            g["images"] += int(row.get("images") or 0)
            g["merged"] += int(row.get("merged") or 0)
            g["held_s"] += float(row.get("held_s") or 0.0)
            if row.get("flags") or row.get("short"):
                g["quality_warnings"] += 1
        elif status in {"unknown", "submitting", "recording"}:
            g["uncertain"] += 1
        elif status in {"failed", "not_sent", "sensor_error"}:
            g["failed"] += 1
        elif status == "skipped":
            g["skipped"] += 1
    ordered = sorted(groups.values(), key=lambda g: tuple(str(g.get(k) or "") for k in group_by))
    for g in ordered:
        g["held_s"] = round(g["held_s"], 3)
    return ordered


def coverage_text(summary: list[dict[str, Any]]) -> str:
    lines = []
    for g in summary:
        head = " · ".join(str(g[k]) for k in ("round_id", "site", "band", "direction") if k in g and g[k] not in (None, ""))
        lines.append(
            f"{head}: {g['confirmed']}/{g['planned']} confirmed, {g['files']} file(s), {g['images']} image(s), "
            f"{g['uncertain']} uncertain, {g['failed']} failed"
            + (f", {g['quality_warnings']} with quality warnings" if g["quality_warnings"] else "")
        )
    return "\n".join(lines)


ProgressFn = Callable[[str], None]
