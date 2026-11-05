"""Requested-range markers and the absolute frequency axis. No GUI.

Synthetic CRFS-style WAVs (stereo PCM16 + trailing ``meta`` chunk), Recorder
manifests and history files are written to tmp_path, and every lookup is
pointed at them, so nothing on this PC is read.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import iq_data
import iq_metadata

HERE = Path(__file__).resolve().parent
NUMERIC_KEYS = ("freqs_hz", "spectrum_mean_db", "spectrum_peak_db", "waterfall_db")
SCALAR_KEYS = ("n_fft", "hop", "n_frames", "frames_done", "rbw_true_hz", "peak_db", "peak_freq_hz", "floor_db", "vmin", "vmax")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def _write_wav(path: Path, fs: int, seconds: float, tones=((0.0, 0.0),), seed=3):
    n = int(round(fs * seconds))
    t = np.arange(n, dtype=np.float64) / fs
    rng = np.random.default_rng(seed)
    iq = 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    for f0, amp in tones:
        iq += amp * np.exp(2j * np.pi * f0 * t)
    data = np.clip(np.column_stack((iq.real, iq.imag)), -1.0, 1.0).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, fs, subtype="PCM_16")
    return path


def _append_meta(path: Path, centre_hz=None, bandwidth_hz=None, sample_rate=None, data_type="complex", receiver="rfeye100400"):
    """CRFS trailer: <signalinfo><base> values written like the sensor does ("1.8425e+09")."""
    base = ""
    if centre_hz is not None:
        base += f"<rf_frequency>{centre_hz:g}</rf_frequency>"
    if bandwidth_hz is not None:
        base += f"<bandwidth>{bandwidth_hz:g}</bandwidth>"
    if data_type:
        base += f"<data_type>{data_type}</data_type>"
    if sample_rate is not None:
        base += f"<sample_rate>{sample_rate:g}</sample_rate>"
    xml = (
        f'<?xml version="1.0"?><signalinfo><base>{base}</base>'
        f"<extended><receiver>{receiver}</receiver></extended></signalinfo>"
    ).encode("utf-8")
    pad = b"\x00" if len(xml) % 2 else b""
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        fh.write(b"meta" + struct.pack("<I", len(xml)) + xml + pad)
        size = fh.tell()
        fh.seek(4)
        fh.write(struct.pack("<I", size - 8))
    return path


def _crfs_wav(path: Path, fs: int, centre_hz, bandwidth_hz, seconds=0.004, tones=((0.0, 0.0),), **meta):
    _write_wav(path, fs, seconds, tones)
    return _append_meta(path, centre_hz, bandwidth_hz, meta.pop("sample_rate", fs), **meta)


def _manifest(folder: Path, stem: str, start_hz, stop_hz, serial="rfeye100400", centre_hz=None, **alloc):
    data = {
        "schema": 1,
        "kind": "cellular_capture",
        "identifiers": {"recording_stem": stem},
        "sensor": {"serial": serial},
        "recording": {"recorded_center_hz": centre_hz},
        "allocation": {"requested_start_hz": start_hz, "requested_stop_hz": stop_hz, **alloc},
    }
    path = folder / f"{stem}.cellular.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _history(path: Path, *entries):
    records = []
    for e in entries:
        rec = {"demo": False, "sensor_id": "rfeye100400", "started_at": "2026-11-05T10:00:00"}
        rec.update(e)
        if "center_hz" not in rec:
            rec["center_hz"] = (rec["start_hz"] + rec["end_hz"]) / 2
        if "bandwidth_hz" not in rec:
            rec["bandwidth_hz"] = rec["end_hz"] - rec["start_hz"]
        records.append(rec)
    path.write_text(json.dumps({"recordings": records}), encoding="utf-8")
    return path


def _convert(wav: Path, history: Path, rbw=15_000.0, **kwargs):
    stats: dict = {}
    out = wav.parent / "out"
    iq_data.process_file(str(wav), str(out), rbw, stats=stats, recorder_history=str(history), **kwargs)
    assert Path(stats["out_path"]).is_file()
    return stats


def _lines(stats, kind=None):
    return [line for line in stats["range_lines"] if kind is None or line["kind"] == kind]


def _tag_texts(stats):
    return [text for row in stats["range_tag_rows"] for text in row["texts"]]


# ---------------------------------------------------------------------------
# Layout checks (display pixels of the saved figure)
# ---------------------------------------------------------------------------
def _overlap(a, b, tol=0.5):
    return a[0] < b[2] - tol and b[0] < a[2] - tol and a[1] < b[3] - tol and b[1] < a[3] - tol


def _inside(inner, outer, tol=0.5):
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol
        and inner[3] <= outer[3] + tol
    )


def _assert_clean_layout(stats):
    lay = stats["layout"]
    saved = lay["saved"]
    for key in ("title", "xlabel", "colorbar", "spectrum_axes", "spectrogram_axes"):
        assert _inside(lay[key], saved), key
    tags = [t["box"] for t in lay["tags"]]
    for box in tags:
        assert _inside(box, saved), "tag clipped"
        assert box[1] >= lay["spectrum_axes"][3] - 0.5, "tag covers the spectrum"
        assert box[0] >= lay["spectrum_axes"][0] - 0.5 and box[2] <= lay["spectrum_axes"][2] + 0.5, "tag beyond the axes"
        assert not _overlap(box, lay["title"]), "tag covers the title"
        assert not _overlap(box, lay["colorbar"]), "tag covers the colour bar"
    for i, a in enumerate(tags):
        for b in tags[i + 1 :]:
            assert not _overlap(a, b), "tags overlap"
    if lay["legend"] is not None:
        legend = lay["legend"]
        assert _inside(legend, saved), "legend clipped"
        assert legend[3] <= lay["xlabel"][1] + 0.5, "legend covers the x label"
        for tick in lay["xticklabels"]:
            assert not _overlap(legend, tick), "legend covers tick labels"
        assert not _overlap(legend, lay["spectrogram_axes"]), "legend covers the spectrogram"
        assert not _overlap(legend, lay["colorbar"]), "legend covers the colour bar"
        for item in lay["legend_texts"]:
            assert _inside(item["box"], saved), f"legend text clipped: {item['text'][:40]}"


# ---------------------------------------------------------------------------
# Metadata and request lookup
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, stem",
    [
        ("iq_20261105_164741_0001.wav", "iq_20261105_164741"),
        ("iq_20261105_164741.wav", "iq_20261105_164741"),
        ("iq_20261105_164741_merged.wav", "iq_20261105_164741"),
        ("iq_20261103_143313_0001 (2).wav", "iq_20261103_143313"),
        ("iq_20261103_143313_0002 - Copy.wav", "iq_20261103_143313"),
        ("file_200740_0001.wav", "file_200740"),
        ("my capture.wav", "my capture"),
    ],
)
def test_recording_stem_strips_parts_and_copies(name, stem):
    assert iq_metadata.recording_stem(name) == stem


def test_exact_frequency_text_is_not_rounded():
    assert iq_data._fmt_freq(1_842_500_000.0, 1e6) == "1842.5"
    assert iq_data._fmt_freq(1_805_000_000.0, 1e6) == "1805"
    assert iq_data._fmt_freq(3_906_250.0, 1e6) == "3.90625"
    assert iq_data._fmt_freq(-37_500_000.0, 1e6) == "-37.5"
    assert iq_data._fmt_rate(125e6) == "125 MS/s"
    assert iq_data._fmt_rate(3_906_250.0) == "3.90625 MS/s"


def test_file_meta_is_read_from_the_trailer(tmp_path):
    wav = _crfs_wav(tmp_path / "a.wav", 1_000_000, 1_842_500_000.0, 75e6)
    meta = iq_metadata.read_file_meta(wav, 1_000_000)
    assert meta.present and meta.centre_verified
    assert meta.centre_hz == 1_842_500_000.0
    assert meta.bandwidth_hz == 75e6
    assert meta.receiver == "rfeye100400"
    assert sf.info(str(wav)).frames == 4000  # the trailer does not disturb the samples


def test_file_without_meta_has_no_centre(tmp_path):
    wav = _write_wav(tmp_path / "plain.wav", 48_000, 0.1)
    meta = iq_metadata.read_file_meta(wav, 48_000)
    assert not meta.present and meta.centre_hz is None and not meta.centre_verified


def test_inconsistent_meta_is_not_trusted(tmp_path):
    wav = _crfs_wav(tmp_path / "a.wav", 1_000_000, 100e6, 0.8e6, sample_rate=2_000_000)
    meta = iq_metadata.read_file_meta(wav, 1_000_000)
    assert meta.problem and not meta.centre_verified and meta.sensor_bandwidth_hz is None
    wav2 = _crfs_wav(tmp_path / "b.wav", 1_000_000, 100e6, 0.8e6, data_type="real")
    assert not iq_metadata.read_file_meta(wav2, 1_000_000).centre_verified


def test_history_path_setting_then_env_then_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(iq_metadata.HISTORY_ENV, raising=False)
    assert iq_metadata.resolve_history_path() == tmp_path / "local" / "CRFS IQ Recorder" / ".crfs_iq_recorder_recordings.json"
    monkeypatch.setenv(iq_metadata.HISTORY_ENV, str(tmp_path / "env.json"))
    assert iq_metadata.resolve_history_path() == tmp_path / "env.json"
    assert iq_metadata.resolve_history_path(str(tmp_path / "set.json")) == tmp_path / "set.json"


def _meta(centre=1_842_500_000.0, bw=75e6, receiver="rfeye100400"):
    return iq_metadata.FileMeta(present=True, centre_hz=centre, bandwidth_hz=bw, sample_rate_hz=125e6, data_type="complex", receiver=receiver)


def test_manifest_beside_the_wav_wins(tmp_path):
    wav = tmp_path / "iq_20261105_164741_0001.wav"
    wav.write_bytes(b"")
    _manifest(tmp_path, "iq_20261105_164741", 1_805_000_000, 1_880_000_000, centre_hz=1_842_500_000)
    hist = _history(tmp_path / "h.json", {"iq_stem": "iq_20261105_164741", "start_hz": 1_805_000_000, "end_hz": 1_880_000_000})
    found = iq_metadata.find_request(wav, _meta(), hist)
    assert found.status == "matched" and found.request.source == "Recorder manifest"
    assert (found.request.start_hz, found.request.stop_hz) == (1_805_000_000.0, 1_880_000_000.0)


def test_manifest_and_history_that_disagree_give_no_request(tmp_path):
    wav = tmp_path / "iq_x_0001.wav"
    wav.write_bytes(b"")
    _manifest(tmp_path, "iq_x", 1_805_000_000, 1_880_000_000)
    hist = _history(tmp_path / "h.json", {"iq_stem": "iq_x", "start_hz": 1_800_000_000, "end_hz": 1_885_000_000, "center_hz": 1_842_500_000})
    found = iq_metadata.find_request(wav, _meta(), hist)
    assert found.request is None and found.status == "conflict"


def test_manifest_accepted_when_one_of_duplicate_history_entries_agrees(tmp_path):
    wav = tmp_path / "iq_dup_0001.wav"
    wav.write_bytes(b"")
    _manifest(tmp_path, "iq_dup", 753_000_000, 803_000_000)
    hist = _history(
        tmp_path / "h.json",
        {"iq_stem": "iq_dup", "start_hz": 3_300_000_000, "end_hz": 3_800_000_000},
        {"iq_stem": "iq_dup", "start_hz": 753_000_000, "end_hz": 803_000_000},
    )
    found = iq_metadata.find_request(wav, _meta(centre=778e6, bw=50e6), hist)
    assert found.status == "matched" and found.request.start_hz == 753e6
    (tmp_path / "iq_dup.cellular.json").unlink()
    alone = iq_metadata.find_request(wav, _meta(centre=778e6, bw=50e6), hist)
    assert alone.request is None and alone.status == "ambiguous"


def test_history_match_requires_same_centre_and_receiver(tmp_path):
    wav = tmp_path / "iq_h_0002.wav"
    wav.write_bytes(b"")
    good = {"iq_stem": "iq_h", "start_hz": 3_000_000_000, "end_hz": 3_200_000_000, "center_hz": 3_100_000_000}
    hist = _history(tmp_path / "h1.json", good)
    found = iq_metadata.find_request(wav, _meta(centre=3.1e9, bw=100e6), hist)
    assert found.status == "matched" and found.request.source == "Recorder history"
    other_centre = iq_metadata.find_request(wav, _meta(centre=3.15e9, bw=100e6), hist)
    assert other_centre.request is None and other_centre.status == "conflict"
    other_rx = iq_metadata.find_request(wav, _meta(centre=3.1e9, bw=100e6, receiver="rfeye999"), hist)
    assert other_rx.request is None and other_rx.status == "not_found"
    demo = _history(tmp_path / "h2.json", dict(good, demo=True))
    assert iq_metadata.find_request(wav, _meta(centre=3.1e9, bw=100e6), demo).request is None


def test_history_cannot_be_confirmed_without_a_centre(tmp_path):
    wav = tmp_path / "iq_n_0002.wav"
    wav.write_bytes(b"")
    hist = _history(tmp_path / "h.json", {"iq_stem": "iq_n", "start_hz": 5_310_000_000, "end_hz": 5_330_000_000})
    found = iq_metadata.find_request(wav, iq_metadata.FileMeta(), hist)
    assert found.request is None and found.status == "unverified"


def test_missing_history_file_is_reported(tmp_path):
    wav = tmp_path / "iq_m.wav"
    wav.write_bytes(b"")
    found = iq_metadata.find_request(wav, _meta(), tmp_path / "missing.json")
    assert found.request is None and found.status == "no_records"


def test_manifest_for_another_receiver_or_centre_is_refused(tmp_path):
    wav = tmp_path / "iq_r.wav"
    wav.write_bytes(b"")
    _manifest(tmp_path, "iq_r", 1_805_000_000, 1_880_000_000, serial="rfeye111111")
    assert iq_metadata.find_request(wav, _meta(), tmp_path / "none.json").status == "conflict"
    _manifest(tmp_path, "iq_r", 1_805_000_000, 1_880_000_000, centre_hz=1_900_000_000)
    assert iq_metadata.find_request(wav, _meta(), tmp_path / "none.json").status == "conflict"


def test_legacy_window_manifest_gives_the_window(tmp_path):
    wav = tmp_path / "iq_w_0001.wav"
    wav.write_bytes(b"")
    _manifest(
        tmp_path, "iq_w", 3_300_000_000, 3_800_000_000,
        window_count=5, window_index=2, window_start_hz=3_400_000_000, window_stop_hz=3_500_000_000,
    )
    found = iq_metadata.find_request(wav, _meta(centre=3.45e9, bw=100e6), tmp_path / "none.json")
    req = found.request
    assert (req.start_hz, req.stop_hz, req.window_index, req.window_count) == (3.4e9, 3.5e9, 2, 5)
    assert (req.allocation_start_hz, req.allocation_stop_hz) == (3.3e9, 3.8e9)


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------
def test_1805_to_1880_example_on_an_absolute_axis(tmp_path):
    """The operator's example at its real geometry: centre 1842.5 MHz, 125 MS/s, request 1805-1880 MHz."""
    fs = 125_000_000
    centre = 1_842_500_000.0
    wav = _crfs_wav(tmp_path / "iq_20261105_164741_0001.wav", fs, centre, 75e6, tones=((10e6, 0.5),))
    _manifest(tmp_path, "iq_20261105_164741", 1_805_000_000, 1_880_000_000, centre_hz=1_842_500_000)
    stats = _convert(wav, tmp_path / "none.json")

    axis = stats["frequency_axis"]
    assert axis["absolute"] and axis["centre_hz"] == centre and axis["unit"] == "MHz"
    assert axis["x_min"] == 1780.0 and axis["x_max"] == 1905.0
    assert axis["xlabel"] == "Frequency (MHz)"
    assert stats["file_meta"]["centre_hz"] == centre
    assert abs(stats["peak_rf_hz"] - 1_852_500_000.0) <= stats["df_hz"]

    lines = _lines(stats, "requested")
    assert len(lines) == 4 and not _lines(stats, "sensor")  # sensor range is the same: one pair only
    for boundary, mhz, offset in (("start", 1805.0, -37.5e6), ("end", 1880.0, 37.5e6)):
        drawn = [line for line in lines if line["boundary"] == boundary]
        assert {line["axes"] for line in drawn} == {"spectrum", "spectrogram"}
        for line in drawn:
            assert line["xdata"] == [mhz, mhz]
            assert line["hz"] - centre == offset

    assert _tag_texts(stats) == ["Requested start 1805 MHz", "Requested end 1880 MHz"]
    legend = stats["range_legend"]
    assert legend[0].startswith("Requested range: start 1805 MHz · end 1880 MHz · bandwidth 75 MHz")
    assert any("Centre 1842.5 MHz (file metadata) · Sample rate 125 MS/s" in t for t in legend)
    assert any(t.startswith("Displayed span 125 MHz, 1780–1905 MHz") for t in legend)
    assert any("do not prove uniform sensitivity, exact filter edges or complete capture" in t for t in legend)
    assert not any("Bandwidth: 125" in t or "bandwidth 125" in t for t in legend)
    _assert_clean_layout(stats)


@pytest.mark.parametrize(
    "fs, centre, sensor_bw, start, stop",
    [
        (3_906_250, 411e6, 2e6, 410e6, 412e6),
        (15_625_000, 1_621e6, 10e6, 1_616e6, 1_626e6),
        (31_250_000, 511e6, 22e6, 500e6, 522e6),
        (125_000_000, 3_450e6, 100e6, 3_400e6, 3_500e6),
    ],
)
def test_other_sample_rates_and_requested_bandwidths(tmp_path, fs, centre, sensor_bw, start, stop):
    wav = _crfs_wav(tmp_path / "iq_s_0001.wav", fs, centre, sensor_bw, seconds=max(0.004, 200_000 / fs))
    _manifest(tmp_path, "iq_s", start, stop)
    stats = _convert(wav, tmp_path / "none.json")
    axis = stats["frequency_axis"]
    assert axis["x_min"] == (centre - fs / 2) / 1e6 and axis["x_max"] == (centre + fs / 2) / 1e6
    assert [(b["kind"], b["x"], b["in_view"]) for b in stats["range_boundaries"]] == [
        ("requested", start / 1e6, True),
        ("requested", stop / 1e6, True),
    ]
    for boundary, hz in (("start", start), ("end", stop)):
        drawn = [line for line in _lines(stats, "requested") if line["boundary"] == boundary]
        assert [line["xdata"] for line in drawn] == [[hz / 1e6, hz / 1e6]] * 2
    assert not _lines(stats, "sensor")
    assert _tag_texts(stats) == [f"Requested start {start / 1e6:g} MHz", f"Requested end {stop / 1e6:g} MHz"]
    _assert_clean_layout(stats)


def test_request_and_sensor_range_differ_both_are_drawn_with_distinct_styles(tmp_path):
    wav = _crfs_wav(tmp_path / "iq_d.wav", 10_000_000, 100e6, 8e6, seconds=0.02)
    _manifest(tmp_path, "iq_d", 97_000_000, 101_000_000)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0)
    req, sen = _lines(stats, "requested"), _lines(stats, "sensor")
    assert sorted(line["xdata"][0] for line in req) == [97.0, 97.0, 101.0, 101.0]
    assert sorted(line["xdata"][0] for line in sen) == [96.0, 96.0, 104.0, 104.0]
    assert req[0]["color"] != sen[0]["color"] and req[0]["dashes"] != sen[0]["dashes"]
    assert stats["request_matches_sensor"] is False
    legend = stats["range_legend"]
    assert legend[0].startswith("Requested range: start 97 MHz · end 101 MHz · bandwidth 4 MHz")
    assert legend[1].startswith("Sensor-reported range: 96–104 MHz · sensor-reported bandwidth 8 MHz")
    texts = _tag_texts(stats)
    assert "Requested start 97 MHz" in texts and "Requested end 101 MHz" in texts
    assert "Sensor-reported 96 MHz" in texts and "Sensor-reported 104 MHz" in texts
    _assert_clean_layout(stats)


@pytest.mark.parametrize(
    "start, stop, expect_lines, expect_tags",
    [
        (90e6, 110e6, [], ["◀ Requested start 90 MHz (outside view)", "Requested end 110 MHz (outside view) ▶"]),
        (97e6, 110e6, [97.0], ["Requested start 97 MHz", "Requested end 110 MHz (outside view) ▶"]),
        (80e6, 90e6, [], None),
    ],
)
def test_boundaries_outside_the_span_get_no_line_only_an_edge_tag(tmp_path, start, stop, expect_lines, expect_tags):
    wav = _crfs_wav(tmp_path / "iq_o.wav", 10_000_000, 100e6, 8e6, seconds=0.02)
    _manifest(tmp_path, "iq_o", start, stop)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0)
    lo, hi = stats["frequency_axis"]["x_min"], stats["frequency_axis"]["x_max"]
    req = _lines(stats, "requested")
    assert sorted({line["xdata"][0] for line in req}) == expect_lines
    assert all(lo <= line["xdata"][0] <= hi for line in _lines(stats))
    assert not any(line["xdata"][0] in (lo, hi) for line in req), "a boundary was drawn at the edge"
    rows = {row["kind"]: row for row in stats["range_tag_rows"]}
    if expect_tags is not None:
        assert rows["requested"]["texts"] == expect_tags
    else:
        assert rows["requested"]["texts"] == ["◀ Requested 80–90 MHz (outside view)"] or rows["requested"]["texts"] == [
            "◀ Requested start 80 MHz (outside view)",
            "◀ Requested end 90 MHz (outside view)",
        ]
    assert "outside the displayed span" in stats["range_legend"][0]
    _assert_clean_layout(stats)


def test_wide_request_at_125_msps_shows_edge_tags_and_the_sensor_range(tmp_path):
    """Geometry of a real 3000-3200 MHz request recorded at centre 3100 MHz with 100 MHz sensor bandwidth."""
    wav = _crfs_wav(tmp_path / "iq_20260916_090319_0001.wav", 125_000_000, 3_100e6, 100e6)
    hist = _history(tmp_path / "h.json", {"iq_stem": "iq_20260916_090319", "start_hz": 3_000_000_000, "end_hz": 3_200_000_000})
    stats = _convert(wav, hist)
    assert stats["request_lookup"]["request"]["source"] == "Recorder history"
    assert stats["frequency_axis"]["x_min"] == 3037.5 and stats["frequency_axis"]["x_max"] == 3162.5
    assert not _lines(stats, "requested")
    assert sorted(line["xdata"][0] for line in _lines(stats, "sensor")) == [3050.0, 3050.0, 3150.0, 3150.0]
    rows = {row["kind"]: row["texts"] for row in stats["range_tag_rows"]}
    assert rows["requested"] == ["◀ Requested start 3000 MHz (outside view)", "Requested end 3200 MHz (outside view) ▶"]
    assert rows["sensor"] == ["Sensor-reported 3050 MHz", "Sensor-reported 3150 MHz"]
    _assert_clean_layout(stats)


def test_without_a_request_the_sensor_range_is_labelled_as_such(tmp_path):
    wav = _crfs_wav(tmp_path / "file_200740_0001.wav", 3_906_250, 411e6, 2e6, seconds=0.05)
    stats = _convert(wav, tmp_path / "none.json")
    assert not _lines(stats, "requested")
    assert sorted(line["xdata"][0] for line in _lines(stats, "sensor")) == [410.0, 410.0, 412.0, 412.0]
    legend = stats["range_legend"]
    assert legend[0].startswith("Requested range: not available")
    assert legend[1].startswith("Sensor-reported range: 410–412 MHz")
    assert not any(t.startswith("Requested start") or t.startswith("Requested end") for t in _tag_texts(stats))
    assert all(t.startswith("Sensor-reported") for t in _tag_texts(stats))
    assert any("not a request" in t for t in legend)
    _assert_clean_layout(stats)


def test_without_a_centre_the_axis_stays_relative_and_says_so(tmp_path):
    wav = _write_wav(tmp_path / "iq_nc_0001.wav", 1_000_000, 0.02)
    _manifest(tmp_path, "iq_nc", 1_805_000_000, 1_880_000_000)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0)
    axis = stats["frequency_axis"]
    assert not axis["absolute"] and axis["centre_hz"] is None
    assert (axis["x_min"], axis["x_max"], axis["unit"]) == (-500.0, 500.0, "kHz")
    assert "absolute frequency unknown" in axis["xlabel"]
    assert not stats["range_lines"]
    legend = stats["range_legend"]
    assert legend[0].startswith("Requested range 1805–1880 MHz (Recorder manifest): not marked, absolute frequency unknown")
    assert any(t.startswith("Centre frequency unknown") for t in legend)
    _assert_clean_layout(stats)


def test_inconsistent_meta_keeps_a_relative_axis(tmp_path):
    wav = _crfs_wav(tmp_path / "iq_bad.wav", 1_000_000, 100e6, 0.8e6, seconds=0.02, sample_rate=2_000_000)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0)
    assert not stats["frequency_axis"]["absolute"]
    assert "not used" in stats["frequency_axis"]["xlabel"]
    assert not stats["range_lines"]


def test_markers_can_be_turned_off_but_the_axis_stays_absolute(tmp_path):
    wav = _crfs_wav(tmp_path / "iq_off.wav", 10_000_000, 100e6, 8e6, seconds=0.02)
    _manifest(tmp_path, "iq_off", 97_000_000, 101_000_000)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0, range_markers=False)
    assert stats["frequency_axis"]["absolute"] and stats["frequency_axis"]["x_min"] == 95.0
    assert not stats["range_lines"] and not stats["range_legend"] and stats["layout"]["legend"] is None
    assert stats["request_lookup"] is None


@pytest.mark.parametrize("cmap", ["jet", "turbo", "viridis", "gray"])
def test_markers_and_layout_do_not_depend_on_the_colormap(tmp_path, cmap):
    wav = _crfs_wav(tmp_path / "iq_c.wav", 10_000_000, 100e6, 8e6, seconds=0.02)
    _manifest(tmp_path, "iq_c", 97_000_000, 101_000_000)
    stats = _convert(wav, tmp_path / "none.json", rbw=2_000.0, cmap=cmap)
    reference = _convert(wav, tmp_path / "none.json", rbw=2_000.0, cmap="jet") if cmap != "jet" else stats
    assert stats["range_lines"] == reference["range_lines"]
    assert stats["layout"] == reference["layout"]
    _assert_clean_layout(stats)


def test_spectrum_and_spectrogram_values_do_not_change(tmp_path):
    """Same samples with/without metadata, request and markers: identical arrays, image data unchanged."""
    fs = 10_000_000
    plain = _write_wav(tmp_path / "plain" / "iq_v_0001.wav", fs, 0.02, tones=((1.5e6, 0.3), (-2.25e6, 0.05)))
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    crfs = meta_dir / plain.name
    crfs.write_bytes(plain.read_bytes())
    _append_meta(crfs, 100e6, 8e6, fs)
    _manifest(meta_dir, "iq_v", 97_000_000, 101_000_000)
    none = tmp_path / "none.json"
    runs = [
        _convert(plain, none, rbw=2_000.0),
        _convert(crfs, none, rbw=2_000.0),
        _convert(crfs, none, rbw=2_000.0, range_markers=False),
        _convert(crfs, none, rbw=2_000.0, db_auto=False, db_vmin=-120, db_vmax=-20),
    ]
    assert runs[1]["frequency_axis"]["absolute"] and not runs[0]["frequency_axis"]["absolute"]
    assert len(runs[1]["range_lines"]) == 8
    for other in runs[1:]:
        for key in NUMERIC_KEYS:
            assert np.array_equal(runs[0][key], other[key]), key
        for key in SCALAR_KEYS:
            if key in ("vmin", "vmax") and other is runs[3]:
                continue
            assert runs[0][key] == other[key], key
    for run in runs:
        assert run["waterfall_image_is_data"]
    assert (runs[3]["vmin"], runs[3]["vmax"]) == (-120.0, -20.0)


def test_folder_batch_from_the_command_line_marks_every_file(tmp_path):
    folder = tmp_path / "batch"
    a = _crfs_wav(folder / "iq_ba_0001.wav", 10_000_000, 100e6, 8e6, seconds=0.02)
    _manifest(folder, "iq_ba", 97_000_000, 101_000_000)
    _crfs_wav(folder / "file_bb.wav", 10_000_000, 400e6, 6e6, seconds=0.02)
    _write_wav(folder / "iq_bc.wav", 1_000_000, 0.02)
    hist = _history(tmp_path / "h.json", {"iq_stem": "iq_bc", "start_hz": 1e9, "end_hz": 1.001e9})
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(HERE / "iq_data.py"), "--folder", str(folder), "--output", str(out), "--rbw", "2000",
         "--recorder-history", str(hist)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    for name in ("iq_ba_0001.png", "file_bb.png", "iq_bc.png"):
        assert (out / name).is_file(), name
    log = proc.stdout
    assert "Requested range: 97 - 101 MHz" in log
    assert "Sensor-reported: bandwidth 6 MHz, 397 - 403 MHz" in log
    assert "Requested range: not available (Recorder history lists 1000-1001 MHz for iq_bc" in log
    assert "Bandwidth      :" not in log
    assert a.is_file()
