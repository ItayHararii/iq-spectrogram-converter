import argparse
import gc
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import iq_metadata

# matplotlib and scipy are imported on first use (see _plt / _fft / _get_window).
# Importing them here costs about a second before the GUI window can appear.
_plt_mod = None
_fft_fn = None
_get_window_fn = None


def _plt():
    global _plt_mod
    if _plt_mod is None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _plt_mod = plt
    return _plt_mod


def _numpy_fft(x, n=None, axis=-1, workers=None, overwrite_x=False):
    """scipy.fft.fft stand-in for builds without scipy (one thread, same spectrum)."""
    return np.fft.fft(x, n=n, axis=axis)


def _numpy_get_window(window, nx, fftbins=True):
    """scipy.signal.windows.get_window stand-in: periodic Blackman-Harris only."""
    if window != "blackmanharris":
        raise ValueError(f"window {window!r} needs scipy")
    coefs = (0.35875, 0.48829, 0.14128, 0.01168)
    m = int(nx) + 1 if fftbins else int(nx)
    fac = np.linspace(-np.pi, np.pi, m)
    w = np.zeros(m)
    for k, coef in enumerate(coefs):
        w += coef * np.cos(k * fac)
    return w[:-1] if fftbins else w


def _fft():
    global _fft_fn
    if _fft_fn is None:
        try:
            from scipy.fft import fft
        except ImportError:
            fft = _numpy_fft
        _fft_fn = fft
    return _fft_fn


def _get_window():
    global _get_window_fn
    if _get_window_fn is None:
        try:
            from scipy.signal.windows import get_window
        except ImportError:
            get_window = _numpy_get_window
        _get_window_fn = get_window
    return _get_window_fn


def fft_backend():
    """'scipy' or 'numpy': which FFT this process uses."""
    return "numpy" if _fft() is _numpy_fft else "scipy"


def preload_engine():
    """Import the heavy numeric and plotting modules (safe from a worker thread)."""
    _fft()
    _get_window()
    _plt()


class ConversionCancelled(Exception):
    """Raised by process_file when cancel_event is set."""


# Display grid cap: PNG is 12x8 in at 125 dpi. Full STFT (e.g. 32k x 9k)
# does not need to sit in RAM or be sent to the renderer.
MAX_FREQ_BINS = 2048
MAX_TIME_BINS = 1536
# Largest FFT the engine will run. 2^24 bins is 128 MB of float64 window plus
# a 256 MB framed block; anything finer gives no visible benefit on a 2048-bin
# display and risks MemoryError on an ordinary laptop.
MAX_N_FFT = 2**24
SMOOTH_TAPS = 5
FIGSIZE = (12.0, 8.0)
SPECTRUM_HEIGHT_RATIO = 1
WATERFALL_HEIGHT_RATIO = 3
SAVE_DPI = 125
# Spectrogram-only preview for tables (no axes, text or markers): the PNG's
# display grid, colormap and dB limits at a fixed size, 2:1 like its spectrogram panel.
THUMBNAIL_SIZE = (320, 160)
THUMBNAIL_COLORS = 128
ALLOWED_CMAPS = ("jet", "turbo", "viridis", "gray")
SPECTRUM_COLOR = "#1f77b4"
SPECTRUM_HOLD_COLOR = "#e07a3d"
SPECTRUM_FACE = "#f7f7f7"
FLOOR_COLOR = "#666666"
# Range markers: a light line on a dark stroke stays visible on every colormap
# and on the light spectrum panel. Requested = dashed magenta, sensor-reported =
# dotted white, so the two never look alike.
REQUEST_LINE_COLOR = "#ff3bd4"
REQUEST_TEXT_COLOR = "#a3007f"
SENSOR_LINE_COLOR = "#ffffff"
SENSOR_TEXT_COLOR = "#222222"
MARKER_STROKE_COLOR = "#111111"
REQUEST_DASHES = (0, (6.0, 3.0))
SENSOR_DASHES = (0, (1.0, 2.0))
MARKER_LINEWIDTH = 1.1
MARKER_STROKE_WIDTH = 2.6
TAG_FONTSIZE = 7.5
TAG_ROW_PT = 14.0
TAG_GAP_PT = 3.0
TAG_EDGE_INSET_PT = 3.0
INFO_FONTSIZE = 8.0
INFO_WRAP_CHARS = 135
TITLE_PAD_PT = 6.0
ETA_SCALE_MIN = 0.25
ETA_SCALE_MAX = 4.0
ETA_EMA_ALPHA = 0.35
CALIBRATION_NAME = ".iq_eta_calibration.json"
POWER_EPS = np.float32(1e-12)

_eta_scale_cache = None


def _app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _calibration_path():
    # A host that bundles the engine keeps the file in its own settings folder.
    override = os.environ.get("IQ_ETA_CALIBRATION", "").strip()
    if override:
        return Path(override)
    return _app_dir() / CALIBRATION_NAME


def _n_fft(fs, rbw):
    return int(2 ** np.ceil(np.log2(max(fs / max(float(rbw), 1.0) * 2, 2.0))))


def _n_stft_frames(n_samples, n_fft, hop):
    if n_samples < n_fft:
        return 0
    return 1 + (n_samples - n_fft) // hop


def _pooled_len(n_in, n_max):
    if n_in <= n_max:
        return n_in
    factor = int(np.ceil(n_in / n_max))
    return n_in // factor


def _max_pool_axis0(arr, out_bins):
    """Peak-hold (max) pool frequency (axis 0) with an integer factor; trim leftover bins."""
    n = arr.shape[0]
    if n <= out_bins:
        return arr
    factor = n // out_bins
    usable = factor * out_bins
    if usable != n:
        arr = arr[:usable]
    arr = np.ascontiguousarray(arr)
    n_time = arr.shape[1]
    return arr.reshape(out_bins, factor, n_time).max(axis=1)


def _smooth_frequency(power):
    """5-tap moving average along frequency (axis 0), zero-padded like np.convolve(..., 'same')."""
    pad = SMOOTH_TAPS // 2
    padded = np.pad(
        np.asarray(power, dtype=np.float32),
        ((pad, pad), (0, 0)),
        mode="constant",
        constant_values=np.float32(0.0),
    )
    acc = padded[:-4] + padded[1:-3] + padded[2:-2] + padded[3:-1] + padded[4:]
    del padded
    return acc * np.float32(1.0 / SMOOTH_TAPS)


def _iq_from_stereo(block):
    if block.ndim != 2 or block.shape[1] < 2:
        raise ValueError("Need stereo IQ (I left, Q right).")
    iq = np.empty(block.shape[0], dtype=np.complex64)
    iq.real = block[:, 0]
    iq.imag = block[:, 1]
    return iq


def _blackmanharris_window(n_fft):
    """Periodic Blackman-Harris window, scipy spectrum scale, and ENBW in bins.

    scipy.signal.stft(..., scaling='spectrum') multiplies Zxx by 1/sum(w), so
    |Zxx|^2 = |FFT(x*w)|^2 / sum(w)^2. That already includes coherent gain
    (cg = mean(w)); do not apply cg again.

    0 dBFS is a complex IQ tone x[n] = exp(jωn) with |a|=1 (WAV FS = ±1.0).
    """
    win64 = np.asarray(_get_window()("blackmanharris", n_fft, fftbins=True), dtype=np.float64)
    wsum = float(win64.sum())
    scale = np.float32(1.0 / max(wsum, 1e-20))
    enbw_bins = float(n_fft) * float(np.dot(win64, win64)) / max(wsum * wsum, 1e-30)
    return win64.astype(np.float32), scale, enbw_bins


def _stft_power(iq, n_fft, hop, window, scale):
    """Two-sided STFT linear power as float32 (n_fft, n_frames)."""
    iq = np.ascontiguousarray(iq, dtype=np.complex64)
    n_frames = _n_stft_frames(iq.shape[0], n_fft, hop)
    if n_frames < 1:
        return np.empty((n_fft, 0), dtype=np.float32)

    frame_view = np.lib.stride_tricks.as_strided(
        iq,
        shape=(n_frames, n_fft),
        strides=(iq.strides[0] * hop, iq.strides[0]),
        writeable=False,
    )
    windowed = np.multiply(frame_view, window, dtype=np.complex64)
    del frame_view
    workers = -1 if n_frames * n_fft >= 65536 else 1
    spec = _fft()(windowed, n=n_fft, axis=1, workers=workers, overwrite_x=True)
    if spec is not windowed:
        del windowed
    power = spec.real * spec.real + spec.imag * spec.imag
    del spec
    # scipy scaling='spectrum': |Zxx|^2 = |FFT(x*w)|^2 / sum(w)^2 (scale is 1/sum(w)).
    power *= np.float32(scale * scale)
    return np.ascontiguousarray(power.T, dtype=np.float32)


def _block_sample_count(n_fft, hop):
    """Keep one STFT block well under a few hundred MB even at large n_fft."""
    max_workspace = 96 * 1024 * 1024  # complex64 framed STFT workspace
    bytes_per_frame = n_fft * 8
    max_frames = max(24, int(max_workspace / max(bytes_per_frame, 1)))
    samples = (max_frames - 1) * hop + n_fft
    return int(min(max(samples, n_fft), 8_000_000))


def _load_eta_scale():
    global _eta_scale_cache
    if _eta_scale_cache is not None:
        return _eta_scale_cache
    path = _calibration_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scale = float(data.get("scale", 1.0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        scale = 1.0
    _eta_scale_cache = float(np.clip(scale, ETA_SCALE_MIN, ETA_SCALE_MAX))
    return _eta_scale_cache


def _update_eta_calibration(raw_predicted_s, actual_s):
    global _eta_scale_cache
    if raw_predicted_s < 2.0 or actual_s < 2.0:
        return
    old = _load_eta_scale()
    ratio = float(actual_s) / float(raw_predicted_s)
    scale = ETA_EMA_ALPHA * ratio + (1.0 - ETA_EMA_ALPHA) * old
    scale = float(np.clip(scale, ETA_SCALE_MIN, ETA_SCALE_MAX))
    _eta_scale_cache = scale
    payload = {
        "scale": scale,
        "last_raw_predicted": float(raw_predicted_s),
        "last_actual": float(actual_s),
        "last_ratio": ratio,
    }
    try:
        _calibration_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


_PART_SUFFIX = re.compile(r"^(?P<stem>.+)_(?P<part>\d{4})$")


def part_time_offset(path):
    """Where a CRFS split part starts within its recording.

    The sensor splits long recordings into ``<stem>_0001.wav``, ``_0002.wav``,
    ... and every part's own time axis starts at zero. When the earlier parts
    sit next to this one, their durations (from the headers only) give the
    offset. Returns (part_number, offset_seconds) with offset None when the
    earlier parts are not all present, or (None, None) for a non-part file.
    """
    p = Path(os.fspath(path))
    m = _PART_SUFFIX.match(p.stem)
    if not m:
        return None, None
    part = int(m.group("part"))
    if part <= 1:
        return part, 0.0
    offset = 0.0
    for k in range(1, part):
        sibling = p.with_name(f"{m.group('stem')}_{k:04d}{p.suffix}")
        if not sibling.is_file():
            return part, None
        try:
            info = sf.info(str(sibling))
        except Exception:
            return part, None
        if not info.samplerate:
            return part, None
        offset += int(info.frames) / float(info.samplerate)
    return part, offset


def _raw_estimate_seconds(path, rbw):
    """Wall-clock model for chunked STFT + bounded imshow save (no calibration)."""
    info = sf.info(path)
    fs = float(info.samplerate)
    n_samples = int(info.frames)
    file_mb = os.path.getsize(path) / (1024 * 1024)
    n_fft = _n_fft(fs, rbw)
    hop = max(n_fft // 2, 1)
    n_frames = max(1, _n_stft_frames(n_samples, n_fft, hop))
    n_freq_out = _pooled_len(n_fft, MAX_FREQ_BINS)
    n_time_out = min(n_frames, MAX_TIME_BINS)

    fft_ops = n_frames * n_fft * np.log2(max(n_fft, 2))
    stft_sec = fft_ops / 8.5e7
    read_sec = 0.2 + file_mb / 70.0
    smooth_sec = (n_fft * n_frames) / 1.2e8
    render_sec = 0.4 + (n_freq_out * n_time_out) / 2.4e6
    return float(read_sec + stft_sec + smooth_sec + render_sec)


def estimate_conversion_seconds(path, rbw):
    """Wall-clock estimate from file size / RBW, scaled by recent conversions."""
    return max(2.0, _raw_estimate_seconds(path, rbw) * _load_eta_scale())


def wav_info(path):
    """Header-only WAV metadata (no sample payload)."""
    info = sf.info(path)
    fs = float(info.samplerate)
    frames = int(info.frames)
    return {
        "samplerate": fs,
        "frames": frames,
        "channels": int(info.channels),
        "duration": (frames / fs) if fs else 0.0,
        "size": os.path.getsize(path),
    }


def _axis_units(fs):
    """Label baseband span −fs/2 … +fs/2.

    RFIX labels Frequency (Hz), but 125e6 Hz tick labels are unreadable.
    Use MHz for high-rate IQ; fall back to kHz/Hz for narrower captures.
    """
    half = float(fs) / 2.0
    if half >= 1e6:
        return 1e6, "MHz"
    if half >= 1e3:
        return 1e3, "kHz"
    return 1.0, "Hz"


def _fftshift_freqs(n_fft, fs):
    """fftshifted bin-center frequencies (Hz). n_fft is even (power of two)."""
    df = float(fs) / float(n_fft)
    return (np.arange(n_fft, dtype=np.float64) - (n_fft // 2)) * df


def _format_rbw(rbw_hz):
    rbw_hz = float(rbw_hz)
    if rbw_hz >= 1000.0:
        return f"{rbw_hz / 1e3:g} kHz"
    return f"{rbw_hz:g} Hz"


def _to_dbfs_bin(linear_power):
    """10*log10(power+eps). 0 dBFS = complex tone |a|=1 after spectrum scaling."""
    out = np.empty_like(linear_power, dtype=np.float32)
    np.add(linear_power, POWER_EPS, out=out)
    np.log10(out, out=out)
    out *= np.float32(10.0)
    out[~np.isfinite(out)] = np.nan
    return out


def _robust_noise_floor_db(spec_db):
    """20th percentile — DC/edge bins can be −inf, so absolute min is not a noise floor."""
    finite = spec_db[np.isfinite(spec_db)]
    if finite.size == 0:
        return None
    return float(np.percentile(finite, 20.0))


def _annotate_peak_marker(ax, freq_plot, freq_hz, peak_db, x_mid=0.0, freq_text=None):
    ax.axvline(freq_plot, color="#444444", linewidth=0.7, alpha=0.65, zorder=3)
    right_half = freq_plot >= x_mid
    ha = "right" if right_half else "left"
    x_off = -8 if right_half else 8
    if freq_text is None:
        freq_text = f"{freq_hz / 1e6:.4f} MHz"
    # The y-axis ends 4 dB above the peak, so a label placed above the peak
    # left the axes and covered the title (seen on real sensor files). Anchor
    # the label inside the axes, below the stats box, with a line to the peak.
    ax.annotate(
        f"{freq_text}, {peak_db:.2f} dBFS/bin",
        xy=(freq_plot, peak_db),
        xytext=(x_off, 0.66),
        textcoords=("offset points", "axes fraction"),
        ha=ha,
        va="top",
        fontsize=8,
        color=SPECTRUM_COLOR,
        zorder=4,
        bbox={
            "boxstyle": "round,pad=0.25",
            "facecolor": "white",
            "edgecolor": SPECTRUM_COLOR,
            "linewidth": 0.8,
        },
        arrowprops={"arrowstyle": "-", "color": "#444444", "lw": 0.6},
    )


def _annotate_spectrum_stats(ax, peak_db, floor_db):
    if peak_db is not None and np.isfinite(peak_db):
        ax.text(
            0.985,
            0.94,
            f"{peak_db:.2f} dBFS/bin",
            transform=ax.transAxes,
            ha="right",
            va="top",
            color=SPECTRUM_COLOR,
            fontsize=8.5,
            bbox={
                "boxstyle": "round,pad=0.28",
                "facecolor": "white",
                "edgecolor": SPECTRUM_COLOR,
                "linewidth": 1.0,
            },
        )
    if floor_db is not None and np.isfinite(floor_db):
        ax.text(
            0.985,
            0.08,
            f"{floor_db:.2f} dBFS/bin",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            color=FLOOR_COLOR,
            fontsize=8.5,
            bbox={
                "boxstyle": "round,pad=0.28",
                "facecolor": "white",
                "edgecolor": "#888888",
                "linewidth": 1.0,
            },
        )


def _fmt_freq(hz, scale):
    """Exact value in the axis unit, never rounded to a tidy number: 1842.5, 1805, 3.90625."""
    text = f"{float(hz) / float(scale):.9f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_rate(fs):
    fs = float(fs)
    if fs >= 1e6:
        return f"{_fmt_freq(fs, 1e6)} MS/s"
    if fs >= 1e3:
        return f"{_fmt_freq(fs, 1e3)} kS/s"
    return f"{_fmt_freq(fs, 1.0)} S/s"


def _rf_units(centre_hz):
    centre = abs(float(centre_hz))
    if centre >= 1e6:
        return 1e6, "MHz"
    if centre >= 1e3:
        return 1e3, "kHz"
    return 1.0, "Hz"


def _frequency_plan(fs, meta, lookup, markers=True):
    """Axis, marker positions and legend text, from numeric metadata only.

    The axis is absolute RF when the file's centre frequency is verified and
    the offset from the centre otherwise. Markers: the requested start/end
    (a matched request on an absolute axis) and the sensor-reported range
    (when no request was matched, or when it differs from the request). A
    boundary outside the displayed span gets no line, only an edge tag.
    """
    fs = float(fs)
    half = fs / 2.0
    meta = meta if meta is not None else iq_metadata.FileMeta()
    absolute = bool(meta.centre_verified)
    if absolute:
        centre = float(meta.centre_hz)
        scale, unit = _rf_units(centre)
        lo_hz, hi_hz = centre - half, centre + half
    else:
        centre = None
        scale, unit = _axis_units(fs)
        lo_hz, hi_hz = -half, half

    def f(hz):
        return _fmt_freq(hz, scale)

    request = lookup.request if (markers and lookup is not None) else None
    sensor = None
    bw = meta.sensor_bandwidth_hz
    if bw:
        if absolute:
            sensor = (centre - bw / 2.0, centre + bw / 2.0)
        elif meta.centre_hz is None:
            sensor = (-bw / 2.0, bw / 2.0)
    same = bool(
        request is not None
        and sensor is not None
        and absolute
        and iq_metadata.same_hz(request.start_hz, sensor[0])
        and iq_metadata.same_hz(request.stop_hz, sensor[1])
    )
    draw_request = bool(markers and request is not None and absolute)
    draw_sensor = bool(markers and sensor is not None and not (draw_request and same))

    boundaries = []

    def add(kind, start, stop):
        for name, hz in (("start", start), ("end", stop)):
            hz = float(hz)
            if hz < lo_hz - iq_metadata.HZ_TOLERANCE:
                side = "left"
            elif hz > hi_hz + iq_metadata.HZ_TOLERANCE:
                side = "right"
            else:
                side = None
            boundaries.append(
                {"kind": kind, "boundary": name, "hz": hz, "x": hz / scale, "in_view": side is None, "side": side}
            )

    if draw_request:
        add("requested", request.start_hz, request.stop_hz)
    if draw_sensor:
        add("sensor", sensor[0], sensor[1])

    def outside_note(kind):
        out = [b["boundary"] for b in boundaries if b["kind"] == kind and not b["in_view"]]
        if len(out) == 2:
            return " · both boundaries outside the displayed span"
        if out:
            return f" · {out[0]} outside the displayed span"
        return ""

    rate = _fmt_rate(fs)
    legend = []
    log = [f"  Displayed span : {f(fs)} {unit} ({f(lo_hz)} - {f(hi_hz)} {unit}{'' if absolute else ' from centre'}), "
           "= sample rate, not a measured bandwidth"]
    if absolute:
        xlabel = f"Frequency ({unit})"
        log.append(f"  Centre         : {f(centre)} {unit} (file metadata)")
    else:
        why = "centre-frequency metadata not used" if meta.problem else "no centre-frequency metadata"
        xlabel = f"Offset from centre ({unit}) · absolute frequency unknown ({why})"
        log.append(
            f"  Centre         : unknown ({meta.problem or 'no frequency metadata in the file'}); axis is the offset from centre"
        )
    if sensor is not None:
        sbw = sensor[1] - sensor[0]
        where = f"{f(sensor[0])} - {f(sensor[1])} {unit}" if absolute else f"+/-{f(sbw / 2.0)} {unit} around the centre"
        log.append(f"  Sensor-reported: bandwidth {f(sbw)} {unit}, {where} (file metadata)")
    elif meta.problem:
        log.append(f"  Sensor-reported: not used ({meta.problem})")
    else:
        log.append(f"  Sensor-reported: {'not in the file metadata' if meta.present else 'unknown (no frequency metadata in the file)'}")
    if not markers:
        log.append("  Requested range: not looked up (range markers off)")
    elif request is not None:
        rs, ru = _rf_units(request.centre_hz)
        log.append(
            f"  Requested range: {_fmt_freq(request.start_hz, rs)} - {_fmt_freq(request.stop_hz, rs)} {ru}, "
            f"bandwidth {_fmt_freq(request.bandwidth_hz, rs)} {ru} ({request.source}"
            + (f", {request.detail}" if request.detail else "")
            + ")"
        )
    else:
        note = lookup.note if lookup is not None and lookup.note else "no request record"
        log.append(f"  Requested range: not available ({note})")

    if markers:
        if draw_request:
            s, e = float(request.start_hz), float(request.stop_hz)
            text = (
                f"Requested range: start {f(s)} {unit} · end {f(e)} {unit} · bandwidth {f(e - s)} {unit}"
                + outside_note("requested")
            )
            if request.is_window and request.allocation_start_hz is not None:
                text += (
                    f" · capture window {request.window_index or '?'} of {request.window_count} of the requested "
                    f"{f(request.allocation_start_hz)}–{f(request.allocation_stop_hz)} {unit}"
                )
            legend.append(("requested", f"{text}  ({request.source})"))
        elif request is not None:
            rs, ru = _rf_units(request.centre_hz)
            legend.append((
                None,
                f"Requested range {_fmt_freq(request.start_hz, rs)}–{_fmt_freq(request.stop_hz, rs)} {ru} "
                f"({request.source}): not marked, absolute frequency unknown",
            ))
        else:
            note = lookup.note if lookup is not None and lookup.note else "no request record"
            legend.append((None, f"Requested range: not available ({note})"))
        if sensor is not None:
            sbw = sensor[1] - sensor[0]
            if draw_sensor and absolute:
                legend.append((
                    "sensor",
                    f"Sensor-reported range: {f(sensor[0])}–{f(sensor[1])} {unit} · sensor-reported bandwidth "
                    f"{f(sbw)} {unit}" + outside_note("sensor") + "  (file metadata)",
                ))
            elif draw_sensor:
                legend.append((
                    "sensor",
                    f"Sensor-reported bandwidth {f(sbw)} {unit} (file metadata): ±{f(sbw / 2.0)} {unit} "
                    "around the unknown centre",
                ))
            else:
                legend.append((None, f"Sensor-reported bandwidth {f(sbw)} {unit} (file metadata): same range as requested"))
        elif meta.problem:
            legend.append((None, f"Sensor-reported bandwidth: not used ({meta.problem})"))
        elif meta.present:
            legend.append((None, "Sensor-reported bandwidth: not in the file metadata"))
        else:
            legend.append((None, "Sensor-reported bandwidth: unknown (no frequency metadata in the file)"))
        if absolute:
            legend.append((None, f"Centre {f(centre)} {unit} (file metadata) · Sample rate {rate}"))
            legend.append((
                None,
                f"Displayed span {f(fs)} {unit}, {f(lo_hz)}–{f(hi_hz)} {unit} "
                "(= sample rate for complex IQ, not a measured bandwidth)",
            ))
        else:
            what = f"Centre-frequency metadata not used ({meta.problem})" if meta.problem else "Centre frequency unknown"
            legend.append((None, f"{what}: the axis shows the offset from the centre · Sample rate {rate}"))
            legend.append((
                None,
                f"Displayed span {f(fs)} {unit} (= sample rate for complex IQ, not a measured bandwidth)",
            ))
        tail = "do not prove uniform sensitivity, exact filter edges or complete capture."
        req_shown = any(b["kind"] == "requested" and b["in_view"] for b in boundaries)
        sensor_shown = any(b["kind"] == "sensor" and b["in_view"] for b in boundaries)
        if req_shown and sensor_shown:
            legend.append((None, "Dashed: requested. Dotted: sensor-reported. The markers " + tail))
        elif req_shown:
            legend.append((None, "Dashed lines: requested boundaries. They " + tail))
        elif sensor_shown and draw_request:
            legend.append((None, "Dotted lines: sensor-reported range. They " + tail))
        elif sensor_shown:
            legend.append((None, "Dotted lines: sensor-reported range, not a request. They " + tail))
        elif draw_request:
            legend.append((None, "The requested boundaries are outside the displayed span; the tags show their values."))

    tag_rows = []
    for kind in ("requested", "sensor"):
        own = [b for b in boundaries if b["kind"] == kind]
        if not own:
            continue
        label = "Requested" if kind == "requested" else "Sensor-reported"
        specs = []
        for b in own:
            word = f"{label} {b['boundary']}" if kind == "requested" else label
            value = f"{f(b['hz'])} {unit}"
            if b["in_view"]:
                options = ("right", "center", "left") if b["boundary"] == "start" else ("left", "center", "right")
                specs.append({"x": b["x"], "text": f"{word} {value}", "options": options})
            elif b["side"] == "left":
                specs.append(
                    {"x": lo_hz / scale, "text": f"◀ {word} {value} (outside view)", "options": ("left",), "dx": TAG_EDGE_INSET_PT}
                )
            else:
                specs.append(
                    {"x": hi_hz / scale, "text": f"{word} {value} (outside view) ▶", "options": ("right",), "dx": -TAG_EDGE_INSET_PT}
                )
        start = next(b for b in own if b["boundary"] == "start")
        end = next(b for b in own if b["boundary"] == "end")
        merged = f"{label} {f(start['hz'])}–{f(end['hz'])} {unit}"
        outs = [b for b in own if not b["in_view"]]
        if len(outs) == 2 and outs[0]["side"] == outs[1]["side"]:
            merged += " (outside view)"
        elif len(outs) == 1:
            merged += f" ({outs[0]['boundary']} outside view)"
        elif len(outs) == 2:
            merged += " (both outside view)"
        if any(b["side"] == "left" for b in outs):
            merged_spec = {"x": lo_hz / scale, "text": f"◀ {merged}", "options": ("left",), "dx": TAG_EDGE_INSET_PT}
        elif outs:
            merged_spec = {"x": hi_hz / scale, "text": f"{merged} ▶", "options": ("right",), "dx": -TAG_EDGE_INSET_PT}
        else:
            merged_spec = {"x": 0.5 * (start["x"] + end["x"]), "text": merged, "options": ("center", "left", "right")}
        tag_rows.append({"kind": kind, "specs": specs, "merged": merged_spec})

    return {
        "absolute": absolute,
        "centre_hz": centre,
        "scale": scale,
        "unit": unit,
        "lo_hz": lo_hz,
        "hi_hz": hi_hz,
        "x_min": lo_hz / scale,
        "x_max": hi_hz / scale,
        "span_hz": fs,
        "xlabel": xlabel,
        "markers": bool(markers),
        "request": request,
        "sensor_range_hz": sensor,
        "request_matches_sensor": same,
        "draw_request": draw_request,
        "draw_sensor": draw_sensor,
        "boundaries": boundaries,
        "legend": legend,
        "tag_rows": tag_rows,
        "log": log,
    }


def _marker_style(kind):
    from matplotlib import patheffects

    stroke = [patheffects.withStroke(linewidth=MARKER_STROKE_WIDTH, foreground=MARKER_STROKE_COLOR, alpha=0.85)]
    if kind == "requested":
        return {"color": REQUEST_LINE_COLOR, "linestyle": REQUEST_DASHES, "linewidth": MARKER_LINEWIDTH, "path_effects": stroke}
    return {"color": SENSOR_LINE_COLOR, "linestyle": SENSOR_DASHES, "linewidth": MARKER_LINEWIDTH, "path_effects": stroke}


def _tag_style(kind):
    if kind == "requested":
        edge, text, ls = REQUEST_LINE_COLOR, REQUEST_TEXT_COLOR, "--"
    else:
        edge, text, ls = "#444444", SENSOR_TEXT_COLOR, ":"
    return text, {"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": edge, "linewidth": 0.8, "linestyle": ls}


def _place_tag_row(fig, ax, row, row_index, renderer):
    """Put one row of boundary tags just above the axes, inside its width, without overlaps.

    Tries the alignments of every tag in a fixed order, then one merged range
    tag; if nothing fits the row is left out (the legend still has the values).
    Returns (annotations, how) with how in "separate" / "merged" / "dropped".
    """
    import itertools

    text_color, box = _tag_style(row["kind"])
    axbox = ax.get_window_extent(renderer)
    px = fig.dpi / 72.0
    pad = (0.2 * TAG_FONTSIZE + 1.0) * px
    gap = 3.0 * px

    def make(spec):
        ann = ax.annotate(
            spec["text"],
            xy=(spec["x"], 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(spec.get("dx", 0.0), TAG_GAP_PT + row_index * TAG_ROW_PT),
            textcoords="offset points",
            ha=spec["options"][0],
            va="bottom",
            fontsize=TAG_FONTSIZE,
            color=text_color,
            bbox=dict(box),
            annotation_clip=False,
            zorder=6,
        )
        ann.set_clip_on(False)
        ann.set_gid(f"range-tag:{row['kind']}")
        return ann

    def fits(anns):
        boxes = [a.get_window_extent(renderer) for a in anns]
        for b in boxes:
            if b.x0 - pad < axbox.x0 - 0.5 or b.x1 + pad > axbox.x1 + 0.5:
                return False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a.x0 - pad - gap < b.x1 + pad and b.x0 - pad - gap < a.x1 + pad:
                    return False
        return True

    anns = [make(spec) for spec in row["specs"]]
    for combo in itertools.product(*[spec["options"] for spec in row["specs"]]):
        for ann, ha in zip(anns, combo):
            ann.set_horizontalalignment(ha)
        if fits(anns):
            return anns, "separate"
    for ann in anns:
        ann.remove()
    merged = make(row["merged"])
    for ha in row["merged"]["options"]:
        merged.set_horizontalalignment(ha)
        if fits([merged]):
            return [merged], "merged"
    merged.remove()
    return [], "dropped"


def _draw_frequency_ranges(fig, ax_spec, ax_wf, plan, title):
    """Marker lines on both panels, boundary tags above the spectrum, legend under the x label."""
    from matplotlib.lines import Line2D

    info = {"lines": [], "tags": [], "tag_rows": [], "legend": None, "title_pad": TITLE_PAD_PT}
    for b in plan["boundaries"]:
        if not b["in_view"]:
            continue
        style = _marker_style(b["kind"])
        for name, ax in (("spectrum", ax_spec), ("spectrogram", ax_wf)):
            line = ax.axvline(b["x"], zorder=2.6, **style)
            line.set_gid(f"range-line:{b['kind']}:{b['boundary']}")
            info["lines"].append(
                {
                    "kind": b["kind"],
                    "boundary": b["boundary"],
                    "hz": b["hz"],
                    "axes": name,
                    "xdata": [float(v) for v in line.get_xdata()],
                    "color": line.get_color(),
                    "dashes": [float(v) for v in style["linestyle"][1]],
                    "linewidth": float(line.get_linewidth()),
                    "artist": line,
                }
            )
    ax_spec.set_xlim(plan["x_min"], plan["x_max"])
    ax_wf.set_xlim(plan["x_min"], plan["x_max"])

    renderer = fig.canvas.get_renderer()
    rows_used = 0
    for row in plan["tag_rows"]:
        anns, how = _place_tag_row(fig, ax_spec, row, rows_used, renderer)
        info["tag_rows"].append({"kind": row["kind"], "how": how, "texts": [a.get_text() for a in anns]})
        if anns:
            info["tags"].extend(anns)
            rows_used += 1
    if rows_used:
        info["title_pad"] = TITLE_PAD_PT + rows_used * TAG_ROW_PT + 2.0
    ax_spec.set_title(title, pad=info["title_pad"])

    if plan["legend"]:
        import textwrap

        handles, labels = [], []
        for kind, text in plan["legend"]:
            if kind is None:
                handles.append(Line2D([], [], linestyle="none"))
            else:
                handles.append(Line2D([0, 1], [0, 0], **_marker_style(kind)))
            labels.append(textwrap.fill(text, INFO_WRAP_CHARS, subsequent_indent="    ", break_on_hyphens=False))
        below = ax_wf.get_tightbbox(renderer)
        axbox = ax_wf.get_window_extent(renderer)
        top = below.y0 - 6.0 * fig.dpi / 72.0
        legend = fig.legend(
            handles,
            labels,
            loc="upper left",
            bbox_to_anchor=(axbox.x0 / fig.bbox.width, top / fig.bbox.height),
            bbox_transform=fig.transFigure,
            frameon=False,
            fontsize=INFO_FONTSIZE,
            handlelength=3.2,
            handletextpad=0.6,
            borderaxespad=0.0,
            borderpad=0.0,
            labelspacing=0.35,
        )
        legend.set_gid("range-legend")
        info["legend"] = legend
    return info


def _layout_boxes(fig, ax_spec, ax_wf, cbar, drawn):
    """Display-pixel boxes [x0, y0, x1, y1] of the labels, for overlap and clipping checks."""
    import matplotlib

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    def box(b):
        return [float(b.x0), float(b.y0), float(b.x1), float(b.y1)]

    def text_box(t):
        patch = t.get_bbox_patch()
        if patch is not None:
            return box(patch.get_window_extent(renderer))
        return box(t.get_window_extent(renderer))

    tags = [
        {"text": a.get_text(), "ha": a.get_horizontalalignment(), "box": text_box(a)} for a in drawn["tags"]
    ]
    spectrum_texts = [
        {"text": t.get_text(), "box": text_box(t)}
        for t in ax_spec.texts
        if not str(t.get_gid() or "").startswith("range-tag") and t.get_text()
    ]
    ticks = [t.get_window_extent(renderer) for t in ax_wf.get_xticklabels() if t.get_visible() and t.get_text()]
    legend = drawn["legend"]
    tight = fig.get_tightbbox(renderer).padded(float(matplotlib.rcParams["savefig.pad_inches"]))
    dpi = float(fig.dpi)
    return {
        "dpi": dpi,
        "saved": [tight.x0 * dpi, tight.y0 * dpi, tight.x1 * dpi, tight.y1 * dpi],
        "title": box(ax_spec.title.get_window_extent(renderer)),
        "spectrum_axes": box(ax_spec.get_window_extent(renderer)),
        "spectrogram_axes": box(ax_wf.get_window_extent(renderer)),
        "colorbar": box(cbar.ax.get_tightbbox(renderer)),
        "xlabel": box(ax_wf.xaxis.label.get_window_extent(renderer)),
        "xticklabels": [box(t) for t in ticks],
        "legend": box(legend.get_window_extent(renderer)) if legend is not None else None,
        "legend_texts": [
            {"text": t.get_text(), "box": box(t.get_window_extent(renderer))} for t in legend.get_texts()
        ]
        if legend is not None
        else [],
        "tags": tags,
        "spectrum_texts": spectrum_texts,
    }


def frequency_overview(path, recorder_history=None, look_up_request=True):
    """File metadata, request lookup and a one-line summary for the GUI (header + trailer reads only)."""
    info = sf.info(os.fspath(path))
    meta = iq_metadata.read_file_meta(path, float(info.samplerate))
    lookup = iq_metadata.find_request(path, meta, recorder_history) if look_up_request else None

    def mhz(hz):
        return _fmt_freq(hz, 1e6)

    parts = []
    if meta.centre_verified:
        parts.append(f"Centre {mhz(meta.centre_hz)} MHz")
    elif meta.problem:
        parts.append("Centre not used (metadata inconsistent): relative axis")
    else:
        parts.append("Centre unknown (no frequency metadata): relative axis")
    sensor = None
    if meta.centre_verified and meta.sensor_bandwidth_hz:
        half = meta.sensor_bandwidth_hz / 2.0
        sensor = (meta.centre_hz - half, meta.centre_hz + half)
    req = lookup.request if lookup is not None else None
    if lookup is None:
        if sensor:
            parts.append(f"Sensor-reported {mhz(sensor[0])}–{mhz(sensor[1])} MHz")
        parts.append("range markers off")
    elif req is not None:
        parts.append(f"Requested {mhz(req.start_hz)}–{mhz(req.stop_hz)} MHz ({req.source})")
        if sensor and not (iq_metadata.same_hz(sensor[0], req.start_hz) and iq_metadata.same_hz(sensor[1], req.stop_hz)):
            parts.append(f"Sensor-reported {mhz(sensor[0])}–{mhz(sensor[1])} MHz")
        if not meta.centre_verified:
            parts.append("not marked without a centre")
    else:
        if sensor:
            parts.append(f"Sensor-reported {mhz(sensor[0])}–{mhz(sensor[1])} MHz")
        why = {
            iq_metadata.STATUS_NO_RECORDS: "no Recorder records",
            iq_metadata.STATUS_AMBIGUOUS: "several Recorder records disagree",
            iq_metadata.STATUS_CONFLICT: "Recorder records do not match the file",
            iq_metadata.STATUS_UNVERIFIED: "cannot be checked without a centre",
        }.get(lookup.status, "no matching Recorder record")
        parts.append(f"Requested range not available ({why})")
    return {"meta": meta, "lookup": lookup, "text": "  ·  ".join(parts)}


def _normalize_cmap(cmap):
    name = str(cmap or "jet").strip().lower()
    return name if name in ALLOWED_CMAPS else "jet"


def _auto_db_limits(power_db):
    finite = power_db[np.isfinite(power_db)]
    if finite.size < 8:
        return None, None
    vmin, vmax = np.percentile(finite, [2.0, 98.0])
    vmin = float(vmin)
    vmax = float(vmax)
    if not np.isfinite(vmin) or not np.isfinite(vmax):
        return None, None
    if vmax < vmin:
        vmin, vmax = vmax, vmin
    if vmax - vmin < 8.0:
        mid = 0.5 * (vmin + vmax)
        vmin, vmax = mid - 4.0, mid + 4.0
    pad = max(0.5, 0.02 * (vmax - vmin))
    return vmin - pad, vmax + pad


def _write_denied(exc):
    err = str(exc).lower()
    winerr = getattr(exc, "winerror", None)
    return isinstance(exc, PermissionError) or winerr in (5, 32) or "denied" in err or "being used" in err


def _save_spectrogram_png(fig, output_dir, out_name, cancel_event=None):
    """Render to a temporary file in the output folder, then move it into place.

    A cancelled or failed render never leaves a half-written PNG that looks
    finished, and an existing PNG is replaced only once the new one is complete.
    A cancel that arrives while the PNG is being encoded is still honoured: the
    temp file is removed and nothing is moved into place.
    """
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, out_name)
    # Short temp name: a 200+ character stem must not push the temp path past MAX_PATH.
    tmp_path = os.path.join(output_dir, f".iq_{os.getpid()}_{time.time_ns() % 1_000_000}.part.png")
    try:
        fig.savefig(tmp_path, dpi=SAVE_DPI, bbox_inches="tight")
        if cancel_event is not None and cancel_event.is_set():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise ConversionCancelled("Conversion cancelled.")
        os.replace(tmp_path, out_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        if _write_denied(exc):
            raise PermissionError(
                f"Cannot write {out_name}. Close the PNG if it is open in another app, "
                "check that the output folder is not read-only, then try again."
            ) from exc
        raise
    return out_path


def _thumbnail_rgb(power_db, cmap, vmin, vmax, size):
    """(height, width, 3) uint8 image of the display grid: frequency left to right, time upwards.

    Peak-hold pooling down to about twice the size, then an area average, so a
    narrow carrier stays visible at thumbnail size.
    """
    import matplotlib
    from matplotlib.colors import Normalize
    from PIL import Image

    width, height = (int(v) for v in size)
    if width < 8 or height < 8:
        raise ValueError("Thumbnail size must be at least 8 x 8 pixels.")
    grid = np.asarray(power_db, dtype=np.float32)  # (frequency, time)
    f_factor = grid.shape[0] // (2 * width)
    if f_factor > 1:
        grid = _max_pool_axis0(grid, grid.shape[0] // f_factor)
    t_factor = grid.shape[1] // (2 * height)
    if t_factor > 1:
        grid = _max_pool_axis0(np.ascontiguousarray(grid.T), grid.shape[1] // t_factor).T
    finite = grid[np.isfinite(grid)]
    lo = float(vmin) if vmin is not None else (float(finite.min()) if finite.size else 0.0)
    hi = float(vmax) if vmax is not None else (float(finite.max()) if finite.size else lo + 1.0)
    image = Image.fromarray(np.ascontiguousarray(grid.T[::-1], dtype=np.float32))
    values = np.asarray(image.resize((width, height), Image.Resampling.BOX), dtype=np.float32)
    rgba = matplotlib.colormaps[cmap](Normalize(vmin=lo, vmax=hi, clip=True)(values), bytes=True)
    return np.ascontiguousarray(rgba[..., :3])


def _save_thumbnail_png(rgb, output_dir, name, cancel_event=None):
    """Palette PNG of the thumbnail, written to a temp file and moved into place like the full PNG."""
    from PIL import Image

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, name)
    tmp_path = os.path.join(output_dir, f".iq_{os.getpid()}_{time.time_ns() % 1_000_000}.thumb.part.png")
    try:
        image = Image.fromarray(rgb).quantize(colors=THUMBNAIL_COLORS, dither=Image.Dither.NONE)
        image.save(tmp_path, format="PNG", optimize=True)
        if cancel_event is not None and cancel_event.is_set():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise ConversionCancelled("Conversion cancelled.")
        os.replace(tmp_path, out_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        if _write_denied(exc):
            raise PermissionError(
                f"Cannot write {name}. Close the PNG if it is open in another app, "
                "check that the output folder is not read-only, then try again."
            ) from exc
        raise
    return out_path


def validate_rbw(rbw):
    """Return rbw as a positive finite float or raise ValueError with a user message."""
    try:
        value = float(rbw)
    except (TypeError, ValueError):
        raise ValueError("RBW must be a positive number (Hz).") from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("RBW must be a positive number (Hz).")
    return value


def process_file(
    path,
    output_dir,
    rbw,
    progress_callback=None,
    cmap="jet",
    freq_smooth=False,
    db_auto=True,
    db_vmin=None,
    db_vmax=None,
    cancel_event=None,
    output_name=None,
    stats=None,
    range_markers=True,
    recorder_history=None,
    thumbnail_name=None,
    thumbnail_size=None,
    title_centre=False,
):
    """Convert one stereo IQ WAV to <output_dir>/<stem>.png and return the PNG path.

    cancel_event: optional threading.Event. When set, the chunk loop stops and
    ConversionCancelled is raised; no PNG is written.
    output_name: optional PNG file name (default <input stem>.png). The GUI
    uses this so two inputs with the same name do not overwrite each other.
    stats: optional dict. When given it is filled with the numbers behind the
    image (fs, n_fft, frames, true RBW, peak/floor dBFS, full-resolution
    spectra and the pooled waterfall grid) so tests can check the DSP against
    independent expectations instead of inspecting pixels, plus the frequency
    axis, the marker lines read back from the axes and the label boxes.
    range_markers: mark the requested (or else sensor-reported) range and
    explain the axis under the plot. The frequency axis is absolute RF
    whenever the file's centre frequency is verified, either way.
    recorder_history: CRFS IQ Recorder history file to look requests up in;
    None uses IQ_RECORDER_HISTORY or this PC's Recorder (if installed).
    thumbnail_name: also write <output_dir>/<thumbnail_name>, the spectrogram
    alone (no axes, text or markers) at thumbnail_size (width, height) pixels,
    default THUMBNAIL_SIZE; its path goes to stats["thumbnail_path"].
    title_centre: put the centre frequency (or "centre unknown") in the title.
    None of this changes the samples, the FFT or any computed value.
    """

    def report(fraction, message):
        if progress_callback is not None:
            progress_callback(max(0.0, min(1.0, fraction)), message)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ConversionCancelled("Conversion cancelled.")

    rbw = validate_rbw(rbw)
    path = os.fspath(path)
    output_dir = os.fspath(output_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input file not found:\n{path}")
    out_name = str(output_name).strip() if output_name else Path(path).stem + ".png"
    if not out_name.lower().endswith(".png"):
        out_name += ".png"
    thumb_name = str(thumbnail_name).strip() if thumbnail_name else None
    if thumb_name:
        if not thumb_name.lower().endswith(".png"):
            thumb_name += ".png"
        if thumb_name.casefold() == out_name.casefold():
            raise ValueError("The thumbnail needs a file name of its own.")
    thumb_size = tuple(thumbnail_size) if thumbnail_size else THUMBNAIL_SIZE

    t0 = time.perf_counter()
    try:
        raw_est = _raw_estimate_seconds(path, rbw)
    except Exception:
        raw_est = 0.0

    cmap = _normalize_cmap(cmap)
    freq_smooth = bool(freq_smooth)
    db_auto = bool(db_auto)

    check_cancel()
    report(0.02, "Opening WAV…")
    try:
        snd = sf.SoundFile(path)
    except Exception as exc:
        raise ValueError(
            f"Could not read this WAV file (it may be truncated or invalid):\n{exc}"
        ) from exc

    with snd:
        fs = float(snd.samplerate)
        n_samples = int(len(snd))
        n_channels = int(snd.channels)

        duration_s = n_samples / fs if fs else 0.0
        file_size_kb = os.path.getsize(path) / 1024
        n_fft = _n_fft(fs, rbw)
        hop = max(n_fft // 2, 1)
        n_frames = _n_stft_frames(n_samples, n_fft, hop)

        # Validate before allocating anything sized by n_fft. A 1 Hz RBW on a
        # 125 MHz recording would otherwise build a 2 GB window only to fail.
        if n_channels < 2:
            raise ValueError("Need stereo IQ (I left, Q right).")
        if n_samples < 1:
            raise ValueError("WAV file has no samples.")
        if n_fft > MAX_N_FFT:
            min_rbw = 2.0 * fs / MAX_N_FFT
            raise ValueError(
                f"RBW {_format_rbw(rbw)} is too fine for a {fs:,.0f} Hz recording "
                f"(FFT would need {n_fft:,} points). Use an RBW of at least {_format_rbw(min_rbw)}."
            )
        if n_frames < 1:
            raise ValueError(
                f"File is too short for this RBW (need at least {n_fft:,} samples, have {n_samples:,})."
            )
        float_samples = str(getattr(snd, "subtype", "") or "").upper().startswith(("FLOAT", "DOUBLE"))

        n_freq_out = _pooled_len(n_fft, MAX_FREQ_BINS)
        n_time_out = max(1, min(n_frames, MAX_TIME_BINS))
        read_size = _block_sample_count(n_fft, hop)
        window, win_scale, enbw_bins = _blackmanharris_window(n_fft)
        df_hz = fs / n_fft
        rbw_true = float(enbw_bins) * df_hz

        part_no, part_offset_s = part_time_offset(path)

        # Frequency facts never stop a conversion: unreadable metadata or
        # records just leave the axis relative and the request unavailable.
        try:
            file_meta = iq_metadata.read_file_meta(path, fs)
        except Exception:
            file_meta = iq_metadata.FileMeta()
        lookup = None
        if range_markers:
            try:
                lookup = iq_metadata.find_request(path, file_meta, recorder_history)
            except Exception as exc:
                lookup = iq_metadata.RequestLookup(
                    None, iq_metadata.STATUS_NOT_FOUND, f"request lookup failed: {exc.__class__.__name__}"
                )
        plan = _frequency_plan(fs, file_meta, lookup, markers=bool(range_markers))

        print(f"--- {Path(path).name} ---")
        print(f"  Sample rate    : {fs:,.0f} Hz ({_fmt_rate(fs)})")
        print(f"  Samples        : {n_samples:,}")
        print(f"  Duration       : {duration_s:.3f} s")
        if part_no is not None and part_no > 1:
            if part_offset_s is None:
                print(f"  Split part     : {part_no} (earlier parts not found; time axis starts at 0)")
            else:
                print(f"  Split part     : {part_no}, starts {part_offset_s:.3f} s into the recording")
        for line in plan["log"]:
            print(line)
        print(f"  RBW requested  : {rbw / 1e3:.3f} kHz")
        print(f"  RBW (ENBW)     : {rbw_true / 1e3:.3f} kHz  ({enbw_bins:.3f} bins x {df_hz / 1e3:.3f} kHz)")
        print(f"  File size      : {file_size_kb:.1f} kB")
        print(f"  FFT size       : {n_fft:,}")
        print(f"  Display grid   : {n_freq_out} × {n_time_out}")
        print(f"  Colormap       : {cmap}")
        print(f"  Freq smooth    : {'on' if freq_smooth else 'off'}")

        # Metadata for axes — extent uses these, not the pooled display shape.
        axis_meta = {
            "fs": fs,
            "duration_s": duration_s,
            "part_no": part_no,
            "part_offset_s": part_offset_s,
            "rbw": float(rbw),
            "rbw_true": rbw_true,
            "n_fft": n_fft,
        }

        report(0.03, "Computing STFT…")
        acc = np.zeros((n_freq_out, n_time_out), dtype=np.float32)
        sum_1d = np.zeros(n_fft, dtype=np.float64)
        max_1d = np.zeros(n_fft, dtype=np.float32)
        leftover = np.empty((0, n_channels), dtype=np.float32)
        frames_done = 0

        while True:
            check_cancel()
            remaining = n_samples - snd.tell()
            if remaining > 0:
                chunk = snd.read(min(read_size, remaining), dtype="float32", always_2d=True)
                if float_samples and chunk is not None and chunk.shape[0] and not np.isfinite(chunk).all():
                    # A single NaN or Inf sample in a float WAV would turn every
                    # bin of its frame, and so the max-hold spectrum, into NaN.
                    np.nan_to_num(chunk, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                if chunk is None or chunk.shape[0] == 0:
                    remaining = 0
                    block = leftover
                    leftover = np.empty((0, n_channels), dtype=np.float32)
                    if block.shape[0] < n_fft:
                        break
                elif leftover.shape[0]:
                    block = np.concatenate((leftover, chunk), axis=0)
                    del chunk
                else:
                    block = chunk
            else:
                block = leftover
                leftover = np.empty((0, n_channels), dtype=np.float32)
                if block.shape[0] < n_fft:
                    break

            n_blk_frames = _n_stft_frames(block.shape[0], n_fft, hop)
            if n_blk_frames < 1:
                leftover = np.array(block, dtype=np.float32, copy=True)
                if remaining <= 0:
                    break
                continue

            used = n_blk_frames * hop
            leftover = np.array(block[used:], dtype=np.float32, copy=True)
            stft_samples = (n_blk_frames - 1) * hop + n_fft
            iq = _iq_from_stereo(block[:stft_samples])
            del block

            power = _stft_power(iq, n_fft, hop, window, win_scale)
            del iq
            if freq_smooth:
                power = _smooth_frequency(power)
            power = np.fft.fftshift(power, axes=0)
            sum_1d += power.sum(axis=1, dtype=np.float64)
            np.maximum(max_1d, power.max(axis=1), out=max_1d)
            power = _max_pool_axis0(power, n_freq_out)

            idx = (
                np.arange(frames_done, frames_done + power.shape[1], dtype=np.int64) * n_time_out
            ) // n_frames
            np.clip(idx, 0, n_time_out - 1, out=idx)
            np.maximum.at(acc.T, idx, power.T)
            frames_done += power.shape[1]
            del power

            frac = 0.03 + 0.82 * (frames_done / max(n_frames, 1))
            report(frac, f"Computing STFT… {frames_done:,}/{n_frames:,} frames")

            if remaining <= 0 and leftover.shape[0] < n_fft:
                break

        del leftover, window

    if frames_done < 1:
        raise ValueError("No STFT frames were produced from this file.")

    check_cancel()
    report(0.86, "Scaling power…")
    # Full-resolution Welch mean and max-hold (not the downsampled display grid).
    mean_lin = (sum_1d / max(frames_done, 1)).astype(np.float32)
    del sum_1d
    peak_lin = max_1d
    del max_1d
    mean_db = _to_dbfs_bin(mean_lin)
    peak_db = _to_dbfs_bin(peak_lin)
    del peak_lin, mean_lin

    power_db = np.empty_like(acc, dtype=np.float32)
    np.add(acc, POWER_EPS, out=power_db)
    del acc
    np.log10(power_db, out=power_db)
    power_db *= np.float32(10.0)
    gc.collect()

    if db_auto:
        vmin, vmax = _auto_db_limits(power_db)
    else:
        vmin = float(db_vmin) if db_vmin is not None and str(db_vmin).strip() != "" else None
        vmax = float(db_vmax) if db_vmax is not None and str(db_vmax).strip() != "" else None
        if vmin is not None and vmax is not None and vmax <= vmin:
            vmin, vmax = vmax, vmin

    check_cancel()
    report(0.90, "Rendering spectrum + spectrogram…")
    plt = _plt()
    fig = None
    out_path = None
    f_scale, f_unit = plan["scale"], plan["unit"]
    x_min, x_max = plan["x_min"], plan["x_max"]
    duration_s = float(axis_meta["duration_s"])
    part_no = axis_meta.get("part_no")
    part_offset_s = axis_meta.get("part_offset_s")
    # A split part's own clock starts at 0; show where it sits in the recording.
    t_start = float(part_offset_s) if part_offset_s else 0.0
    part_label = ""
    if part_no is not None and part_no > 1:
        if part_offset_s is None:
            part_label = f"  ·  part {part_no} (earlier parts not found)"
        else:
            part_label = f"  ·  part {part_no} from {part_offset_s:.2f} s"
    rbw_req = float(axis_meta["rbw"])
    rbw_true = float(axis_meta["rbw_true"])
    rbw_true_label = _format_rbw(rbw_true)
    if abs(rbw_true - rbw_req) / max(rbw_req, 1.0) > 0.02:
        rbw_label = f"{rbw_true_label}  ·  requested {_format_rbw(rbw_req)}"
    else:
        rbw_label = rbw_true_label
    freqs_hz = _fftshift_freqs(int(axis_meta["n_fft"]), axis_meta["fs"])
    if plan["absolute"]:
        freqs = (plan["centre_hz"] + freqs_hz) / f_scale
    else:
        freqs = freqs_hz / f_scale
    if np.isfinite(peak_db).any():
        peak_idx = int(np.nanargmax(peak_db))
        peak_val = float(peak_db[peak_idx])
        peak_freq_hz = float(freqs_hz[peak_idx])
    else:
        peak_idx = None
        peak_val = None
        peak_freq_hz = None
    floor_val = _robust_noise_floor_db(mean_db)
    if stats is not None:
        stats.update(
            {
                "fs": float(axis_meta["fs"]),
                "n_samples": int(n_samples),
                "duration_s": duration_s,
                "part_no": part_no,
                "time_offset_s": t_start,
                "n_fft": int(axis_meta["n_fft"]),
                "hop": int(hop),
                "n_frames": int(n_frames),
                "frames_done": int(frames_done),
                "df_hz": float(axis_meta["fs"]) / float(axis_meta["n_fft"]),
                "enbw_bins": float(enbw_bins),
                "rbw_requested_hz": rbw_req,
                "rbw_true_hz": rbw_true,
                "freqs_hz": freqs_hz.copy(),
                "spectrum_mean_db": mean_db.copy(),
                "spectrum_peak_db": peak_db.copy(),
                "waterfall_db": power_db.copy(),
                "peak_db": peak_val,
                "peak_freq_hz": peak_freq_hz,
                "floor_db": floor_val,
                "vmin": vmin,
                "vmax": vmax,
                "file_meta": file_meta.as_dict(),
                "request_lookup": lookup.as_dict() if lookup is not None else None,
                "frequency_axis": {
                    "absolute": plan["absolute"],
                    "centre_hz": plan["centre_hz"],
                    "unit": f_unit,
                    "scale": f_scale,
                    "x_min": x_min,
                    "x_max": x_max,
                    "span_hz": plan["span_hz"],
                    "xlabel": plan["xlabel"],
                },
                "range_boundaries": [dict(b) for b in plan["boundaries"]],
                "range_legend": [text for _kind, text in plan["legend"]],
                "request_matches_sensor": plan["request_matches_sensor"],
                "peak_rf_hz": (plan["centre_hz"] + peak_freq_hz)
                if plan["absolute"] and peak_freq_hz is not None
                else None,
            }
        )
    centre_label = ""
    if title_centre:
        if plan["absolute"]:
            centre_label = f"  ·  centre {_fmt_freq(plan['centre_hz'], f_scale)} {f_unit}"
        else:
            centre_label = "  ·  centre unknown"
    title = f"{Path(path).name}{centre_label}  ·  RBW {rbw_label}{part_label}"
    if stats is not None:
        stats["title"] = title
    thumb_path = None
    try:
        fig, (ax_spec, ax_wf) = plt.subplots(
            2,
            1,
            figsize=FIGSIZE,
            dpi=SAVE_DPI,
            sharex=True,
            gridspec_kw={
                "height_ratios": [SPECTRUM_HEIGHT_RATIO, WATERFALL_HEIGHT_RATIO],
                "hspace": 0.06,
            },
        )

        finite_mean = mean_db[np.isfinite(mean_db)]
        finite_peak = peak_db[np.isfinite(peak_db)]
        ax_spec.plot(
            freqs,
            peak_db,
            color=SPECTRUM_HOLD_COLOR,
            linewidth=0.65,
            alpha=0.75,
            zorder=1,
        )
        ax_spec.plot(
            freqs,
            mean_db,
            color=SPECTRUM_COLOR,
            linewidth=0.95,
            zorder=2,
        )
        ax_spec.set_xlim(x_min, x_max)
        if finite_mean.size or finite_peak.size:
            y_lo_src = finite_mean if finite_mean.size else finite_peak
            y_hi_src = finite_peak if finite_peak.size else finite_mean
            y_lo = float(np.percentile(y_lo_src, 1.0)) - 4.0
            y_hi = float(np.max(y_hi_src)) + 4.0
            if y_hi - y_lo < 8.0:
                mid = 0.5 * (y_lo + y_hi)
                y_lo, y_hi = mid - 4.0, mid + 4.0
            ax_spec.set_ylim(y_lo, y_hi)
        ax_spec.set_facecolor(SPECTRUM_FACE)
        for spine in ax_spec.spines.values():
            spine.set_color("black")
        ax_spec.set_ylabel("Bin Power (dBFS/bin)")
        ax_spec.set_title(title)
        ax_spec.tick_params(labelbottom=False)
        ax_spec.grid(False)
        if peak_idx is not None and np.isfinite(peak_val) and np.isfinite(peak_freq_hz):
            if plan["absolute"]:
                peak_text = f"{(plan['centre_hz'] + peak_freq_hz) / 1e6:.4f} MHz"
            else:
                peak_text = f"{peak_freq_hz / 1e6:+.4f} MHz from centre"
            _annotate_peak_marker(
                ax_spec, freqs[peak_idx], peak_freq_hz, peak_val, x_mid=0.5 * (x_min + x_max), freq_text=peak_text
            )
        _annotate_spectrum_stats(ax_spec, peak_val, floor_val)

        im = ax_wf.imshow(
            power_db.T,
            cmap=cmap,
            origin="lower",
            aspect="auto",
            interpolation="nearest",
            extent=[x_min, x_max, t_start, t_start + duration_s],
            vmin=vmin,
            vmax=vmax,
        )
        ax_wf.set_xlim(x_min, x_max)
        ax_wf.set_ylim(t_start, t_start + duration_s)
        ax_wf.set_xlabel(plan["xlabel"])
        ax_wf.set_ylabel("Time (s)")
        # Absolute MHz must read as 1842.5, not as an offset like "+1.8e3".
        ax_wf.ticklabel_format(axis="x", style="plain", useOffset=False)
        cbar = fig.colorbar(im, ax=[ax_spec, ax_wf], fraction=0.046, pad=0.04)
        cbar.set_label("dBFS/bin")
        drawn = _draw_frequency_ranges(fig, ax_spec, ax_wf, plan, title)
        if stats is not None:
            stats["waterfall_image_is_data"] = bool(
                im.get_array().shape == power_db.T.shape and np.array_equal(np.asarray(im.get_array()), power_db.T)
            )
            stats["range_lines"] = [{k: v for k, v in line.items() if k != "artist"} for line in drawn["lines"]]
            stats["range_tag_rows"] = drawn["tag_rows"]
            stats["layout"] = _layout_boxes(fig, ax_spec, ax_wf, cbar, drawn)
        thumb_rgb = _thumbnail_rgb(power_db, cmap, vmin, vmax, thumb_size) if thumb_name else None
        del power_db, peak_db, mean_db
        check_cancel()
        report(0.95, "Saving PNG…")
        out_path = _save_spectrogram_png(fig, output_dir, out_name, cancel_event)
        print(f"  Saved          : {out_name}")
        print("  Layout         : spectrum + spectrogram")
        if thumb_rgb is not None:
            thumb_path = _save_thumbnail_png(thumb_rgb, output_dir, thumb_name, cancel_event)
            print(f"  Thumbnail      : {thumb_name} ({thumb_rgb.shape[1]} x {thumb_rgb.shape[0]} px, spectrogram only)")
        marked = [b for b in plan["boundaries"]]
        if marked:
            parts = []
            for b in marked:
                where = "" if b["in_view"] else " (outside view, not drawn)"
                parts.append(f"{b['kind']} {b['boundary']} {_fmt_freq(b['hz'], f_scale)} {f_unit}{where}")
            print("  Range markers  : " + "; ".join(parts))
        elif range_markers:
            print("  Range markers  : none (nothing to mark on this axis)")
        if peak_val is not None and peak_freq_hz is not None:
            if plan["absolute"]:
                print(f"  Peak           : {peak_val:.2f} dBFS/bin @ {(plan['centre_hz'] + peak_freq_hz) / 1e6:.4f} MHz")
            else:
                print(f"  Peak           : {peak_val:.2f} dBFS/bin @ {peak_freq_hz / 1e6:+.4f} MHz from centre")
        if floor_val is not None:
            print(f"  Noise floor    : {floor_val:.2f} dBFS/bin")
        if vmin is not None and vmax is not None:
            print(f"  dB scale       : {vmin:.1f} … {vmax:.1f}")
    finally:
        if fig is not None:
            plt.close(fig)
        plt.close("all")
        gc.collect()

    _update_eta_calibration(raw_est, time.perf_counter() - t0)
    if stats is not None:
        stats["out_path"] = out_path
        stats["thumbnail_path"] = thumb_path
    report(1.0, "Done")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IQ data spectrogram generator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder", help="Folder containing .wav files")
    group.add_argument("--input", help="Single .wav file to process")
    parser.add_argument("--output", default=None, help="Output folder (default: <folder>_output or current directory)")
    parser.add_argument("--rbw", type=float, default=15000, help="Resolution bandwidth in Hz (default: 15000)")
    parser.add_argument("--cmap", default="jet", help="Colormap: jet, turbo, viridis, gray (default: jet)")
    parser.add_argument("--freq-smooth", action="store_true", help="Enable 5-tap frequency smoothing (off by default)")
    parser.add_argument("--no-freq-smooth", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db-min", type=float, default=None, help="Colorbar vmin in dB (implies manual scale)")
    parser.add_argument("--db-max", type=float, default=None, help="Colorbar vmax in dB (implies manual scale)")
    parser.add_argument(
        "--recorder-history",
        default=None,
        help=f"CRFS IQ Recorder history file for the requested ranges (default: ${iq_metadata.HISTORY_ENV} "
        "or this PC's Recorder)",
    )
    parser.add_argument("--no-range-markers", action="store_true", help="Do not mark requested / sensor-reported ranges")
    parser.add_argument(
        "--thumbnail",
        action="store_true",
        help=f"Also write <stem>.thumb.png: the spectrogram alone, {THUMBNAIL_SIZE[0]}x{THUMBNAIL_SIZE[1]} px",
    )
    parser.add_argument("--title-centre", action="store_true", help="Show the centre frequency in the title")
    args = parser.parse_args()

    db_auto = args.db_min is None and args.db_max is None

    def _run(wav_path, out_dir):
        process_file(
            str(wav_path),
            out_dir,
            args.rbw,
            cmap=args.cmap,
            freq_smooth=bool(args.freq_smooth) and not args.no_freq_smooth,
            db_auto=db_auto,
            db_vmin=args.db_min,
            db_vmax=args.db_max,
            range_markers=not args.no_range_markers,
            recorder_history=args.recorder_history,
            thumbnail_name=f"{Path(wav_path).stem}.thumb.png" if args.thumbnail else None,
            title_centre=args.title_centre,
        )

    if args.input:
        input_path = Path(args.input)
        if not input_path.is_file():
            print(f"File not found: {args.input}")
            exit(1)
        output_dir = args.output or "."
        os.makedirs(output_dir, exist_ok=True)
        _run(input_path, output_dir)
    else:
        folder = args.folder
        output_dir = args.output or (folder.rstrip("/\\") + "_output")
        os.makedirs(output_dir, exist_ok=True)

        try:
            wav_files = sorted(
                p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() == ".wav"
            )
        except OSError:
            wav_files = []
        if not wav_files:
            print(f"No .wav files found in {folder}")
            exit(1)

        print(f"Found {len(wav_files)} .wav file(s). Saving results to {output_dir}/")
        failed = []
        for wav_path in wav_files:
            try:
                _run(wav_path, output_dir)
            except Exception as exc:
                print(f"FAILED {wav_path.name}: {exc}")
                failed.append(wav_path.name)
        if failed:
            print(f"Finished with {len(failed)} failure(s): {', '.join(failed)}")
            raise SystemExit(1)
