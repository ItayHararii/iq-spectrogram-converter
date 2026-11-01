"""Merge engine: parsing, validation, byte-exact concatenation, RF64, failure paths.

The synthetic parts copy what the Node 40-8 writes (measured 1 Nov 2026 on
file_063929_0001..0008.wav): 44-byte RIFF header, PCM 16-bit stereo, then a
``meta`` chunk with <signalinfo> whose ``signaltime`` has 1 ms resolution.
"""

from __future__ import annotations

import json
import struct
import threading
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crfs_iq_recorder import iq_merge
from crfs_iq_recorder.iq_merge import (
    STATUS_BROKEN,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    MergeCancelled,
    MergeError,
    inspect_part,
    merge_parts,
    merged_output_path,
    plan_merge,
    read_sidecar,
)

FS = 1000
T0 = datetime(2026, 11, 1, 6, 39, 29, 1000, tzinfo=timezone.utc)


def _meta_xml(start: datetime, *, rf=5.11e8, bw=2.2e7, fs=FS, receiver="rfeye300540") -> bytes:
    stamp = start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"
    text = (
        '<?xml version="1.0" encoding="utf-8"?><signalinfo><base>'
        f"<signaltime>{stamp}</signaltime><rf_frequency>{rf:g}</rf_frequency><bandwidth>{bw:g}</bandwidth>"
        f"<data_type>complex</data_type><sample_rate>{fs:g}</sample_rate><if_frequency>0</if_frequency></base>"
        f"<extended><antenna>UID 1</antenna><receiver>{receiver}</receiver></extended></signalinfo>"
    )
    body = text.encode("utf-8")
    body += b"\x00" * (4 - len(body) % 4)  # the Node pads to a multiple of 4
    return body


def _payload(seed: int, frames: int) -> bytes:
    # Deterministic, non-repeating 16-bit I/Q values so misordering is detectable.
    values = []
    for n in range(frames):
        values.append(((seed * 7919 + n * 13) % 65536) - 32768)
        values.append(((seed * 104729 + n * 29) % 65536) - 32768)
    return struct.pack(f"<{len(values)}h", *values)


def write_part(
    path: Path,
    *,
    frames: int,
    start: datetime | None,
    seed: int,
    fs: int = FS,
    channels: int = 2,
    bits: int = 16,
    rf: float = 5.11e8,
    bw: float = 2.2e7,
    receiver: str = "rfeye300540",
    truncate_by: int = 0,
    meta: bool = True,
) -> bytes:
    block = channels * bits // 8
    data = _payload(seed, frames) if (channels, bits) == (2, 16) else bytes(frames * block)
    fmt = struct.pack("<HHIIHH", 1, channels, fs, fs * block, block, bits)
    trailer = b""
    if meta and start is not None:
        xml = _meta_xml(start, rf=rf, bw=bw, fs=fs, receiver=receiver)
        trailer = b"meta" + struct.pack("<I", len(xml)) + xml
    riff_size = 4 + 8 + 16 + 8 + len(data) + len(trailer)
    blob = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    blob += b"data" + struct.pack("<I", len(data)) + data + trailer
    if truncate_by:
        blob = blob[:-truncate_by]
    path.write_bytes(blob)
    return data


def make_recording(folder: Path, frames_per_part=(250, 250, 100), *, stem="file_063929", gap_ms=0.0, **kw) -> tuple[list[Path], bytes]:
    paths, payload, start = [], b"", T0
    for index, frames in enumerate(frames_per_part, start=1):
        path = folder / f"{stem}_{index:04d}.wav"
        payload += write_part(path, frames=frames, start=start, seed=index, **kw)
        paths.append(path)
        start = start + timedelta(seconds=frames / FS) + timedelta(milliseconds=gap_ms)
    return paths, payload


def test_inspect_part_reads_node_style_container(tmp_path):
    path = tmp_path / "file_063929_0001.wav"
    write_part(path, frames=250, start=T0, seed=1)
    info = inspect_part(path)
    assert info.ok
    assert info.container == "RIFF"
    assert info.part == 1 and info.stem == "file_063929"
    assert info.fmt.describe() == "PCM 16-bit, 2 ch, 1,000 S/s"
    assert info.data_offset == 44 and info.data_size == 1000 and info.frames == 250
    assert info.duration_s == 0.25
    assert info.meta is not None
    assert info.meta.signal_time == T0.replace(microsecond=1000)
    assert info.meta.rf_frequency_hz == 5.11e8 and info.meta.bandwidth_hz == 2.2e7
    assert info.meta.receiver == "rfeye300540"
    assert [cid for cid, _ in info.trailer] == [b"meta"]


def test_merge_three_parts_is_byte_exact_and_readable(tmp_path):
    paths, payload = make_recording(tmp_path)
    originals = [(p.stat().st_size, p.read_bytes()) for p in paths]
    plan = plan_merge(paths)
    assert plan.ok, plan.errors
    assert plan.names() == [p.name for p in paths]
    assert plan.total_frames == 600 and plan.duration_s == 0.6
    assert plan.continuity == STATUS_VERIFIED
    assert [j.status for j in plan.joins] == [STATUS_VERIFIED, STATUS_VERIFIED]
    assert plan.container == "RIFF"
    out = merged_output_path("file_063929", tmp_path)
    assert out.name == "file_063929_merged.wav"
    seen: list[tuple[int, int]] = []
    result = merge_parts(plan, out, progress=lambda d, t: seen.append((d, t)), chunk_bytes=300)
    assert result.output == out and out.exists()
    assert not out.with_name(out.name + ".partial").exists()
    assert result.frames == 600 and result.verified and result.container == "RIFF"
    assert seen[0] == (0, 2400) and seen[-1] == (2400, 2400)
    assert all(a <= b for (a, _), (b, _) in zip(seen, seen[1:]))
    # Sample payload is the parts' payloads back to back, untouched.
    blob = out.read_bytes()
    info = inspect_part(out)
    assert info.ok and info.frames == 600
    assert blob[info.data_offset:info.data_offset + info.data_size] == payload
    assert info.meta is not None and info.meta.signal_time == T0.replace(microsecond=1000)
    assert [cid for cid, _ in info.trailer] == [b"meta", b"LIST"]
    assert b"Merged from 3 parts" in blob
    # A plain WAVE reader (the stdlib) sees one continuous stereo 16-bit file.
    with wave.open(str(out), "rb") as wav:
        assert wav.getnchannels() == 2 and wav.getsampwidth() == 2 and wav.getframerate() == FS
        assert wav.getnframes() == 600
        assert wav.readframes(600) == payload
    # Originals untouched.
    assert [(p.stat().st_size, p.read_bytes()) for p in paths] == originals
    side = read_sidecar(out)
    assert side is not None
    assert side["merged_file"] == out.name and side["total_frames"] == 600
    assert side["continuity"] == STATUS_VERIFIED
    assert [p["name"] for p in side["parts"]] == plan.names()
    assert side["tuning"]["rf_frequency_hz"] == 5.11e8
    assert side["format"] == {"encoding": "PCM", "channels": 2, "bits": 16, "sample_rate": FS, "channel_order": "I, Q"}
    assert side["verification"]["samples_modified"] is False


def test_parts_are_sorted_numerically_not_lexically(tmp_path):
    frames = [10] * 12
    paths, payload = make_recording(tmp_path, frames_per_part=frames)
    shuffled = [paths[9], paths[1], paths[11], paths[0]] + [p for p in paths if p not in {paths[9], paths[1], paths[11], paths[0]}]
    plan = plan_merge(shuffled)
    assert plan.ok, plan.errors
    assert [p.part for p in plan.parts] == list(range(1, 13))
    out = merge_parts(plan, tmp_path / "x_merged.wav")
    info = inspect_part(out.output)
    assert out.output.read_bytes()[info.data_offset:info.data_offset + info.data_size] == payload


def test_duplicate_missing_and_single_parts_are_refused(tmp_path):
    paths, _ = make_recording(tmp_path)
    dup = tmp_path / "copy" / paths[1].name
    dup.parent.mkdir()
    dup.write_bytes(paths[1].read_bytes())
    plan = plan_merge(paths + [dup])
    assert not plan.ok
    assert any("present twice" in e for e in plan.errors)
    plan = plan_merge([paths[0], paths[2]])
    assert any("Missing part(s) 0002" in e for e in plan.errors)
    plan = plan_merge([paths[0]])
    assert any("Only one part" in e for e in plan.errors)
    with pytest.raises(MergeError):
        merge_parts(plan, tmp_path / "never_merged.wav")
    assert not (tmp_path / "never_merged.wav").exists()


def test_incompatible_parts_are_refused(tmp_path):
    paths, _ = make_recording(tmp_path)
    # Different stem.
    other = tmp_path / "file_070000_0004.wav"
    write_part(other, frames=10, start=T0, seed=9)
    plan = plan_merge(paths + [other])
    assert any("different recordings" in e for e in plan.errors)
    # Different sample format.
    write_part(paths[2], frames=100, start=T0 + timedelta(seconds=0.5), seed=3, fs=2000)
    plan = plan_merge(paths)
    assert any("S/s" in e and paths[2].name in e for e in plan.errors)
    # Different tuning (frequency window).
    write_part(paths[2], frames=100, start=T0 + timedelta(seconds=0.5), seed=3, rf=6.0e8)
    plan = plan_merge(paths)
    assert any("different tuning" in e for e in plan.errors)
    # Different receiver.
    write_part(paths[2], frames=100, start=T0 + timedelta(seconds=0.5), seed=3, receiver="rfeye000001")
    plan = plan_merge(paths)
    assert any("different receivers" in e for e in plan.errors)
    # Not a WAVE file at all.
    paths[2].write_bytes(b"not a wave file at all, just text" * 4)
    plan = plan_merge(paths)
    assert any("not a WAVE file" in e for e in plan.errors)


def test_truncated_or_unfinished_part_is_refused(tmp_path):
    paths, _ = make_recording(tmp_path)
    write_part(paths[1], frames=250, start=T0 + timedelta(seconds=0.25), seed=2, truncate_by=500)
    info = inspect_part(paths[1])
    assert any("truncated" in p for p in info.problems)
    plan = plan_merge(paths)
    assert any("truncated" in e for e in plan.errors)
    # A part the Node is still writing has a zero-length data chunk.
    blob = bytearray(paths[1].read_bytes())
    struct.pack_into("<I", blob, 40, 0)
    paths[1].write_bytes(bytes(blob))
    plan = plan_merge(paths)
    assert any("no samples" in e for e in plan.errors)


def test_gap_and_overlap_between_parts_block_the_merge(tmp_path):
    paths, _ = make_recording(tmp_path, gap_ms=5)
    plan = plan_merge(paths)
    assert plan.continuity == STATUS_BROKEN
    assert [j.status for j in plan.joins] == ["gap", "gap"]
    assert any("Gap of about 5.0 ms (5 samples)" in e for e in plan.errors)
    paths, _ = make_recording(tmp_path, gap_ms=-3)
    plan = plan_merge(paths)
    assert [j.status for j in plan.joins] == ["overlap", "overlap"]
    assert any("3.0 ms (3 samples) before" in e for e in plan.errors)
    # One millisecond of rounding is normal for the Node's time stamps.
    paths, _ = make_recording(tmp_path, gap_ms=1)
    plan = plan_merge(paths)
    assert plan.ok and plan.continuity == STATUS_VERIFIED


def test_parts_without_meta_merge_with_unknown_continuity(tmp_path):
    paths, payload = make_recording(tmp_path, meta=False)
    plan = plan_merge(paths)
    assert plan.ok, plan.errors
    assert plan.continuity == STATUS_UNKNOWN
    assert any("cannot be verified" in w for w in plan.warnings)
    result = merge_parts(plan, tmp_path / "file_063929_merged.wav")
    assert result.continuity == STATUS_UNKNOWN
    side = read_sidecar(result.output)
    assert side["continuity"] == STATUS_UNKNOWN and "not verified" in side["continuity_note"]
    info = inspect_part(result.output)
    assert info.meta is None and [cid for cid, _ in info.trailer] == [b"LIST"]
    assert result.output.read_bytes()[info.data_offset:info.data_offset + info.data_size] == payload


def test_rf64_output_header_describes_the_payload(tmp_path):
    paths, payload = make_recording(tmp_path)
    plan = plan_merge(paths, force_rf64=True)
    assert plan.container == "RF64"
    result = merge_parts(plan, tmp_path / "file_063929_merged.wav")
    blob = result.output.read_bytes()
    assert blob[:4] == b"RF64" and blob[4:8] == b"\xff\xff\xff\xff" and blob[8:12] == b"WAVE"
    assert blob[12:16] == b"ds64"
    riff_lo, riff_hi, data_lo, data_hi, frames_lo, frames_hi, table = struct.unpack("<IIIIIII", blob[20:48])
    assert (riff_lo | riff_hi << 32) == len(blob) - 8
    assert (data_lo | data_hi << 32) == 2400 and (frames_lo | frames_hi << 32) == 600 and table == 0
    info = inspect_part(result.output)
    assert info.ok and info.container == "RF64" and info.frames == 600
    assert blob[info.data_offset:info.data_offset + 2400] == payload
    assert blob[info.data_offset - 8:info.data_offset - 4] == b"data"
    assert blob[info.data_offset - 4:info.data_offset] == b"\xff\xff\xff\xff"


def test_riff_or_rf64_is_chosen_from_the_output_size(tmp_path, monkeypatch):
    paths, _ = make_recording(tmp_path)
    assert plan_merge(paths).container == "RIFF"
    monkeypatch.setattr(iq_merge, "RIFF_MAX_BYTES", 2000)
    assert plan_merge(paths).container == "RF64"


def test_insufficient_disk_space_stops_before_writing(tmp_path, monkeypatch):
    paths, _ = make_recording(tmp_path)
    plan = plan_merge(paths)
    out = tmp_path / "file_063929_merged.wav"

    class Usage:
        free = 10

    monkeypatch.setattr(iq_merge.shutil, "disk_usage", lambda _p: Usage())
    with pytest.raises(MergeError, match="Not enough free space"):
        merge_parts(plan, out)
    assert not out.exists() and not out.with_name(out.name + ".partial").exists()


def test_cancel_leaves_no_output(tmp_path):
    paths, _ = make_recording(tmp_path)
    plan = plan_merge(paths)
    out = tmp_path / "file_063929_merged.wav"
    cancel = threading.Event()

    def progress(done, _total):
        if done >= 1000:
            cancel.set()

    with pytest.raises(MergeCancelled):
        merge_parts(plan, out, progress=progress, cancel=cancel, chunk_bytes=500)
    assert not out.exists() and not out.with_name(out.name + ".partial").exists()
    assert not iq_merge.sidecar_path(out).exists()


def test_failed_verification_leaves_no_output(tmp_path, monkeypatch):
    paths, _ = make_recording(tmp_path)
    plan = plan_merge(paths)
    out = tmp_path / "file_063929_merged.wav"
    monkeypatch.setattr(iq_merge, "_verify_output", lambda *_a, **_k: False)
    with pytest.raises(MergeError, match="Verification failed"):
        merge_parts(plan, out)
    assert not out.exists() and not out.with_name(out.name + ".partial").exists()


def test_existing_output_is_protected_and_names_stay_unique(tmp_path):
    paths, _ = make_recording(tmp_path)
    plan = plan_merge(paths)
    out = tmp_path / "file_063929_merged.wav"
    out.write_bytes(b"precious")
    with pytest.raises(MergeError, match="already exists"):
        merge_parts(plan, out)
    assert out.read_bytes() == b"precious"
    assert merged_output_path("file_063929", tmp_path).name == "file_063929_merged (2).wav"
    (tmp_path / "file_063929_merged (2).wav.partial").write_bytes(b"")
    assert merged_output_path("file_063929", tmp_path).name == "file_063929_merged (3).wav"
    with pytest.raises(MergeError, match="numbered part"):
        merge_parts(plan, tmp_path / "file_063929_0009.wav")


def test_stale_partial_from_a_crash_is_replaced(tmp_path):
    paths, payload = make_recording(tmp_path)
    plan = plan_merge(paths)
    out = tmp_path / "file_063929_merged.wav"
    out.with_name(out.name + ".partial").write_bytes(b"half written")
    result = merge_parts(plan, out)
    assert result.output.exists() and not out.with_name(out.name + ".partial").exists()
    info = inspect_part(out)
    assert out.read_bytes()[info.data_offset:info.data_offset + info.data_size] == payload


def test_merged_output_is_not_mistaken_for_a_part():
    from crfs_iq_recorder.recording_history import iq_group_key, iq_part_number

    assert iq_merge.part_number("file_063929_merged.wav") is None
    assert iq_part_number("file_063929_merged.wav") is None
    # Not grouped with the parts of file_063929 by either application.
    assert iq_group_key("file_063929_merged.wav") != iq_group_key("file_063929_0001.wav")
    assert iq_merge.is_merged_name("file_063929_merged (2).wav")
    assert not iq_merge.is_merged_name("file_063929_0001.wav")


def test_second_local_copies_of_parts_merge_under_the_recording_name(tmp_path):
    """A repeated download lands in "name (2).wav"; the parts are still recognised."""
    assert iq_merge.part_number("file_063929_0002 (2).wav") == 2
    assert iq_merge.part_stem("file_063929_0002 (2).wav") == "file_063929"
    paths, payload = make_recording(tmp_path)
    copies = []
    for path in paths:
        copy = path.with_name(f"{path.stem} (2){path.suffix}")
        copy.write_bytes(path.read_bytes())
        copies.append(copy)
    plan = iq_merge.plan_merge(copies)
    assert plan.ok, plan.errors
    assert plan.stem == "file_063929"
    output = iq_merge.merged_output_path(plan.stem, tmp_path)
    assert output.name == "file_063929_merged.wav"
    result = iq_merge.merge_parts(plan, output)
    info = iq_merge.inspect_part(output)
    assert result.frames == 600
    assert output.read_bytes()[info.data_offset:info.data_offset + info.data_size] == payload


def test_plan_describe_and_json_are_serialisable(tmp_path):
    paths, _ = make_recording(tmp_path)
    plan = plan_merge(paths)
    text = plan.describe()
    assert "3 parts, 0.600 s" in text and "continuity verified" in text
    json.dumps([j.as_dict() for j in plan.joins])
    json.dumps([p.summary() for p in plan.parts])
