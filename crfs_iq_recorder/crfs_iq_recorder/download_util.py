"""Safe local download paths. Never treat a .part file as a finished copy."""

from __future__ import annotations

from pathlib import Path


class DownloadError(Exception):
    """Local download finalization failed."""


def part_path(dest: Path) -> Path:
    dest = Path(dest)
    return dest.with_name(dest.name + ".part")


def unique_destination(dest: Path) -> Path:
    """Return dest, or dest with ' (n)' inserted before the suffix if it exists."""
    dest = Path(dest)
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    n = 2
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1
        if n > 10_000:
            raise DownloadError(f"Could not find a free name near {dest}.")


def cleanup_part(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def finalize_download(part: Path, dest: Path, *, expected_size: int = 0) -> None:
    """Move a complete .part file into place. Delete the part file on failure."""
    part = Path(part)
    dest = Path(dest)
    if not part.is_file():
        raise DownloadError("Download did not create a file.")
    actual = part.stat().st_size
    if actual <= 0:
        cleanup_part(part)
        raise DownloadError("Download produced an empty file.")
    if expected_size > 0 and actual < int(expected_size):
        cleanup_part(part)
        raise DownloadError(
            f"Incomplete download: got {actual} bytes, expected at least {int(expected_size)}."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            dest.unlink()
        except OSError as exc:
            cleanup_part(part)
            raise DownloadError(f"Could not replace existing file: {exc}") from exc
    try:
        part.replace(dest)
    except OSError as exc:
        cleanup_part(part)
        raise DownloadError(f"Could not save download: {exc}") from exc
