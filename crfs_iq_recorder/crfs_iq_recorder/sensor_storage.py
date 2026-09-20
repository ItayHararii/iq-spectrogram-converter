"""IQ recording disk space on the sensor filesystem that holds /mnt/1/remdata/."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .constants import SFTP_REMDATA_ROOT
from .size_estimate import empirical_size_mb

STORAGE_WARN_RATIO = 0.10
STORAGE_CRITICAL_RATIO = 0.05
STORAGE_MARGIN_RATIO = 0.15
STORAGE_MARGIN_MIN_BYTES = 32 * 1024 * 1024
STORAGE_POLL_MS = 30_000
STORAGE_QUERY_PATHS = (SFTP_REMDATA_ROOT, "/mnt/1/", "/mnt/")

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
LEVEL_UNAVAILABLE = "unavailable"

DEMO_TOTAL_BYTES = 128 * 1024 * 1024 * 1024
DEMO_FREE_BYTES = int(18.4 * 1024 * 1024 * 1024)


@dataclass(frozen=True)
class StorageSnapshot:
    path: str = ""
    total_bytes: int = 0
    free_bytes: int = 0
    ok: bool = False
    stale: bool = False
    detail: str = ""

    @classmethod
    def unavailable(cls, detail: str = "") -> StorageSnapshot:
        return cls(ok=False, stale=False, detail=detail)

    def as_stale(self, detail: str = "") -> StorageSnapshot:
        if not self.ok or self.total_bytes <= 0:
            return self.unavailable(detail or self.detail)
        return StorageSnapshot(
            path=self.path,
            total_bytes=self.total_bytes,
            free_bytes=self.free_bytes,
            ok=True,
            stale=True,
            detail=detail or self.detail,
        )


def format_storage_bytes(size: int) -> str:
    """Readable MB, GB, or TB for the IQ Storage line."""
    value = float(max(int(size or 0), 0))
    tb = 1024.0 ** 4
    gb = 1024.0 ** 3
    mb = 1024.0 ** 2
    if value >= tb:
        return _trim(value / tb, 2) + " TB"
    if value >= gb:
        return _trim(value / gb, 1) + " GB"
    if value >= mb:
        return _trim(value / mb, 1) + " MB"
    if value >= 1024:
        return _trim(value / 1024.0, 1) + " KB"
    return f"{int(value)} B"


def _trim(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def snapshot_from_statvfs(path: str, attr) -> StorageSnapshot | None:
    frsize = int(getattr(attr, "st_frsize", 0) or getattr(attr, "st_bsize", 0) or 0)
    blocks = int(getattr(attr, "st_blocks", 0) or 0)
    bavail = int(getattr(attr, "st_bavail", 0) or getattr(attr, "st_bfree", 0) or 0)
    if frsize <= 0 or blocks <= 0:
        return None
    total = frsize * blocks
    free = frsize * max(0, bavail)
    if total <= 0:
        return None
    return StorageSnapshot(
        path=path,
        total_bytes=total,
        free_bytes=min(free, total),
        ok=True,
    )


def snapshot_from_df_text(text: str, path: str) -> StorageSnapshot | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    try:
        total_k = int(parts[1])
        avail_k = int(parts[3])
    except (TypeError, ValueError):
        return None
    total = total_k * 1024
    free = max(0, avail_k) * 1024
    if total <= 0:
        return None
    return StorageSnapshot(
        path=path,
        total_bytes=total,
        free_bytes=min(free, total),
        ok=True,
    )


def estimated_recording_bytes(bandwidth_hz: int, duration_s: float | Decimal | str) -> int:
    mb = empirical_size_mb(bandwidth_hz, duration_s)
    raw = (mb * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING)
    return max(int(raw), 0)


def needed_bytes_with_margin(estimated_bytes: int) -> int:
    estimate = max(int(estimated_bytes or 0), 0)
    extra = max(int(estimate * STORAGE_MARGIN_RATIO), STORAGE_MARGIN_MIN_BYTES)
    return estimate + extra


def warning_level(snapshot: StorageSnapshot | None) -> str:
    if snapshot is None or not snapshot.ok or snapshot.total_bytes <= 0:
        return LEVEL_UNAVAILABLE
    ratio = snapshot.free_bytes / snapshot.total_bytes
    if ratio < STORAGE_CRITICAL_RATIO:
        return LEVEL_CRITICAL
    if ratio < STORAGE_WARN_RATIO:
        return LEVEL_WARN
    return LEVEL_OK


def storage_line(snapshot: StorageSnapshot | None) -> str:
    if snapshot is None or not snapshot.ok or snapshot.total_bytes <= 0:
        return "Storage unavailable"
    text = f"{format_storage_bytes(snapshot.free_bytes)} free / {format_storage_bytes(snapshot.total_bytes)}"
    if snapshot.stale:
        return f"{text} (outdated)"
    return text


def storage_note(snapshot: StorageSnapshot | None) -> str:
    level = warning_level(snapshot)
    if snapshot is not None and snapshot.stale:
        return "Storage unavailable - last reading is outdated."
    if level == LEVEL_CRITICAL:
        return "Critical IQ storage: under 5% free."
    if level == LEVEL_WARN:
        return "Low IQ storage: under 10% free."
    if level == LEVEL_UNAVAILABLE:
        return "Storage unavailable"
    return ""


def recording_blocked_reason(snapshot: StorageSnapshot | None, estimated_bytes: int) -> str | None:
    if snapshot is None or snapshot.total_bytes <= 0:
        return None
    if not snapshot.ok and not snapshot.stale:
        return None
    needed = needed_bytes_with_margin(estimated_bytes)
    if snapshot.free_bytes >= needed:
        return None
    return (
        "Not enough IQ storage for this recording. "
        f"Need about {format_storage_bytes(needed)} free "
        "(estimate plus a safety margin for split files). "
        f"Sensor has {format_storage_bytes(snapshot.free_bytes)} free. "
        "Existing recordings were left in place."
    )


def query_iq_storage(browser) -> StorageSnapshot:
    """Read free space for the filesystem that contains /mnt/1/remdata/.

    Today's dated folder does not need to exist.
    """
    last_detail = ""
    for path in STORAGE_QUERY_PATHS:
        try:
            snap = browser.storage_usage(path)
        except Exception as exc:
            last_detail = str(exc)
            continue
        if snap is not None and snap.ok and snap.total_bytes > 0:
            return snap
        if snap is not None and snap.detail:
            last_detail = snap.detail
    return StorageSnapshot.unavailable(last_detail)
