"""QA suite for the CRFS IQ Spectrogram Converter.

Runs the engine (iq_data.process_file) and the GUI (iq_gui) against small
synthetic WAV files written to a temporary folder and prints PASS / FAIL per
case. Nothing is written inside the project folder.

    venv\\Scripts\\python.exe qa_run.py
    venv\\Scripts\\python.exe qa_run.py --real-wav "C:\\path\\big.wav"      (chunking proof)
    venv\\Scripts\\python.exe qa_run.py --reference old_iq_data.py        (DSP unchanged)
    venv\\Scripts\\python.exe qa_run.py --exe dist\\IQ_Spectrogram_Converter.exe
    venv\\Scripts\\python.exe qa_run.py --no-gui --report qa_results.json

Exit code is the number of failed cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import iq_data  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HEBREW_FOLDER = "\u05ea\u05d9\u05e7\u05d9\u05d9\u05d4 \u05d1\u05d3\u05d9\u05e7\u05d4"  # "test folder"

RESULTS: list[dict] = []


def convert(*args, **kwargs):
    """process_file with its per-file printout silenced (keeps the QA log readable)."""
    import io
    from contextlib import redirect_stdout

    with redirect_stdout(io.StringIO()):
        return iq_data.process_file(*args, **kwargs)


class Skip(Exception):
    pass


def case(name):
    def wrap(fn):
        fn._qa_name = name
        return fn

    return wrap


def run_case(fn, ctx):
    name = getattr(fn, "_qa_name", fn.__name__)
    t0 = time.perf_counter()
    try:
        note = fn(ctx)
        status = "PASS"
    except Skip as exc:
        status, note = "SKIP", str(exc)
    except AssertionError as exc:
        status, note = "FAIL", str(exc) or "assertion failed"
    except Exception as exc:  # unexpected exception = failure with traceback
        status = "FAIL"
        note = f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc(limit=4)}"
    ms = (time.perf_counter() - t0) * 1000
    RESULTS.append({"name": name, "status": status, "note": str(note or ""), "ms": round(ms)})
    print(f"[{status}] {name} ({ms:.0f} ms)" + (f"  - {str(note).splitlines()[0]}" if note else ""), flush=True)


# ---------------------------------------------------------------------------
# Synthetic WAV helpers
# ---------------------------------------------------------------------------
def write_iq(path, fs=48_000, seconds=0.25, tones=((4_000.0, 0.5),), subtype="PCM_16", noise=0.0, fill=None):
    """Stereo WAV, I left / Q right. tones: (frequency_hz, amplitude). fill: constant array value."""
    n = int(seconds * fs)
    t = np.arange(n, dtype=np.float64) / fs
    iq = np.zeros(n, dtype=np.complex128)
    for f0, a in tones:
        iq += a * np.exp(2j * np.pi * f0 * t)
    if noise:
        rng = np.random.default_rng(7)
        iq += noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    if fill is not None:
        iq[:] = fill
    data = np.column_stack((iq.real, iq.imag)).astype(np.float32)
    if subtype.startswith("PCM"):
        data = np.clip(data, -1.0, 1.0)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, fs, subtype=subtype)
    return fs, n


def append_crfs_meta(path, centre_hz, bandwidth_hz, sample_rate, receiver="rfeye100400"):
    """Trailing CRFS ``meta`` chunk like the sensor writes (centre, sensor-reported bandwidth, sample rate)."""
    xml = (
        f'<?xml version="1.0"?><signalinfo><base><rf_frequency>{centre_hz:g}</rf_frequency>'
        f"<bandwidth>{bandwidth_hz:g}</bandwidth><data_type>complex</data_type>"
        f"<sample_rate>{sample_rate:g}</sample_rate></base>"
        f"<extended><receiver>{receiver}</receiver></extended></signalinfo>"
    ).encode("utf-8")
    pad = b"\x00" if len(xml) % 2 else b""
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        fh.write(b"meta" + struct.pack("<I", len(xml)) + xml + pad)
        size = fh.tell()
        fh.seek(4)
        fh.write(struct.pack("<I", size - 8))


def write_manifest(folder, stem, start_hz, stop_hz):
    data = {
        "schema": 1,
        "kind": "cellular_capture",
        "identifiers": {"recording_stem": stem},
        "allocation": {"requested_start_hz": start_hz, "requested_stop_hz": stop_hz},
    }
    (Path(folder) / f"{stem}.cellular.json").write_text(json.dumps(data), encoding="utf-8")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def leftovers(folder):
    return [p.name for p in Path(folder).glob("*.part.png")] + [p.name for p in Path(folder).glob(".iq_*")]


def peak_spectrum(wav, rbw, mod=iq_data):
    """Mean + max-hold spectra (dBFS/bin) the same way process_file accumulates them."""
    data, fs = sf.read(str(wav), dtype="float32", always_2d=True)
    iq = mod._iq_from_stereo(data)
    n_fft = mod._n_fft(fs, rbw)
    hop = max(n_fft // 2, 1)
    window, scale, enbw = mod._blackmanharris_window(n_fft)
    power = mod._stft_power(iq, n_fft, hop, window, scale)
    power = np.fft.fftshift(power, axes=0)
    mean_db = mod._to_dbfs_bin(power.mean(axis=1).astype(np.float32))
    peak_db = mod._to_dbfs_bin(power.max(axis=1))
    freqs = mod._fftshift_freqs(n_fft, fs)
    return freqs, mean_db, peak_db, n_fft, enbw, fs


# ---------------------------------------------------------------------------
# Engine cases
# ---------------------------------------------------------------------------
@case("single file converts to <stem>.png")
def t_single(ctx):
    wav = ctx.tmp / "single" / "tone.wav"
    write_iq(wav)
    out = ctx.tmp / "single_out"
    result = convert(str(wav), str(out), 500)
    assert Path(result) == out / "tone.png", result
    assert (out / "tone.png").stat().st_size > 5000
    assert not leftovers(out), leftovers(out)
    return "PNG written, no temp files left"


@case("batch of several files")
def t_batch(ctx):
    folder = ctx.tmp / "batch"
    names = []
    for i in range(4):
        wav = folder / f"rec_{i}.wav"
        write_iq(wav, tones=((1_000.0 * (i + 1), 0.4),))
        names.append(wav)
    out = ctx.tmp / "batch_out"
    for wav in names:
        convert(str(wav), str(out), 500)
    pngs = sorted(p.name for p in out.glob("*.png"))
    assert pngs == [f"rec_{i}.png" for i in range(4)], pngs
    return f"{len(pngs)} PNGs"


@case("drag-and-drop string parser (braces, spaces, escapes)")
def t_drop_parser(ctx):
    from iq_gui import parse_drop_paths

    cases = {
        "{C:/My Files/a b.wav} C:/plain.wav": ["C:/My Files/a b.wav", "C:/plain.wav"],
        "{D:/x/y.wav}": ["D:/x/y.wav"],
        "C:/one.wav C:/two.wav": ["C:/one.wav", "C:/two.wav"],
        "{C:/tricky {name}.wav}": ["C:/tricky {name}.wav"],
        "C:/esc\\ aped.wav": ["C:/esc aped.wav"],
        "  {C:/lead space.wav}  ": ["C:/lead space.wav"],
        "{C:/\u05ea\u05d9\u05e7\u05d9\u05d9\u05d4 \u05d1\u05d3\u05d9\u05e7\u05d4/a.wav}": [
            "C:/\u05ea\u05d9\u05e7\u05d9\u05d9\u05d4 \u05d1\u05d3\u05d9\u05e7\u05d4/a.wav"
        ],
        "": [],
    }
    for raw, expected in cases.items():
        got = parse_drop_paths(raw)
        assert got == expected, f"{raw!r} -> {got!r}, expected {expected!r}"
    return f"{len(cases)} strings"


@case("mono WAV is rejected with 'Need stereo IQ'")
def t_mono(ctx):
    wav = ctx.tmp / "mono.wav"
    sf.write(str(wav), np.zeros(4096, dtype=np.float32), 48_000, subtype="PCM_16")
    try:
        convert(str(wav), str(ctx.tmp / "mono_out"), 500)
    except ValueError as exc:
        assert "Need stereo IQ" in str(exc), str(exc)
        assert not (ctx.tmp / "mono_out" / "mono.png").exists()
        return str(exc)
    raise AssertionError("mono file was accepted")


@case("0-byte file gives a clean error")
def t_zero_byte(ctx):
    wav = ctx.tmp / "empty.wav"
    wav.write_bytes(b"")
    try:
        convert(str(wav), str(ctx.tmp / "zero_out"), 500)
    except ValueError as exc:
        assert "Could not read" in str(exc), str(exc)
        return str(exc).splitlines()[0]
    raise AssertionError("0-byte file was accepted")


@case("truncated WAV (data cut) still converts the readable part")
def t_truncated_data(ctx):
    wav = ctx.tmp / "trunc_data.wav"
    write_iq(wav, seconds=0.5)
    raw = wav.read_bytes()
    cut = ctx.tmp / "trunc_cut.wav"
    cut.write_bytes(raw[: len(raw) // 2])
    out = ctx.tmp / "trunc_out"
    try:
        result = convert(str(cut), str(out), 500)
    except ValueError as exc:
        return f"rejected cleanly: {str(exc).splitlines()[0]}"
    assert Path(result).is_file()
    return "converted the readable half"


@case("truncated WAV (header cut) is rejected cleanly")
def t_truncated_header(ctx):
    wav = ctx.tmp / "trunc_header.wav"
    wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    try:
        convert(str(wav), str(ctx.tmp / "trunc_h_out"), 500)
    except ValueError as exc:
        return str(exc).splitlines()[0]
    raise AssertionError("header-truncated file was accepted")


@case("non-WAV data renamed to .wav is rejected")
def t_non_wav(ctx):
    wav = ctx.tmp / "notwav.wav"
    wav.write_bytes(b"This is not audio. " * 500)
    try:
        convert(str(wav), str(ctx.tmp / "notwav_out"), 500)
    except ValueError as exc:
        return str(exc).splitlines()[0]
    raise AssertionError("text file was accepted")


@case("unsupported encoding (MPEG format tag) is rejected")
def t_unsupported_encoding(ctx):
    wav = ctx.tmp / "mp3tag.wav"
    fs, ch, bits = 48_000, 2, 16
    data = b"\x00" * 8192
    fmt = struct.pack("<HHIIHH", 0x0055, ch, fs, fs * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    try:
        convert(str(wav), str(ctx.tmp / "mp3tag_out"), 500)
    except ValueError as exc:
        return str(exc).splitlines()[0]
    raise AssertionError("MPEG-tagged WAV was accepted")


@case("WAV without rf_frequency metadata converts (baseband axis)")
def t_no_metadata(ctx):
    wav = ctx.tmp / "plain.wav"
    n = 48_000 // 4
    t = np.arange(n) / 48_000
    pcm = (np.column_stack((np.cos(2 * np.pi * 3000 * t), np.sin(2 * np.pi * 3000 * t))) * 16000).astype(np.int16)
    with wave.open(str(wav), "wb") as handle:  # plain stdlib WAV: fmt + data only
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48_000)
        handle.writeframes(pcm.tobytes())
    result = convert(str(wav), str(ctx.tmp / "plain_out"), 500)
    assert Path(result).is_file()
    return "converted; centre frequency unknown, axis is -fs/2..+fs/2"


@case("file shorter than one FFT is rejected")
def t_too_short(ctx):
    wav = ctx.tmp / "short.wav"
    sf.write(str(wav), np.zeros((64, 2), dtype=np.float32), 48_000)
    try:
        convert(str(wav), str(ctx.tmp / "short_out"), 500)
    except ValueError as exc:
        assert "too short" in str(exc), str(exc)
        return str(exc)
    raise AssertionError("64-sample file was accepted")


@case("all-zero silence converts without NaN crash")
def t_silence(ctx):
    wav = ctx.tmp / "silence.wav"
    write_iq(wav, fill=0.0)
    result = convert(str(wav), str(ctx.tmp / "silence_out"), 500)
    assert Path(result).is_file()
    return "ok"


@case("clipped +/-1.0 full-scale samples convert")
def t_clipped(ctx):
    wav = ctx.tmp / "clipped.wav"
    n = 48_000 // 4
    sq = np.sign(np.sin(2 * np.pi * 1000 * np.arange(n) / 48_000)).astype(np.float32)
    sf.write(str(wav), np.column_stack((sq, sq)), 48_000, subtype="FLOAT")
    result = convert(str(wav), str(ctx.tmp / "clipped_out"), 500)
    assert Path(result).is_file()
    return "ok"


@case("invalid RBW values (0, negative, text, inf) are rejected; enormous RBW still converts")
def t_bad_rbw(ctx):
    wav = ctx.tmp / "rbw.wav"
    write_iq(wav)
    for bad in (0, -5, "abc", "", None, float("inf"), float("nan")):
        try:
            convert(str(wav), str(ctx.tmp / "rbw_out"), bad)
        except ValueError as exc:
            assert "RBW" in str(exc), f"{bad!r}: {exc}"
            continue
        raise AssertionError(f"RBW {bad!r} was accepted")
    result = convert(str(wav), str(ctx.tmp / "rbw_out"), 1e12)
    assert Path(result).is_file()
    return "7 invalid values rejected; 1e12 Hz -> 2-point FFT ok"


@case("dB min >= dB max: engine swaps, GUI rejects")
def t_db_range(ctx):
    wav = ctx.tmp / "db.wav"
    write_iq(wav)
    result = convert(str(wav), str(ctx.tmp / "db_out"), 500, db_auto=False, db_vmin=-20, db_vmax=-80)
    assert Path(result).is_file()
    if ctx.gui is None:
        return "engine ok (GUI check skipped)"
    app = ctx.gui
    app.db_auto_var.set(False)
    app.db_vmin_var.set("-20")
    app.db_vmax_var.set("-20")
    ctx.dialogs.clear()
    assert app._parse_display_options() is None
    assert any("dB max must be greater" in d[2] for d in ctx.dialogs), ctx.dialogs
    app.db_vmax_var.set("abc")
    ctx.dialogs.clear()
    assert app._parse_display_options() is None
    app.db_vmin_var.set("")
    app.db_vmax_var.set("")
    app.db_auto_var.set(True)
    return "engine swapped limits; GUI showed 'dB max must be greater than dB min'"


@case("bad colormap name falls back to jet")
def t_bad_cmap(ctx):
    wav = ctx.tmp / "cmap.wav"
    write_iq(wav)
    result = convert(str(wav), str(ctx.tmp / "cmap_out"), 500, cmap="not-a-map")
    assert Path(result).is_file()
    assert iq_data._normalize_cmap("TURBO") == "turbo"
    assert iq_data._normalize_cmap(None) == "jet"
    return "ok"


@case("paths with spaces and Hebrew (input and output)")
def t_unicode_paths(ctx):
    wav = ctx.tmp / HEBREW_FOLDER / "my file \u05d1\u05d3\u05d9\u05e7\u05d4.wav"
    write_iq(wav)
    out = ctx.tmp / f"{HEBREW_FOLDER} out" / "IQ Results"
    result = convert(str(wav), str(out), 500)
    assert Path(result).is_file(), result
    assert Path(result).name == "my file \u05d1\u05d3\u05d9\u05e7\u05d4.png"
    return "ok"


@case("long filenames (> 200 chars) for input and output")
def t_long_names(ctx):
    stem = "L" * 205
    wav = ctx.tmp / f"{stem}.wav"
    out = ctx.tmp / ("O" * 120)
    try:
        write_iq(wav)
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Skip(f"this OS refused to create the long path ({exc.__class__.__name__}); enable Win32 long paths") from exc
    result = convert(str(wav), str(out), 500)
    assert Path(result).is_file()
    assert not leftovers(out)
    return f"input {len(str(wav))} chars, output {len(str(result))} chars"


@case("missing input file gives FileNotFoundError")
def t_missing_input(ctx):
    try:
        convert(str(ctx.tmp / "nope.wav"), str(ctx.tmp / "nope_out"), 500)
    except FileNotFoundError as exc:
        return str(exc).splitlines()[0]
    raise AssertionError("missing file did not raise")


@case("read-only output folder (deny-write ACL) gives a clean PermissionError")
def t_readonly_output(ctx):
    if os.name != "nt":
        raise Skip("Windows ACL test")
    wav = ctx.tmp / "ro.wav"
    write_iq(wav)
    out = ctx.tmp / "ro_out"
    out.mkdir()
    user = os.environ.get("USERNAME", "")
    deny = subprocess.run(["icacls", str(out), "/deny", f"{user}:(OI)(CI)(W,AD,WD)"], capture_output=True, text=True)
    if deny.returncode != 0:
        raise Skip(f"icacls deny failed: {deny.stderr.strip() or deny.stdout.strip()}")
    try:
        try:
            convert(str(wav), str(out), 500)
        except PermissionError as exc:
            assert "read-only" in str(exc) or "Cannot write" in str(exc), str(exc)
            note = str(exc).splitlines()[0]
        else:
            raise AssertionError("write to deny-ACL folder succeeded")
        if ctx.gui is not None:
            assert ctx.gui._folder_writable(out) is False
            note += "; GUI pre-check detects it"
        return note
    finally:
        subprocess.run(["icacls", str(out), "/remove:d", user], capture_output=True)


@case("duplicate stems from two folders get (2) suffix, no silent overwrite")
def t_duplicate_names(ctx):
    from iq_gui import unique_output_names

    a = ctx.tmp / "dupA" / "capture.wav"
    b = ctx.tmp / "dupB" / "capture.wav"
    write_iq(a, tones=((1000.0, 0.5),))
    write_iq(b, tones=((9000.0, 0.5),))
    names = unique_output_names([a, b, ctx.tmp / "dupC" / "Capture.wav"])
    assert names == ["capture.png", "capture (2).png", "Capture (3).png"], names
    out = ctx.tmp / "dup_out"
    for wav, name in zip((a, b), names):
        convert(str(wav), str(out), 500, output_name=name)
    files = sorted(p.name for p in out.glob("*.png"))
    assert files == ["capture (2).png", "capture.png"], files
    assert sha256(out / "capture.png") != sha256(out / "capture (2).png")
    return "two different PNGs kept"


@case("existing PNG is replaced atomically; a failed run keeps the old PNG")
def t_existing_output(ctx):
    wav = ctx.tmp / "exist.wav"
    write_iq(wav)
    out = ctx.tmp / "exist_out"
    out.mkdir()
    png = out / "exist.png"
    png.write_bytes(b"OLD")
    convert(str(wav), str(out), 500)
    assert png.stat().st_size > 5000 and png.read_bytes()[:4] == b"\x89PNG"
    good = sha256(png)
    # a failing conversion for the same stem must leave the good PNG untouched
    bad = ctx.tmp / "exist_bad" / "exist.wav"
    bad.parent.mkdir()
    sf.write(str(bad), np.zeros(4096, dtype=np.float32), 48_000)  # mono -> error
    try:
        convert(str(bad), str(out), 500)
    except ValueError:
        pass
    assert sha256(png) == good
    assert not leftovers(out), leftovers(out)
    return "replaced once; preserved on failure; no temp files"


@case("cancel mid-conversion leaves no PNG and no temp file")
def t_cancel(ctx):
    wav = ctx.tmp / "cancel.wav"
    write_iq(wav, seconds=4.0, fs=96_000, noise=0.01)
    out = ctx.tmp / "cancel_out"
    event = threading.Event()
    seen = []

    def cb(frac, msg):
        seen.append(frac)
        if frac > 0.2:
            event.set()

    try:
        convert(str(wav), str(out), 50, progress_callback=cb, cancel_event=event)
    except iq_data.ConversionCancelled:
        pass
    else:
        raise AssertionError("cancel_event was ignored")
    assert not (out / "cancel.png").exists()
    assert not leftovers(out) if out.exists() else True
    assert max(seen) < 0.9, f"cancelled too late: {max(seen):.2f}"
    return f"stopped at {max(seen):.0%} progress"


@case("signal: complex tone at +f0 appears at +f0 (sign convention) with the right time span")
def t_sign_convention(ctx):
    wav = ctx.tmp / "sign.wav"
    fs, n = write_iq(wav, fs=48_000, seconds=0.5, tones=((6_000.0, 0.5),), noise=0.001)
    freqs, mean_db, peak_db, n_fft, enbw, _ = peak_spectrum(wav, 100)
    f_peak = freqs[int(np.nanargmax(mean_db))]
    assert abs(f_peak - 6_000.0) < fs / n_fft, f"peak at {f_peak:.1f} Hz"
    assert mean_db[int(np.nanargmax(mean_db))] > mean_db[int(np.argmin(np.abs(freqs + 6_000.0)))] + 40
    hop = n_fft // 2
    frames = iq_data._n_stft_frames(n, n_fft, hop)
    covered = (frames - 1) * hop + n_fft
    assert abs(covered - n) < n_fft, f"time coverage {covered}/{n}"
    assert abs(iq_data.wav_info(str(wav))["duration"] - 0.5) < 1e-6
    return f"peak {f_peak:+.1f} Hz, image at -6 kHz is > 40 dB down, {frames} frames cover {covered}/{n} samples"


@case("signal: full-scale tone |a|=1 on a bin centre reads ~0 dBFS/bin")
def t_full_scale(ctx):
    fs = 48_000
    rbw = 100
    n_fft = iq_data._n_fft(fs, rbw)
    f0 = 40 * fs / n_fft  # exactly on a bin centre: no scalloping loss
    wav = ctx.tmp / "fullscale.wav"
    write_iq(wav, fs=fs, seconds=0.5, tones=((f0, 1.0),), subtype="FLOAT")
    freqs, mean_db, peak_db, _, _, _ = peak_spectrum(wav, rbw)
    top = float(np.nanmax(peak_db))
    assert abs(top) < 0.1, f"peak {top:.3f} dBFS/bin"
    return f"peak {top:+.3f} dBFS/bin at {freqs[int(np.nanargmax(peak_db))]:.1f} Hz"


@case("signal: two tones separated by > RBW are resolved")
def t_two_tones(ctx):
    fs, rbw = 48_000, 100
    wav = ctx.tmp / "twotone.wav"
    write_iq(wav, fs=fs, seconds=0.5, tones=((5_000.0, 0.5), (5_500.0, 0.5)), noise=0.001)
    freqs, mean_db, _, n_fft, enbw, _ = peak_spectrum(wav, rbw)
    rbw_true = enbw * fs / n_fft
    assert 5_500.0 - 5_000.0 > 3 * rbw_true
    sel = (freqs > 4_500) & (freqs < 6_000)
    f_sel, m_sel = freqs[sel], mean_db[sel]
    i1 = int(np.argmin(np.abs(f_sel - 5_000)))
    i2 = int(np.argmin(np.abs(f_sel - 5_500)))
    mid = m_sel[(f_sel > 5_150) & (f_sel < 5_350)].max()
    assert m_sel[i1] - mid > 30 and m_sel[i2] - mid > 30, f"dip only {min(m_sel[i1], m_sel[i2]) - mid:.1f} dB"
    return f"both tones present, valley between them {min(m_sel[i1], m_sel[i2]) - mid:.0f} dB down"


@case("signal: reported RBW equals ENBW x bin width (Blackman-Harris 2.0044 bins)")
def t_rbw_true(ctx):
    import io
    from contextlib import redirect_stdout

    fs, rbw = 48_000, 100
    n_fft = iq_data._n_fft(fs, rbw)
    _w, _s, enbw = iq_data._blackmanharris_window(n_fft)
    assert abs(enbw - 2.0044) < 0.002, enbw
    expected = enbw * fs / n_fft
    wav = ctx.tmp / "rbwtrue.wav"
    write_iq(wav, fs=fs)
    buf = io.StringIO()
    with redirect_stdout(buf):
        iq_data.process_file(str(wav), str(ctx.tmp / "rbwtrue_out"), rbw)
    line = next(l for l in buf.getvalue().splitlines() if "RBW (ENBW)" in l)
    reported_khz = float(line.split(":")[1].split("kHz")[0])
    assert abs(reported_khz * 1e3 - expected) < 1.0, f"{line} vs {expected:.1f} Hz"
    return f"ENBW {enbw:.4f} bins, RBW true {expected:.1f} Hz, log says {line.split(':', 1)[1].strip()}"


@case("DSP unchanged versus reference iq_data.py (spectrum + spectrogram arrays exactly equal)")
def t_reference(ctx):
    if not ctx.reference:
        raise Skip("no --reference module given")
    import importlib.util
    import io
    from contextlib import redirect_stdout

    spec = importlib.util.spec_from_file_location("iq_data_reference", ctx.reference)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    wav = ctx.tmp / "ref.wav"
    write_iq(wav, fs=48_000, seconds=0.5, tones=((3_000.0, 0.5), (-7_000.0, 0.2)), noise=0.01)
    for rbw in (100, 500):
        f_new, m_new, p_new, n_new, e_new, _ = peak_spectrum(wav, rbw, iq_data)
        f_old, m_old, p_old, n_old, e_old, _ = peak_spectrum(wav, rbw, ref)
        assert n_new == n_old and e_new == e_old, f"rbw {rbw}: FFT size / ENBW differ"
        assert np.array_equal(f_new, f_old), f"rbw {rbw}: frequency grid differs"
        assert np.array_equal(m_new, m_old, equal_nan=True), f"rbw {rbw}: mean spectrum differs"
        assert np.array_equal(p_new, p_old, equal_nan=True), f"rbw {rbw}: max-hold spectrum differs"
    # A CRFS file with a Recorder manifest: the new engine draws an absolute axis and markers.
    crfs_dir = ctx.tmp / "ref_crfs"
    crfs_dir.mkdir(parents=True, exist_ok=True)
    crfs = crfs_dir / "iq_ref_0001.wav"
    write_iq(crfs, fs=1_000_000, seconds=0.2, tones=((150_000.0, 0.3), (-225_000.0, 0.05)), noise=0.01)
    append_crfs_meta(crfs, 100e6, 0.8e6, 1_000_000)
    write_manifest(crfs_dir, "iq_ref", 99_700_000, 100_100_000)
    keys = ("freqs_hz", "spectrum_mean_db", "spectrum_peak_db", "waterfall_db")
    scalars = ("n_fft", "hop", "n_frames", "frames_done", "rbw_true_hz", "peak_db", "peak_freq_hz", "floor_db", "vmin", "vmax")
    checked = 0
    for src, rbw, cmap in ((wav, 100, "viridis"), (crfs, 2_000, "jet"), (crfs, 500, "gray")):
        new, old = {}, {}
        convert(str(src), str(ctx.tmp / "ref_new"), rbw, cmap=cmap, stats=new, recorder_history=str(ctx.tmp / "none.json"))
        with redirect_stdout(io.StringIO()):
            ref.process_file(str(src), str(ctx.tmp / "ref_old"), rbw, cmap=cmap, stats=old)
        for key in keys:
            assert np.array_equal(new[key], old[key], equal_nan=True), f"{src.name} rbw {rbw}: {key} differs"
        for key in scalars:
            assert new[key] == old[key], f"{src.name} rbw {rbw}: {key} {new[key]!r} != {old[key]!r}"
        assert new["waterfall_image_is_data"], "spectrogram image is not the computed array"
        checked += 1
    kinds = sorted(line["kind"] for line in new["range_lines"])
    assert new["frequency_axis"]["absolute"], new["frequency_axis"]
    assert kinds == ["requested"] * 4 + ["sensor"] * 4, kinds
    return (
        f"{checked} conversions: frequency, mean/max-hold spectrum and spectrogram arrays exactly equal; "
        "PNG differs by design (absolute axis, markers, legend)"
    )


@case("requested-range markers: 1805-1880 MHz at centre 1842.5 MHz, 125 MS/s")
def t_range_markers(ctx):
    folder = ctx.tmp / "range"
    wav = folder / "iq_qa_range_0001.wav"
    write_iq(wav, fs=125_000_000, seconds=0.004, tones=((10e6, 0.5),), noise=0.01)
    append_crfs_meta(wav, 1_842_500_000.0, 75e6, 125_000_000)
    write_manifest(folder, "iq_qa_range", 1_805_000_000, 1_880_000_000)
    stats = {}
    convert(str(wav), str(folder / "out"), 15_000, stats=stats, recorder_history=str(ctx.tmp / "none.json"))
    axis = stats["frequency_axis"]
    assert axis["absolute"] and axis["centre_hz"] == 1_842_500_000.0, axis
    assert (axis["x_min"], axis["x_max"]) == (1780.0, 1905.0), axis
    xs = sorted((line["boundary"], line["axes"], line["xdata"][0]) for line in stats["range_lines"])
    assert xs == [
        ("end", "spectrogram", 1880.0), ("end", "spectrum", 1880.0),
        ("start", "spectrogram", 1805.0), ("start", "spectrum", 1805.0),
    ], xs
    offsets = sorted(b["hz"] - axis["centre_hz"] for b in stats["range_boundaries"])
    assert offsets == [-37.5e6, 37.5e6], offsets
    return "lines at exactly 1805.0 / 1880.0 MHz on both panels; offsets -37.5 / +37.5 MHz from 1842.5 MHz"


@case("thumbnail: spectrogram-only PNG at a fixed size from the same grid; centre in title; off by default")
def t_thumbnail(ctx):
    from PIL import Image

    folder = ctx.tmp / "thumb"
    wav = folder / "iq_qa_thumb_0001.wav"
    fs = 1_000_000
    write_iq(wav, fs=fs, seconds=0.2, tones=((150_000.0, 0.5),), noise=0.001)
    append_crfs_meta(wav, 1_842_500_000.0, 0.8e6, fs)
    plain, thumbed = {}, {}
    convert(str(wav), str(folder / "plain"), 2_000, stats=plain, recorder_history=str(ctx.tmp / "none.json"))
    assert plain["thumbnail_path"] is None and not list((folder / "plain").glob("*.thumb*")), "thumbnail written by default"
    assert "centre" not in plain["title"], plain["title"]
    # Gray with a fixed dB range: brightness is monotonic in power, so the tone column is the brightest.
    convert(str(wav), str(folder / "plain"), 2_000, stats=plain, cmap="gray", db_auto=False, db_vmin=-110, db_vmax=0,
            recorder_history=str(ctx.tmp / "none.json"))
    convert(
        str(wav), str(folder / "thumbed"), 2_000, stats=thumbed, recorder_history=str(ctx.tmp / "none.json"),
        cmap="gray", db_auto=False, db_vmin=-110, db_vmax=0,
        thumbnail_name="iq_qa_thumb_0001.thumb.png", thumbnail_size=(160, 80), title_centre=True,
    )
    for key in ("freqs_hz", "spectrum_mean_db", "spectrum_peak_db", "waterfall_db"):
        assert np.array_equal(plain[key], thumbed[key], equal_nan=True), f"{key} changed with a thumbnail"
    assert "centre 1842.5 MHz" in thumbed["title"], thumbed["title"]
    thumb = Path(thumbed["thumbnail_path"])
    assert thumb.is_file() and Path(thumbed["out_path"]).is_file()
    assert not list(thumb.parent.glob("*.part.png")), "temp file left behind"
    with Image.open(thumb) as im:
        assert im.size == (160, 80), im.size
        assert im.mode == "P", im.mode
        lum = np.asarray(im.convert("L"), dtype=np.float64)
    expected_x = (150_000.0 + fs / 2) / fs * 160
    brightest = float(np.argmax(lum.mean(axis=0)))
    assert abs(brightest - expected_x) <= 2.0, (brightest, expected_x)
    bare = folder / "iq_qa_bare_0001.wav"
    write_iq(bare, fs=fs, seconds=0.1)
    unknown = {}
    convert(str(bare), str(folder / "bare"), 2_000, stats=unknown, title_centre=True, thumbnail_name="b.thumb",
            recorder_history=str(ctx.tmp / "none.json"))
    assert "centre unknown" in unknown["title"] and unknown["thumbnail_path"].endswith("b.thumb.png"), unknown["title"]
    try:
        convert(str(bare), str(folder / "bare"), 2_000, thumbnail_name="iq_qa_bare_0001.png")
    except ValueError:
        pass
    else:
        raise AssertionError("a thumbnail named like the PNG was accepted")
    return f"160x80 palette PNG, {thumb.stat().st_size} bytes, tone column {brightest:.0f} (expected {expected_x:.0f}); arrays unchanged"


@case("without scipy: numpy FFT and window give the same spectrum and spectrogram (bundled engine)")
def t_numpy_fallback(ctx):
    for n in (8, 1024, 65536):
        scipy_w = np.asarray(iq_data._get_window()("blackmanharris", n, fftbins=True), dtype=np.float64)
        assert np.array_equal(scipy_w, iq_data._numpy_get_window("blackmanharris", n)), f"window differs at {n}"
    wav = ctx.tmp / "fallback" / "iq_qa_fallback_0001.wav"
    write_iq(wav, fs=1_000_000, seconds=0.2, tones=((150_000.0, 0.5), (-225_000.0, 0.01)), noise=0.01)
    append_crfs_meta(wav, 100e6, 0.8e6, 1_000_000)
    ref, alt = {}, {}
    convert(str(wav), str(ctx.tmp / "fallback" / "scipy"), 2_000, stats=ref, recorder_history=str(ctx.tmp / "none.json"))
    saved = (iq_data._fft_fn, iq_data._get_window_fn)
    iq_data._fft_fn, iq_data._get_window_fn = iq_data._numpy_fft, iq_data._numpy_get_window
    try:
        assert iq_data.fft_backend() == "numpy"
        convert(str(wav), str(ctx.tmp / "fallback" / "numpy"), 2_000, stats=alt, recorder_history=str(ctx.tmp / "none.json"))
    finally:
        iq_data._fft_fn, iq_data._get_window_fn = saved
    assert np.array_equal(ref["freqs_hz"], alt["freqs_hz"])
    worst = 0.0
    for key in ("spectrum_mean_db", "spectrum_peak_db", "waterfall_db"):
        a, b = np.asarray(ref[key], dtype=np.float64), np.asarray(alt[key], dtype=np.float64)
        assert np.array_equal(np.isfinite(a), np.isfinite(b)), f"{key}: finite cells differ"
        diff = float(np.nanmax(np.abs(a - b))) if a.size else 0.0
        assert diff < 0.05, f"{key} differs by {diff:.4f} dB"
        worst = max(worst, diff)
    for key in ("n_fft", "hop", "n_frames", "rbw_true_hz"):
        assert ref[key] == alt[key], f"{key} {ref[key]!r} != {alt[key]!r}"
    return f"window bit-identical; spectra and spectrogram within {worst:.4f} dB of scipy"


@case("original IQ file is unchanged after conversion (sha256)")
def t_preserve(ctx):
    wav = ctx.tmp / "preserve.wav"
    write_iq(wav)
    before = sha256(wav)
    mtime = wav.stat().st_mtime_ns
    convert(str(wav), str(ctx.tmp / "preserve_out"), 500)
    assert sha256(wav) == before and wav.stat().st_mtime_ns == mtime
    return before[:16]


@case("large real recording converts in chunks (needs --real-wav)")
def t_real_file(ctx):
    if not ctx.real_wav:
        raise Skip("no --real-wav given")
    wav = Path(ctx.real_wav)
    assert wav.is_file(), wav
    info = iq_data.wav_info(str(wav))
    before = sha256(wav)
    read_size = iq_data._block_sample_count(iq_data._n_fft(info["samplerate"], 50_000), iq_data._n_fft(info["samplerate"], 50_000) // 2)
    assert read_size < info["frames"], "file is not large enough to exercise chunking"
    t0 = time.perf_counter()
    result = convert(str(wav), str(ctx.tmp / "real_out"), 50_000)
    dt = time.perf_counter() - t0
    assert Path(result).is_file()
    assert sha256(wav) == before, "input file changed!"
    return f"{info['frames']:,} frames ({info['size'] / 1e6:.0f} MB) in {dt:.1f} s, chunk {read_size:,} samples; input hash unchanged"


# ---------------------------------------------------------------------------
# GUI cases (Tk, no mainloop: the test pumps update())
# ---------------------------------------------------------------------------
def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.01)


def wait_until(app, pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.update()
        if pred():
            return True
        time.sleep(0.01)
    return False


def make_gui(ctx, settings_name):
    import iq_gui

    os.environ["IQ_GUI_SETTINGS"] = str(ctx.tmp / settings_name)
    app = iq_gui.IQConverterGUI()
    app.update()
    # route themed dialogs to a list so nothing blocks
    app._info = lambda t, m: ctx.dialogs.append(("info", t, m))
    app._warn = lambda t, m: ctx.dialogs.append(("warn", t, m))
    app._error = lambda t, m: ctx.dialogs.append(("error", t, m))
    app._ask = lambda t, m: (ctx.dialogs.append(("ask", t, m)) or True)
    app.open_when_done_var.set(False)
    assert wait_until(app, lambda: app._engine_ready, 30), "engine did not load"
    return app


@case("GUI: window opens, engine loads in background, status becomes Ready")
def t_gui_open(ctx):
    app = ctx.gui
    import iq_gui

    assert app.status_var.get() == iq_gui.STATUS_READY
    assert app.title() == iq_gui.APP_NAME
    w, h = app.winfo_width(), app.winfo_height()
    assert w >= 600 and h >= 400, (w, h)
    return f"{w}x{h}, status '{app.status_var.get()}'"


@case("GUI: single conversion via Convert, repeated clicks ignored")
def t_gui_convert(ctx):
    app = ctx.gui
    wav = ctx.tmp / "gui_single.wav"
    write_iq(wav, seconds=1.0, fs=96_000, noise=0.01)
    out = ctx.tmp / "gui_single_out"
    app.input_var.set(str(wav))
    app.output_var.set(str(out))
    app.rbw_var.set("200")
    app.update()
    ctx.dialogs.clear()
    app.start_conversion()
    job = app._job_id
    app.start_conversion()
    app.start_conversion()
    app._on_return_key(type("E", (), {"widget": app.input_entry})())
    assert app._job_id == job, "a second job was started"
    assert str(app.convert_button.cget("state")) == "disabled"
    assert str(app.cancel_button.cget("state")) == "normal"
    assert wait_until(app, lambda: not app._converting, 90), "conversion did not finish"
    assert (out / "gui_single.png").is_file()
    assert "Image saved" in app.status_var.get(), app.status_var.get()
    assert str(app.convert_button.cget("state")) == "normal"
    assert app._last_png_path == out / "gui_single.png"
    return app.status_var.get()


@case("GUI: Cancel stops the job, no PNG, controls recover")
def t_gui_cancel(ctx):
    app = ctx.gui
    wav = ctx.tmp / "gui_cancel.wav"
    write_iq(wav, seconds=6.0, fs=192_000, noise=0.01)
    out = ctx.tmp / "gui_cancel_out"
    app.input_var.set(str(wav))
    app.output_var.set(str(out))
    app.rbw_var.set("20")
    app.update()
    app.start_conversion()
    assert wait_until(app, lambda: app._progress_fraction > 0.05, 60), "no progress"
    app.cancel_conversion()
    assert wait_until(app, lambda: not app._converting, 60), "did not stop"
    assert not (out / "gui_cancel.png").exists()
    assert not leftovers(out) if out.exists() else True
    assert app.status_var.get().startswith("Cancelled"), app.status_var.get()
    assert str(app.convert_button.cget("state")) == "normal"
    return app.status_var.get()


@case("GUI: failed job (mono) then a good job works")
def t_gui_recovery(ctx):
    app = ctx.gui
    mono = ctx.tmp / "gui_mono.wav"
    sf.write(str(mono), np.zeros(8192, dtype=np.float32), 48_000)
    good = ctx.tmp / "gui_good.wav"
    write_iq(good)
    out = ctx.tmp / "gui_recover_out"
    app.output_var.set(str(out))
    app.rbw_var.set("500")
    app.input_var.set(str(mono))
    ctx.dialogs.clear()
    app.start_conversion()
    pump(app, 0.3)
    assert any("Need stereo" in d[2] for d in ctx.dialogs), ctx.dialogs
    assert not app._converting
    # force the engine-level failure path too (file becomes mono after the pre-check)
    app._start_jobs([mono])
    assert wait_until(app, lambda: not app._converting, 30)
    assert app.status_var.get() == "Conversion failed", app.status_var.get()
    app.input_var.set(str(good))
    app.start_conversion()
    assert wait_until(app, lambda: not app._converting, 60)
    assert (out / "gui_good.png").is_file()
    return "error dialog shown, then next job saved a PNG"


@case("GUI: batch of files with duplicate names via _start_jobs")
def t_gui_batch(ctx):
    app = ctx.gui
    a = ctx.tmp / "gb1" / "same.wav"
    b = ctx.tmp / "gb2" / "same.wav"
    c = ctx.tmp / "gb2" / "other.wav"
    for p in (a, b, c):
        write_iq(p)
    out = ctx.tmp / "gui_batch_out"
    app.output_var.set(str(out))
    app.rbw_var.set("500")
    ctx.dialogs.clear()
    app._start_jobs([a, b, c])
    assert wait_until(app, lambda: not app._converting, 90)
    files = sorted(p.name for p in out.glob("*.png"))
    assert files == ["other.png", "same (2).png", "same.png"], files
    assert any(d[1] == "Batch complete" for d in ctx.dialogs), ctx.dialogs
    return ", ".join(files)


@case("GUI: drop event with braces and spaces selects the file")
def t_gui_drop(ctx):
    app = ctx.gui
    wav = ctx.tmp / "drop folder" / "dropped file.wav"
    write_iq(wav)
    event = type("E", (), {"data": "{" + str(wav).replace("\\", "/") + "}"})()
    app._on_drop(event)
    assert Path(app.input_var.get()) == Path(str(wav).replace("\\", "/")), app.input_var.get()
    return app.input_var.get()


@case("GUI: theme toggle persists in settings JSON and reloads")
def t_gui_theme(ctx):
    import iq_gui
    from iq_theme import DARK_MODE, current_mode

    app = ctx.gui
    start = current_mode()
    app._toggle_theme()
    app.update()
    assert current_mode() != start
    data = json.loads(Path(os.environ["IQ_GUI_SETTINGS"]).read_text(encoding="utf-8"))
    assert data["ui_theme"] == current_mode()
    saved = current_mode()
    second = iq_gui.IQConverterGUI()
    second.update()
    try:
        assert current_mode() == saved
    finally:
        second._on_close()
    if current_mode() != DARK_MODE:
        app._toggle_theme()
    app.apply_theme(start)
    return f"{start} -> {saved}, reloaded as {saved}"


@case("GUI: settings round-trip (output, RBW, cmap, dB range)")
def t_gui_settings(ctx):
    import iq_gui

    app = ctx.gui
    out = ctx.tmp / "settings out \u05d1"
    app.output_var.set(str(out))
    app.rbw_var.set("12345")
    app.cmap_var.set("gray")
    app.db_auto_var.set(False)
    app.db_vmin_var.set("-90")
    app.db_vmax_var.set("-10")
    app.freq_smooth_var.set(True)
    history = ctx.tmp / "history \u05d1.json"
    assert app.range_markers_var.get() is True, "range markers must be on by default"
    app.range_markers_var.set(False)
    app.recorder_history_var.set(str(history))
    app._save_settings()
    second = iq_gui.IQConverterGUI()
    try:
        assert second.output_var.get() == str(out)
        assert second.rbw_var.get() == "12345"
        assert second.cmap_var.get() == "gray"
        assert second.db_auto_var.get() is False
        assert second.db_vmin_var.get() == "-90" and second.db_vmax_var.get() == "-10"
        assert second.freq_smooth_var.get() is True
        assert second.range_markers_var.get() is False
        assert second.recorder_history_var.get() == str(history)
    finally:
        second._on_close()
    app.db_auto_var.set(True)
    app.freq_smooth_var.set(False)
    app.cmap_var.set("jet")
    app.range_markers_var.set(True)
    app.recorder_history_var.set("")
    app._save_settings()
    return "ok (incl. range markers + Recorder history)"


@case("GUI: file info shows centre + requested range; conversion marks it (markers on by default)")
def t_gui_range(ctx):
    app = ctx.gui
    folder = ctx.tmp / "gui_range"
    wav = folder / "iq_qa_gui_0001.wav"
    write_iq(wav, fs=10_000_000, seconds=0.05, tones=((1e6, 0.4),), noise=0.01)
    append_crfs_meta(wav, 100e6, 8e6, 10_000_000)
    write_manifest(folder, "iq_qa_gui", 97_000_000, 101_000_000)
    out = ctx.tmp / "gui_range_out"
    app.recorder_history_var.set(str(ctx.tmp / "none.json"))
    app.output_var.set(str(out))
    app.rbw_var.set("2000")
    assert app.range_markers_var.get() is True
    app._set_input_file(wav)
    assert wait_until(app, lambda: "Requested 97–101 MHz" in app.file_info_var.get(), 30), app.file_info_var.get()
    info = app.file_info_var.get()
    assert "10 MS/s" in info and "Centre 100 MHz" in info and "Sensor-reported 96–104 MHz" in info, info
    ctx.dialogs.clear()
    app.start_conversion()
    assert wait_until(app, lambda: not app._converting, 90), "conversion did not finish"
    assert (out / "iq_qa_gui_0001.png").is_file()
    log = app.log.get("1.0", "end")
    assert "range markers on" in log and "Requested range: 97 - 101 MHz" in log, log[-1500:]
    assert "Range markers  : requested start 97 MHz; requested end 101 MHz; sensor start 96 MHz" in log, log[-1500:]
    app.recorder_history_var.set("")
    return info.replace("\n", " | ")


@case("GUI: resizing respects the minimum size; controls stay visible")
def t_gui_resize(ctx):
    app = ctx.gui
    min_w, min_h = app.minsize()
    app.geometry("200x150")
    pump(app, 0.3)
    w, h = app.winfo_width(), app.winfo_height()
    assert w >= min_w - 2 and h >= min_h - 2, (w, h, min_w, min_h)
    bottom = app.winfo_rooty() + h
    for widget in (app.convert_button, app.progress, app.status_label):
        assert widget.winfo_ismapped()
        assert widget.winfo_rooty() + widget.winfo_height() <= bottom + 1, widget
    app.geometry(f"{min_w + 300}x{min_h + 200}")
    pump(app, 0.2)
    return f"min {min_w}x{min_h}"


@case("GUI: close during conversion does not crash and stops the worker")
def t_gui_close_during(ctx):
    import iq_gui

    os.environ["IQ_GUI_SETTINGS"] = str(ctx.tmp / "close.json")
    app = iq_gui.IQConverterGUI()
    app._info = app._warn = app._error = lambda *a: None
    app.open_when_done_var.set(False)
    assert wait_until(app, lambda: app._engine_ready, 30)
    wav = ctx.tmp / "gui_close.wav"
    write_iq(wav, seconds=12.0, fs=192_000, noise=0.01)
    out = ctx.tmp / "gui_close_out"
    app.input_var.set(str(wav))
    app.output_var.set(str(out))
    app.rbw_var.set("20")
    app.update()
    app.start_conversion()
    assert wait_until(app, lambda: app._progress_fraction > 0.03, 60), "no progress reported"
    event = app._cancel_event
    app.after(50, app._on_close)
    try:
        app.mainloop()  # returns when the window is destroyed
    except Exception as exc:
        raise AssertionError(f"mainloop raised {exc!r}")
    assert event.is_set(), "closing did not set the cancel event"
    workers = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
    for t in workers:
        t.join(15)
    still = [t.name for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
    assert not still, f"worker threads still alive: {still}"
    assert not (out / "gui_close.png").exists(), "PNG was written although the window closed mid-job"
    assert not (leftovers(out) if out.exists() else []), "temp files left behind"
    return "window closed mid-job, worker cancelled, no PNG"


# ---------------------------------------------------------------------------
# Packaged EXE (clean-environment substitute)
# ---------------------------------------------------------------------------
@case("EXE: headless conversion from a temp folder with Python removed from the environment")
def t_exe(ctx):
    if not ctx.exe:
        raise Skip("no --exe given")
    exe = Path(ctx.exe)
    assert exe.is_file(), exe
    work = Path(tempfile.mkdtemp(prefix="iq_exe_clean_"))
    try:
        target = work / exe.name
        shutil.copy2(exe, target)
        wav = work / "clean test.wav"
        write_iq(wav, seconds=0.5)
        out = work / "out"
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSTARTUP")}
        env["PATH"] = ";".join(
            p for p in env.get("PATH", "").split(";") if "python" not in p.lower() and "venv" not in p.lower()
        ) or r"C:\Windows\System32;C:\Windows"
        env["TEMP"] = env["TMP"] = str(work)
        log = work / "run.log"
        t0 = time.perf_counter()
        proc = subprocess.run(
            [str(target), "--input", str(wav), "--output", str(out), "--rbw", "500", "--log", str(log)],
            cwd=str(work),
            env=env,
            capture_output=True,
            timeout=300,
        )
        dt = time.perf_counter() - t0
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "(no log)"
        assert proc.returncode == 0, f"exit {proc.returncode}: {text[-800:]}"
        assert (out / "clean test.png").is_file(), text[-800:]
        crfs = work / "iq_exe_range_0001.wav"
        write_iq(crfs, fs=10_000_000, seconds=0.05, tones=((1e6, 0.4),), noise=0.01)
        append_crfs_meta(crfs, 100e6, 8e6, 10_000_000)
        write_manifest(work, "iq_exe_range", 97_000_000, 101_000_000)
        log2 = work / "range.log"
        proc = subprocess.run(
            [str(target), "--input", str(crfs), "--output", str(out), "--rbw", "2000", "--log", str(log2),
             "--recorder-history", str(work / "none.json")],
            cwd=str(work), env=env, capture_output=True, timeout=300,
        )
        text = log2.read_text(encoding="utf-8", errors="replace") if log2.is_file() else "(no log)"
        assert proc.returncode == 0, f"exit {proc.returncode}: {text[-800:]}"
        assert (out / "iq_exe_range_0001.png").is_file(), text[-800:]
        assert "Centre         : 100 MHz (file metadata)" in text, text[-1200:]
        assert "Range markers  : requested start 97 MHz; requested end 101 MHz" in text, text[-1200:]
        return (
            f"PNGs created by the EXE ({dt:.1f} s for the first) with PATH stripped of Python; CRFS file got the "
            "absolute axis and requested-range markers; this is not a separate clean VM"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, args):
        self.tmp = Path(tempfile.mkdtemp(prefix="iq_qa_"))
        self.real_wav = args.real_wav
        self.reference = args.reference
        self.exe = args.exe
        self.gui = None
        self.dialogs: list[tuple] = []


ENGINE_CASES = [
    t_single, t_batch, t_drop_parser, t_mono, t_zero_byte, t_truncated_data, t_truncated_header,
    t_non_wav, t_unsupported_encoding, t_no_metadata, t_too_short, t_silence, t_clipped, t_bad_rbw,
    t_db_range, t_bad_cmap, t_unicode_paths, t_long_names, t_missing_input, t_readonly_output,
    t_duplicate_names, t_existing_output, t_cancel, t_sign_convention, t_full_scale, t_two_tones,
    t_rbw_true, t_reference, t_range_markers, t_thumbnail, t_numpy_fallback, t_preserve, t_real_file,
]
GUI_CASES = [
    t_gui_open, t_gui_convert, t_gui_cancel, t_gui_recovery, t_gui_batch, t_gui_drop, t_gui_theme,
    t_gui_settings, t_gui_range, t_gui_resize,
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real-wav", default=None, help="Large real IQ WAV (read only, never copied)")
    parser.add_argument("--reference", default=None, help="Old iq_data.py to compare DSP output against")
    parser.add_argument("--exe", default=None, help="Packaged EXE to run headless from a temp folder")
    parser.add_argument("--no-gui", action="store_true", help="Skip the Tk GUI cases")
    parser.add_argument("--report", default=None, help="Write JSON results here")
    parser.add_argument("--keep", action="store_true", help="Keep the temp folder")
    args = parser.parse_args()

    ctx = Ctx(args)
    print(f"temp folder: {ctx.tmp}")
    t_start = time.perf_counter()
    try:
        if not args.no_gui:
            try:
                ctx.gui = make_gui(ctx, "qa.json")
            except Exception as exc:
                print(f"GUI could not be created: {exc!r}")
                ctx.gui = None
        for fn in ENGINE_CASES:
            run_case(fn, ctx)
        if ctx.gui is not None:
            for fn in GUI_CASES:
                run_case(fn, ctx)
            try:
                ctx.gui._on_close()
            except Exception:
                pass
            ctx.gui = None
            run_case(t_gui_close_during, ctx)
        run_case(t_exe, ctx)
    finally:
        if not args.keep:
            shutil.rmtree(ctx.tmp, ignore_errors=True)

    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "SKIP")}
    total_s = time.perf_counter() - t_start
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped in {total_s:.0f} s")
    for r in RESULTS:
        if r["status"] == "FAIL":
            print(f"  FAIL {r['name']}: {r['note']}")
    if args.report:
        Path(args.report).write_text(
            json.dumps({"counts": counts, "seconds": round(total_s), "results": RESULTS}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    return counts["FAIL"]


if __name__ == "__main__":
    raise SystemExit(main())
