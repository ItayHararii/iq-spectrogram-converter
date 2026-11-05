"""Frequency facts for an IQ WAV: what the file reports, and what was requested.

Two sources, kept apart on purpose:

- The file's own CRFS ``meta`` chunk (centre, sensor-reported bandwidth,
  sample rate, receiver). Only the RIFF chunk headers and that trailer are
  read; the samples are not touched.
- The request that produced the recording, from the CRFS IQ Recorder: the
  ``<stem>.cellular.json`` manifest next to the WAV, or the Recorder's
  recording history. A request is used only when the match is unambiguous;
  otherwise it is reported as unavailable.

Nothing is inferred from the sample rate, digits in the file name, or the
picture. Every file is opened read-only, with sharing that never blocks the
Recorder from replacing its own files.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

HISTORY_ENV = "IQ_RECORDER_HISTORY"
RECORDER_FOLDER = "CRFS IQ Recorder"
HISTORY_NAME = ".crfs_iq_recorder_recordings.json"
SIDECAR_SUFFIX = ".cellular.json"
# The values are whole Hz written as text ("1.8425e+09"); this only absorbs parsing.
HZ_TOLERANCE = 0.5
_META_MAX_BYTES = 1 << 20
_JSON_MAX_BYTES = 64 << 20

_XML_FIELD = re.compile(r"<(?P<tag>[A-Za-z_][\w.-]*)>(?P<value>[^<]*)</(?P=tag)>")
_COPY_NUMBER = re.compile(r" \(\d+\)$")
_COPY_WORD = re.compile(r" - Copy$", re.IGNORECASE)
_MERGED = re.compile(r"_merged$", re.IGNORECASE)
_PART = re.compile(r"_\d{4}$")

STATUS_MATCHED = "matched"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_RECORDS = "no_records"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_CONFLICT = "conflict"
STATUS_UNVERIFIED = "unverified"


def _read_shared(path, limit):
    """Bytes of a small file, opened so a concurrent os.replace by its owner still succeeds."""
    path = os.fspath(path)
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateFileW
            create.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            create.restype = wintypes.HANDLE
            generic_read = 0x80000000
            share_all = 0x1 | 0x2 | 0x4  # read, write, delete
            open_existing = 3
            handle = create(path, generic_read, share_all, None, open_existing, 0x80, None)
            if handle and handle != wintypes.HANDLE(-1).value:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as fh:
                    return fh.read(limit + 1)
            err = ctypes.get_last_error()
            if err in (2, 3):
                raise FileNotFoundError(path)
        except FileNotFoundError:
            raise
        except Exception:
            pass
    with open(path, "rb") as fh:
        return fh.read(limit + 1)


def _num(value):
    """Finite float, or None for missing / "unknown" / text that is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _known_text(value):
    text = str(value or "").strip()
    return "" if text.casefold() in ("", "unknown", "none", "null", "n/a") else text


def _section(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def same_hz(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) <= HZ_TOLERANCE


def recording_stem(name):
    """The Recorder's name for a recording: drops ` (2)`, ` - Copy`, `_merged` and `_0001`."""
    stem = Path(str(name)).stem
    stem = _COPY_NUMBER.sub("", stem)
    stem = _COPY_WORD.sub("", stem)
    stem = _COPY_NUMBER.sub("", stem)
    stem = _MERGED.sub("", stem)
    stem = _COPY_NUMBER.sub("", stem)
    return _PART.sub("", stem)


# ---------------------------------------------------------------------------
# The file's own metadata
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileMeta:
    present: bool = False
    centre_hz: float | None = None
    bandwidth_hz: float | None = None
    sample_rate_hz: float | None = None
    data_type: str = ""
    receiver: str = ""
    problem: str = ""

    @property
    def centre_verified(self):
        """Centre usable for an absolute axis: present, positive, and consistent with the WAV header."""
        return self.centre_hz is not None and not self.problem

    @property
    def sensor_bandwidth_hz(self):
        return self.bandwidth_hz if not self.problem else None

    def as_dict(self):
        return {
            "present": self.present,
            "centre_hz": self.centre_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "data_type": self.data_type,
            "receiver": self.receiver,
            "problem": self.problem,
            "centre_verified": self.centre_verified,
        }


def _meta_chunk(path):
    """Body of the RIFF ``meta`` chunk, or None. Walks chunk headers only."""
    with open(os.fspath(path), "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        size = os.fstat(fh.fileno()).st_size
        pos = 12
        while pos + 8 <= size:
            fh.seek(pos)
            hdr = fh.read(8)
            if len(hdr) < 8:
                return None
            chunk_id = hdr[:4]
            (length,) = struct.unpack("<I", hdr[4:])
            body = pos + 8
            if chunk_id == b"meta":
                if length > _META_MAX_BYTES:
                    return None
                return fh.read(min(length, max(size - body, 0)))
            nxt = body + length + (length & 1)
            if nxt <= pos:
                return None
            pos = nxt
    return None


def read_file_meta(path, header_sample_rate=None):
    """CRFS ``<signalinfo>`` values of one WAV. Location fields are not parsed."""
    try:
        body = _meta_chunk(path)
    except OSError:
        body = None
    if not body:
        return FileMeta()
    text = body.replace(b"\x00", b"").decode("utf-8", errors="replace")
    base = text
    m = re.search(r"<base>(?P<b>.*?)</base>", text, re.DOTALL)
    if m:
        base = m.group("b")
    fields = {f.group("tag"): f.group("value").strip() for f in _XML_FIELD.finditer(base)}
    rec = re.search(r"<receiver>(?P<v>[^<]*)</receiver>", text)
    centre = _num(fields.get("rf_frequency"))
    bandwidth = _num(fields.get("bandwidth"))
    sample_rate = _num(fields.get("sample_rate"))
    data_type = fields.get("data_type", "").strip()
    if centre is not None and centre <= 0:
        centre = None
    if bandwidth is not None and bandwidth <= 0:
        bandwidth = None
    problem = ""
    if data_type and data_type.casefold() != "complex":
        problem = f"metadata says data type '{data_type}', not complex IQ"
    elif (
        sample_rate is not None
        and header_sample_rate
        and abs(float(sample_rate) - float(header_sample_rate)) > HZ_TOLERANCE
    ):
        problem = (
            f"metadata sample rate {sample_rate:,.0f} S/s differs from the WAV header "
            f"{float(header_sample_rate):,.0f} S/s"
        )
    return FileMeta(
        present=True,
        centre_hz=centre,
        bandwidth_hz=bandwidth,
        sample_rate_hz=sample_rate,
        data_type=data_type,
        receiver=_known_text(rec.group("v")) if rec else "",
        problem=problem,
    )


# ---------------------------------------------------------------------------
# The request (CRFS IQ Recorder records)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Request:
    start_hz: float
    stop_hz: float
    source: str
    detail: str = ""
    window_index: int | None = None
    window_count: int | None = None
    allocation_start_hz: float | None = None
    allocation_stop_hz: float | None = None

    @property
    def bandwidth_hz(self):
        return float(self.stop_hz) - float(self.start_hz)

    @property
    def centre_hz(self):
        return 0.5 * (float(self.start_hz) + float(self.stop_hz))

    @property
    def is_window(self):
        return bool(self.window_count and self.window_count > 1)

    def as_dict(self):
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "source": self.source,
            "detail": self.detail,
            "window_index": self.window_index,
            "window_count": self.window_count,
            "allocation_start_hz": self.allocation_start_hz,
            "allocation_stop_hz": self.allocation_stop_hz,
        }


@dataclass(frozen=True)
class RequestLookup:
    request: Request | None
    status: str
    note: str = ""
    history_path: str = ""

    def as_dict(self):
        return {
            "status": self.status,
            "note": self.note,
            "history_path": self.history_path,
            "request": self.request.as_dict() if self.request else None,
        }


def default_history_path():
    """This PC's Recorder history, if the Recorder was installed: %LOCALAPPDATA%\\CRFS IQ Recorder."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / RECORDER_FOLDER / HISTORY_NAME


def resolve_history_path(explicit=None):
    """Explicit setting, else the IQ_RECORDER_HISTORY variable, else this PC's default."""
    text = str(explicit or "").strip().strip('"')
    if text:
        return Path(text)
    env = os.environ.get(HISTORY_ENV, "").strip().strip('"')
    if env:
        return Path(env)
    return default_history_path()


_history_cache = {}
_history_lock = threading.Lock()


def load_history(path):
    """Recorder history entries (list of dicts), or None if the file is missing or unreadable."""
    if path is None:
        return None
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    with _history_lock:
        cached = _history_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        raw = _read_shared(path, _JSON_MAX_BYTES)
        if len(raw) > _JSON_MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        data = data.get("recordings")
    if not isinstance(data, list):
        return None
    records = [r for r in data if isinstance(r, dict)]
    with _history_lock:
        _history_cache[key] = (stamp, records)
    return records


def _mhz_text(hz):
    text = f"{float(hz) / 1e6:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _range_text(start, stop):
    return f"{_mhz_text(start)}-{_mhz_text(stop)} MHz"


def _valid_range(start, stop):
    return start is not None and stop is not None and start > 0 and stop > start


def _sidecar_request(wav_path, stem, meta):
    """(Request or None, status, note) from ``<stem>.cellular.json`` beside the WAV."""
    path = Path(wav_path).parent / f"{stem}{SIDECAR_SUFFIX}"
    if not path.is_file():
        return None, "", ""
    try:
        raw = _read_shared(path, _JSON_MAX_BYTES)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "unreadable", f"{path.name} could not be read"
    if not isinstance(data, dict) or data.get("kind") != "cellular_capture":
        return None, "unreadable", f"{path.name} is not a Recorder capture manifest"
    named = _known_text(_section(data, "identifiers").get("recording_stem"))
    if named.casefold() != stem.casefold():
        return None, "unreadable", f"{path.name} names another recording"
    serial = _known_text(_section(data, "sensor").get("serial"))
    if serial and meta.receiver and serial.casefold() != meta.receiver.casefold():
        return None, STATUS_CONFLICT, (
            f"{path.name} is for receiver {serial}, the file says {meta.receiver}"
        )
    recorded = _num(_section(data, "recording").get("recorded_center_hz"))
    if recorded is not None and meta.centre_hz is not None and not same_hz(recorded, meta.centre_hz):
        return None, STATUS_CONFLICT, (
            f"{path.name} records centre {_mhz_text(recorded)} MHz, the file says {_mhz_text(meta.centre_hz)} MHz"
        )
    alloc = _section(data, "allocation")
    start = _num(alloc.get("requested_start_hz"))
    stop = _num(alloc.get("requested_stop_hz"))
    count = _num(alloc.get("window_count"))
    count = int(count) if count is not None else None
    if count is not None and count > 1:
        w_start = _num(alloc.get("window_start_hz"))
        w_stop = _num(alloc.get("window_stop_hz"))
        index = _num(alloc.get("window_index"))
        if not _valid_range(w_start, w_stop):
            return None, "unreadable", f"{path.name} has no usable capture window"
        return (
            Request(
                start_hz=w_start,
                stop_hz=w_stop,
                source="Recorder manifest",
                detail=path.name,
                window_index=int(index) if index is not None else None,
                window_count=count,
                allocation_start_hz=start if _valid_range(start, stop) else None,
                allocation_stop_hz=stop if _valid_range(start, stop) else None,
            ),
            STATUS_MATCHED,
            "",
        )
    if not _valid_range(start, stop):
        return None, "unreadable", f"{path.name} has no usable requested start/stop"
    return Request(start_hz=start, stop_hz=stop, source="Recorder manifest", detail=path.name), STATUS_MATCHED, ""


def _history_entries(stem, meta, records):
    """Non-demo history entries with this stem, minus those made with another receiver."""
    key = stem.casefold()
    entries = [
        r for r in records
        if str(r.get("iq_stem") or "").strip().casefold() == key and not bool(r.get("demo"))
    ]
    if meta.receiver:
        entries = [
            r for r in entries
            if not _known_text(r.get("sensor_id"))
            or _known_text(r.get("sensor_id")).casefold() == meta.receiver.casefold()
        ]
    return entries


def _history_request(stem, meta, entries):
    """(Request or None, status, note) from the Recorder history entries for this stem."""
    if not entries:
        return None, STATUS_NOT_FOUND, f"no Recorder record for {stem}"
    ranges = {(_num(r.get("start_hz")), _num(r.get("end_hz"))) for r in entries}
    if len(ranges) > 1:
        listed = ", ".join(
            sorted(_range_text(a, b) for a, b in ranges if a is not None and b is not None)
        )
        return None, STATUS_AMBIGUOUS, f"{len(entries)} Recorder history entries for {stem} disagree ({listed})"
    start, stop = next(iter(ranges))
    if not _valid_range(start, stop):
        return None, STATUS_NOT_FOUND, f"the Recorder record for {stem} has no usable start/end"
    if not meta.centre_verified:
        return None, STATUS_UNVERIFIED, (
            f"Recorder history lists {_range_text(start, stop)} for {stem}, but the file has no "
            "centre-frequency metadata to confirm it"
        )
    for r in entries:
        centre = _num(r.get("center_hz"))
        if not same_hz(centre, meta.centre_hz):
            shown = f"{_mhz_text(centre)} MHz" if centre is not None else "no centre"
            return None, STATUS_CONFLICT, (
                f"the Recorder record for {stem} has {shown}, the file says {_mhz_text(meta.centre_hz)} MHz"
            )
    started = str(entries[0].get("started_at") or "").strip()
    detail = f"entry started {started}" if started else "entry"
    return Request(start_hz=start, stop_hz=stop, source="Recorder history", detail=detail), STATUS_MATCHED, ""


def find_request(wav_path, meta, history_path=None):
    """The request behind this WAV, or why it is unavailable.

    The manifest beside the WAV wins; the Recorder history is the fallback
    (WAVs are often copied elsewhere). A manifest is still refused when the
    history has entries for the same name and receiver and none of them agrees
    with it. Disagreement between a record and the file's metadata, or between
    history entries with the same name, makes the request unavailable rather
    than guessed.
    """
    stem = recording_stem(Path(wav_path).name)
    meta = meta if meta is not None else FileMeta()
    side, side_status, side_note = _sidecar_request(wav_path, stem, meta)
    hist_file = resolve_history_path(history_path)
    hist_text = str(hist_file) if hist_file else ""
    if side_status == STATUS_CONFLICT:
        return RequestLookup(None, STATUS_CONFLICT, side_note, hist_text)
    records = load_history(hist_file)
    entries = []
    if records is None:
        hist, hist_status, hist_note = None, STATUS_NO_RECORDS, "no Recorder history found"
    else:
        entries = _history_entries(stem, meta, records)
        hist, hist_status, hist_note = _history_request(stem, meta, entries)
    if side is not None:
        listed = [(_num(r.get("start_hz")), _num(r.get("end_hz"))) for r in entries]
        if listed and not any(same_hz(a, side.start_hz) and same_hz(b, side.stop_hz) for a, b in listed):
            shown = ", ".join(sorted({_range_text(a, b) for a, b in listed if a is not None and b is not None}))
            return RequestLookup(
                None,
                STATUS_CONFLICT,
                f"the Recorder manifest ({_range_text(side.start_hz, side.stop_hz)}) and history "
                f"({shown or 'no usable range'}) disagree",
                hist_text,
            )
        return RequestLookup(side, STATUS_MATCHED, "", hist_text)
    if hist is not None:
        return RequestLookup(hist, STATUS_MATCHED, "", hist_text)
    if hist_status in (STATUS_AMBIGUOUS, STATUS_CONFLICT, STATUS_UNVERIFIED):
        return RequestLookup(None, hist_status, hist_note, hist_text)
    if side_note:
        return RequestLookup(None, STATUS_NOT_FOUND, side_note, hist_text)
    if hist_status == STATUS_NO_RECORDS:
        return RequestLookup(None, STATUS_NO_RECORDS, f"no manifest beside the file and {hist_note}", hist_text)
    return RequestLookup(None, hist_status, hist_note, hist_text)
