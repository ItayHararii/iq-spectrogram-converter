import argparse
import gc
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.fft import fft
from scipy.signal import get_window
import soundfile as sf


# Display grid cap: PNG is 12x8 in at 125 dpi. Full STFT (e.g. 32k x 9k)
# does not need to sit in RAM or be sent to the renderer.
MAX_FREQ_BINS = 2048
MAX_TIME_BINS = 1536
SMOOTH_TAPS = 5
FIGSIZE = (12.0, 8.0)
SPECTRUM_HEIGHT_RATIO = 1
WATERFALL_HEIGHT_RATIO = 3
SAVE_DPI = 125
ALLOWED_CMAPS = ("jet", "turbo", "viridis", "gray")
SPECTRUM_COLOR = "#1f77b4"
SPECTRUM_HOLD_COLOR = "#e07a3d"
SPECTRUM_FACE = "#f7f7f7"
FLOOR_COLOR = "#666666"
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
    win64 = np.asarray(get_window("blackmanharris", n_fft, fftbins=True), dtype=np.float64)
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
    spec = fft(windowed, n=n_fft, axis=1, workers=workers, overwrite_x=True)
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


def _annotate_peak_marker(ax, freq_plot, freq_hz, peak_db):
    ax.axvline(freq_plot, color="#444444", linewidth=0.7, alpha=0.65, zorder=3)
    ha = "right" if freq_plot >= 0 else "left"
    x_off = -8 if freq_plot >= 0 else 8
    ax.annotate(
        f"{freq_hz / 1e6:.4f} MHz, {peak_db:.2f} dBFS/bin",
        xy=(freq_plot, peak_db),
        xytext=(x_off, 8),
        textcoords="offset points",
        ha=ha,
        va="bottom",
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


def _save_spectrogram_png(fig, output_dir, out_name):
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, out_name)
    try:
        fig.savefig(out_path, dpi=SAVE_DPI, bbox_inches="tight")
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot write {out_name}. Close the PNG if it is open in another app, then try again."
        ) from exc
    except OSError as exc:
        err = str(exc).lower()
        winerr = getattr(exc, "winerror", None)
        if winerr in (5, 32) or "denied" in err or "being used" in err:
            raise PermissionError(
                f"Cannot write {out_name}. Close the PNG if it is open in another app, then try again."
            ) from exc
        raise


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
):
    def report(fraction, message):
        if progress_callback is not None:
            progress_callback(max(0.0, min(1.0, fraction)), message)

    t0 = time.perf_counter()
    try:
        raw_est = _raw_estimate_seconds(path, rbw)
    except Exception:
        raw_est = 0.0

    cmap = _normalize_cmap(cmap)
    freq_smooth = bool(freq_smooth)
    db_auto = bool(db_auto)

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
        bandwidth_hz = fs
        file_size_kb = os.path.getsize(path) / 1024
        n_fft = _n_fft(fs, rbw)
        hop = max(n_fft // 2, 1)
        n_frames = _n_stft_frames(n_samples, n_fft, hop)
        n_freq_out = _pooled_len(n_fft, MAX_FREQ_BINS)
        n_time_out = max(1, min(n_frames, MAX_TIME_BINS)) if n_frames else 1
        read_size = _block_sample_count(n_fft, hop)
        window, win_scale, enbw_bins = _blackmanharris_window(n_fft)
        df_hz = fs / n_fft
        rbw_true = float(enbw_bins) * df_hz

        print(f"--- {Path(path).name} ---")
        print(f"  Sample rate    : {fs:,.0f} Hz")
        print(f"  Samples        : {n_samples:,}")
        print(f"  Duration       : {duration_s:.3f} s")
        print(f"  Bandwidth      : {bandwidth_hz / 1e6:.3f} MHz")
        print(f"  RBW requested  : {rbw / 1e3:.3f} kHz")
        print(f"  RBW (ENBW)     : {rbw_true / 1e3:.3f} kHz  ({enbw_bins:.3f} bins x {df_hz / 1e3:.3f} kHz)")
        print(f"  File size      : {file_size_kb:.1f} kB")
        print(f"  FFT size       : {n_fft:,}")
        print(f"  Display grid   : {n_freq_out} × {n_time_out}")
        print(f"  Colormap       : {cmap}")
        print(f"  Freq smooth    : {'on' if freq_smooth else 'off'}")

        if n_channels < 2:
            raise ValueError("Need stereo IQ (I left, Q right).")
        if n_samples < 1:
            raise ValueError("WAV file has no samples.")
        if n_frames < 1:
            raise ValueError(
                f"File is too short for this RBW (need at least {n_fft:,} samples, have {n_samples:,})."
            )

        # Metadata for axes — extent uses these, not the pooled display shape.
        axis_meta = {
            "fs": fs,
            "duration_s": duration_s,
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
            remaining = n_samples - snd.tell()
            if remaining > 0:
                chunk = snd.read(min(read_size, remaining), dtype="float32", always_2d=True)
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

    report(0.90, "Rendering spectrum + spectrogram…")
    fig = None
    f_scale, f_unit = _axis_units(axis_meta["fs"])
    f_max = (axis_meta["fs"] / 2.0) / f_scale
    duration_s = float(axis_meta["duration_s"])
    rbw_req = float(axis_meta["rbw"])
    rbw_true = float(axis_meta["rbw_true"])
    rbw_true_label = _format_rbw(rbw_true)
    if abs(rbw_true - rbw_req) / max(rbw_req, 1.0) > 0.02:
        rbw_label = f"{rbw_true_label}  ·  requested {_format_rbw(rbw_req)}"
    else:
        rbw_label = rbw_true_label
    freqs_hz = _fftshift_freqs(int(axis_meta["n_fft"]), axis_meta["fs"])
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
    try:
        fig, (ax_spec, ax_wf) = plt.subplots(
            2,
            1,
            figsize=FIGSIZE,
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
        ax_spec.set_xlim(-f_max, f_max)
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
        ax_spec.set_title(f"{Path(path).name}  ·  RBW {rbw_label}")
        ax_spec.tick_params(labelbottom=False)
        ax_spec.grid(False)
        if peak_idx is not None and np.isfinite(peak_val) and np.isfinite(peak_freq_hz):
            _annotate_peak_marker(ax_spec, freqs[peak_idx], peak_freq_hz, peak_val)
        _annotate_spectrum_stats(ax_spec, peak_val, floor_val)

        im = ax_wf.imshow(
            power_db.T,
            cmap=cmap,
            origin="lower",
            aspect="auto",
            interpolation="nearest",
            extent=[-f_max, f_max, 0.0, duration_s],
            vmin=vmin,
            vmax=vmax,
        )
        ax_wf.set_xlim(-f_max, f_max)
        ax_wf.set_ylim(0.0, duration_s)
        ax_wf.set_xlabel(f"Frequency ({f_unit})")
        ax_wf.set_ylabel("Time (s)")
        cbar = fig.colorbar(im, ax=[ax_spec, ax_wf], fraction=0.046, pad=0.04)
        cbar.set_label("dBFS/bin")
        del power_db, peak_db, mean_db
        out_name = Path(path).stem + ".png"
        report(0.95, "Saving PNG…")
        _save_spectrogram_png(fig, output_dir, out_name)
        print(f"  Saved          : {out_name}")
        print("  Layout         : spectrum + spectrogram")
        if peak_val is not None and peak_freq_hz is not None:
            print(f"  Peak           : {peak_val:.2f} dBFS/bin @ {peak_freq_hz / 1e6:.4f} MHz")
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
    report(1.0, "Done")


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
