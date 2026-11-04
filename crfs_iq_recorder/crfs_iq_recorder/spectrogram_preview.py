"""Lightweight waterfall preview matching the IQ-to-Spectrogram Converter.

Same stereo I/Q layout, jet colormap, frequency-vs-time axes, and dBFS/bin
scale. Spectrum plot is omitted. Only a short prefix is read.
"""

from __future__ import annotations

import io
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREVIEW_MAX_SAMPLES = 131_072
PREVIEW_MAX_BYTES = 786_432
PREVIEW_N_FFT = 256
PREVIEW_TIME_BINS = 96
PREVIEW_DPI = 80
PREVIEW_FIGSIZE = (7.2, 3.2)
POWER_EPS = 1e-12
QUIET_DBFS = -45.0


class PreviewError(ValueError):
    """Preview could not be built from the available bytes."""


@dataclass(frozen=True)
class PreviewResult:
    png_bytes: bytes
    caption: str
    sampled_seconds: float
    sample_rate: float
    peak_dbfs: float | None
    layout: str
    whole_file_read: bool
    cache_key: tuple[Any, ...]


def preview_dependencies() -> tuple[bool, str]:
    missing: list[str] = []
    for name in ("numpy", "matplotlib"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return False, "Preview needs " + ", ".join(missing) + "."
    return True, ""


def cache_key(path: str, size: int, mtime_ts: float | int | None) -> tuple[Any, ...]:
    return (str(path), int(size or 0), int(mtime_ts or 0))


def _try_converter():
    if getattr(sys, "frozen", False):
        return None
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import iq_data  # type: ignore
    except Exception:
        return None
    needed = (
        "_iq_from_stereo",
        "_stft_power",
        "_blackmanharris_window",
        "_axis_units",
        "_auto_db_limits",
        "_to_dbfs_bin",
        "_max_pool_axis0",
        "_normalize_cmap",
    )
    if all(hasattr(iq_data, name) for name in needed):
        return iq_data
    return None


def _axis_units(fs: float) -> tuple[float, str]:
    half = float(fs) / 2.0
    if half >= 1e6:
        return 1e6, "MHz"
    if half >= 1e3:
        return 1e3, "kHz"
    return 1.0, "Hz"


def _blackmanharris(n_fft: int):
    import numpy as np

    n = np.arange(n_fft, dtype=np.float64)
    two_pi = 2.0 * np.pi / float(n_fft)
    win = (
        0.35875
        - 0.48829 * np.cos(two_pi * n)
        + 0.14128 * np.cos(2.0 * two_pi * n)
        - 0.01168 * np.cos(3.0 * two_pi * n)
    )
    wsum = float(win.sum())
    scale = np.float32(1.0 / max(wsum, 1e-20))
    return win.astype(np.float32), scale


def _iq_from_stereo(block):
    import numpy as np

    if block.ndim != 2 or block.shape[1] < 2:
        raise PreviewError("Need stereo IQ (I left, Q right).")
    iq = np.empty(block.shape[0], dtype=np.complex64)
    iq.real = block[:, 0]
    iq.imag = block[:, 1]
    return iq


def _stft_power(iq, n_fft: int, hop: int, window, scale: float):
    import numpy as np

    n_samples = int(iq.shape[0])
    if n_samples < n_fft:
        return np.empty((n_fft, 0), dtype=np.float32)
    n_frames = 1 + (n_samples - n_fft) // hop
    iq = np.ascontiguousarray(iq, dtype=np.complex64)
    frames = np.lib.stride_tricks.as_strided(
        iq,
        shape=(n_frames, n_fft),
        strides=(iq.strides[0] * hop, iq.strides[0]),
        writeable=False,
    )
    windowed = np.multiply(frames, window, dtype=np.complex64)
    spec = np.fft.fft(windowed, n=n_fft, axis=1)
    power = spec.real * spec.real + spec.imag * spec.imag
    power *= np.float32(scale * scale)
    return np.ascontiguousarray(power.T, dtype=np.float32)


def _pool_time(power, n_out: int):
    import numpy as np

    n_time = int(power.shape[1])
    if n_time <= n_out:
        return power
    factor = n_time // n_out
    usable = factor * n_out
    trimmed = power[:, :usable]
    return trimmed.reshape(power.shape[0], n_out, factor).max(axis=2)


def _auto_db_limits(power_db):
    import numpy as np

    finite = power_db[np.isfinite(power_db)]
    if finite.size < 8:
        return None, None
    vmin, vmax = np.percentile(finite, [2.0, 98.0])
    vmin = float(vmin)
    vmax = float(vmax)
    if vmax < vmin:
        vmin, vmax = vmax, vmin
    if vmax - vmin < 8.0:
        mid = 0.5 * (vmin + vmax)
        vmin, vmax = mid - 4.0, mid + 4.0
    pad = max(0.5, 0.02 * (vmax - vmin))
    return vmin - pad, vmax + pad


def _read_xdat_prefix(path: Path, max_samples: int) -> tuple[Any, int, int, bool]:
    """First samples of an XDAT part, laid out as its own .xhdr states (int16 interleaved I/Q)."""
    import numpy as np

    from .iq_merge import MergeError
    from .xdat_header import BYTES_PER_IQ_SAMPLE, inspect_xdat

    try:
        info = inspect_xdat(path)
    except MergeError as exc:
        raise PreviewError(str(exc)) from exc
    n_frames = int(info.frames)
    take = min(n_frames, int(max_samples))
    with path.open("rb") as handle:
        raw = handle.read(take * BYTES_PER_IQ_SAMPLE)
    whole = take >= n_frames
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size < 4:
        raise PreviewError("Not enough samples.")
    usable = (samples.size // 2) * 2
    stereo = samples[:usable].reshape(-1, 2).astype(np.float32) * np.float32(1.0 / 32768.0)
    return stereo, int(info.fmt.sample_rate), n_frames, whole


def _read_stereo_prefix(path: Path, max_samples: int) -> tuple[Any, int, int, bool]:
    import numpy as np

    from .xdat_header import is_xdat_file

    if is_xdat_file(path):
        return _read_xdat_prefix(path, max_samples)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        fs = handle.getframerate()
        n_frames = handle.getnframes()
        if channels < 2:
            raise PreviewError(f"Need stereo IQ (got {channels} ch).")
        if width != 2:
            raise PreviewError("Need 16-bit PCM WAVE.")
        take = min(int(n_frames), int(max_samples))
        raw = handle.readframes(take)
    whole = take >= int(n_frames)
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size < 4:
        raise PreviewError("Not enough samples.")
    usable = (samples.size // 2) * 2
    stereo = samples[:usable].reshape(-1, 2).astype(np.float32) * np.float32(1.0 / 32768.0)
    if stereo.shape[1] > 2:
        stereo = stereo[:, :2]
    return stereo, int(fs), int(n_frames), whole


def _file_axis(path: Path, fs: float, requested_start_hz, requested_stop_hz):
    """Axis from the file's own CRFS metadata (absolute RF) or relative when it has none."""
    from .cellular_quality import PreviewAxis, preview_axis

    try:
        from .xdat_header import inspect_recording

        info = inspect_recording(path)
        return preview_axis([info], requested_start_hz=requested_start_hz, requested_stop_hz=requested_stop_hz)
    except Exception:  # noqa: BLE001 - no metadata: relative axis, nothing inferred
        return PreviewAxis(
            sample_rate_hz=float(fs),
            requested_start_hz=float(requested_start_hz) if requested_start_hz is not None else None,
            requested_stop_hz=float(requested_stop_hz) if requested_stop_hz is not None else None,
        )


def build_preview_from_path(
    path: Path | str,
    *,
    cache_id: tuple[Any, ...] | None = None,
    center_hz: int | None = None,
    bandwidth_hz: int | None = None,
    max_samples: int = PREVIEW_MAX_SAMPLES,
    requested_start_hz: int | float | None = None,
    requested_stop_hz: int | float | None = None,
) -> PreviewResult:
    """Waterfall of the file's first samples.

    The frequency axis is absolute RF only when the file itself carries the
    sensor's centre frequency (CRFS metadata); ``center_hz`` from the caller is
    shown as text, never used to place the axis. The requested start/stop are
    drawn as labelled markers on an absolute axis.
    """
    ok, reason = preview_dependencies()
    if not ok:
        raise PreviewError(reason)

    import numpy as np
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = Path(path)
    stereo, fs, total_frames, whole_from_header = _read_stereo_prefix(path, max_samples)
    axis = _file_axis(path, fs, requested_start_hz, requested_stop_hz)
    file_layout = "int16 interleaved I/Q XDAT (layout from its .xhdr)" if path.suffix.casefold() == ".xdat" else "stereo I/Q WAVE"
    sampled = stereo.shape[0]
    whole = bool(whole_from_header and sampled >= total_frames)
    sampled_seconds = float(sampled) / float(fs) if fs else 0.0
    n_fft = PREVIEW_N_FFT
    hop = max(n_fft // 2, 1)

    converter = _try_converter()
    if converter is not None:
        iq = converter._iq_from_stereo(stereo)
        window, scale, _enbw = converter._blackmanharris_window(n_fft)
        power = converter._stft_power(iq, n_fft, hop, window, scale)
        power = np.fft.fftshift(power, axes=0)
        if power.shape[0] > n_fft:
            power = converter._max_pool_axis0(power, n_fft)
        axis_units = converter._axis_units
        auto_db = converter._auto_db_limits
        to_db = converter._to_dbfs_bin
        cmap = converter._normalize_cmap("jet")
        layout = file_layout
    else:
        iq = _iq_from_stereo(stereo)
        window, scale = _blackmanharris(n_fft)
        power = _stft_power(iq, n_fft, hop, window, scale)
        power = np.fft.fftshift(power, axes=0)
        axis_units = _axis_units
        auto_db = _auto_db_limits
        to_db = None
        cmap = "jet"
        layout = file_layout
    del stereo, iq

    if power.size == 0 or power.shape[1] < 1:
        raise PreviewError("File too short for preview.")

    power = _pool_time(power, PREVIEW_TIME_BINS)
    if to_db is not None:
        power_db = to_db(power)
    else:
        power_db = np.empty_like(power, dtype=np.float32)
        np.add(power, POWER_EPS, out=power_db)
        np.log10(power_db, out=power_db)
        power_db *= np.float32(10.0)
    del power
    peak = float(np.nanmax(power_db)) if np.isfinite(power_db).any() else None
    vmin, vmax = auto_db(power_db)

    f_scale, f_unit = axis_units(fs)
    f_max = (float(fs) / 2.0) / f_scale
    if axis.absolute:
        # Absolute RF in MHz, exact centre from the file's metadata.
        x_lo, x_hi = axis.extent_mhz()
        x_label = "Frequency (MHz)"
    else:
        x_lo, x_hi = -f_max, f_max
        x_label = f"{f_unit} from centre (centre unknown: no frequency metadata in the file)"

    fig, ax = plt.subplots(figsize=(PREVIEW_FIGSIZE[0], PREVIEW_FIGSIZE[1] + 0.9), dpi=PREVIEW_DPI)
    im = ax.imshow(
        power_db.T,
        cmap=cmap,
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=[x_lo, x_hi, 0.0, sampled_seconds],
        vmin=vmin,
        vmax=vmax,
    )
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(0.0, sampled_seconds)
    for x_mhz, text in axis.markers_mhz():
        ax.axvline(x_mhz, color="white", linestyle="--", linewidth=1.0, alpha=0.9)
        if x_lo <= x_mhz <= x_hi:
            ax.text(x_mhz, sampled_seconds * 0.97, text, fontsize=7, color="white", rotation=90, ha="right", va="top")
    ax.set_xlabel(x_label, fontsize=8)
    ax.set_ylabel("Time (s)")
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("dBFS/bin")
    fig.text(0.01, 0.01, "\n".join(axis.legend_lines()), fontsize=6.5, va="bottom", ha="left", family="monospace")
    fig.tight_layout(rect=(0.0, 0.22, 1.0, 1.0))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PREVIEW_DPI)
    plt.close(fig)
    del power_db
    _ = bandwidth_hz
    png = buf.getvalue()

    caption = f"First {sampled_seconds:.3f} s"
    if axis.absolute:
        from .cellular_quality import _mhz

        caption += f" · centre {_mhz(axis.center_hz)} MHz (file metadata)"
    elif center_hz:
        caption += f" · planned {center_hz / 1e6:g} MHz (centre not confirmed by the file)"
    else:
        caption += " · centre unknown"
    if not whole:
        caption += " (sample)"
    if (not whole) and peak is not None and peak < QUIET_DBFS:
        caption += " — quiet here"
    key = cache_id if cache_id is not None else cache_key(str(path), path.stat().st_size, path.stat().st_mtime)
    return PreviewResult(
        png_bytes=png,
        caption=caption,
        sampled_seconds=sampled_seconds,
        sample_rate=float(fs),
        peak_dbfs=peak,
        layout=layout,
        whole_file_read=whole,
        cache_key=key,
    )
