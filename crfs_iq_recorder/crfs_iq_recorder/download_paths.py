"""Local destinations that mirror the sensor's recording folders.

The sensor writes ``/mnt/1/remdata/<YYYYMMDD>/<file>``. A download of that file
goes to ``<download root>/<YYYYMMDD>/<file>``: the folder name is taken from
the remote path itself, never from this computer's date, so a recording that
starts before midnight and is downloaded after it lands in the sensor's own
folder. Meaningful sub-folders under the recording root are kept
(``/mnt/1/remdata/20261104/site-a/x.wav`` -> ``20261104/site-a/x.wav``).

Rules:
- every derived component is checked: ``..``, ``.``, empty names, drive letters,
  absolute paths and characters Windows refuses are rejected and the file goes
  to the download root instead (``remote_subfolder`` returns ``""``);
- a file already present at the mirrored destination with the sensor's size is
  the same file (``existing_copy``); a copy saved by an earlier version in the
  flat root is recognised where it is and never moved;
- a different file of the same name (another sensor, or a damaged copy) is
  never overwritten: the whole recording goes to ``<date>/<sensor>/`` and, if
  even that is taken by a different file, to a ``name (2)`` destination.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable

from .constants import SFTP_REMDATA_ROOT
from .download_util import unique_destination

_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_DATED = re.compile(r"\d{8}")
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def _component_ok(name: str) -> bool:
    text = str(name)
    if not text or text in {".", ".."}:
        return False
    if _BAD_CHARS.search(text) or "/" in text or "\\" in text:
        return False
    if text != text.strip() or text.endswith("."):
        return False
    if text.split(".")[0].casefold() in _RESERVED:
        return False
    return len(text) <= 120


def remote_name(remote_path: str) -> str:
    """The file name of a remote path ('' when the path has none)."""
    return str(remote_path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def remote_subfolder(remote_path: str, *, remote_root: str = SFTP_REMDATA_ROOT) -> str:
    """'20261104' or '20261104/site-a' for a file under the recording root; '' otherwise.

    '' also for anything that would leave the download root (``..``, absolute or
    drive-changing components, names Windows refuses).
    """
    text = str(remote_path or "").replace("\\", "/")
    if "/" not in text:
        return ""
    directory = text.rsplit("/", 1)[0] + "/"
    root = str(remote_root or "").replace("\\", "/")
    if not root.endswith("/"):
        root += "/"
    if directory.startswith(root):
        relative = directory[len(root):].strip("/")
    else:
        # Another storage root: keep the sensor's dated folder (YYYYMMDD) when that is the file's folder.
        last = directory.strip("/").rsplit("/", 1)[-1]
        relative = last if _DATED.fullmatch(last) else ""
    if not relative:
        return ""
    parts = relative.split("/")
    if not all(_component_ok(part) for part in parts):
        return ""
    return "/".join(parts)


def _inside(root: Path, candidate: Path) -> bool:
    """Lexical containment: the components are already vetted, so no file system round trip is needed.

    (``Path.resolve`` costs several system calls per path on Windows; a listing
    of hundreds of files asked this thousands of times per refresh.)
    """
    try:
        root_text = os.path.normcase(os.path.normpath(os.path.abspath(str(root))))
        cand_text = os.path.normcase(os.path.normpath(os.path.abspath(str(candidate))))
        return os.path.commonpath([root_text, cand_text]) == root_text
    except (OSError, ValueError):
        return False


def sensor_folder_name(sensor: str) -> str:
    """A folder name for a sensor identifier (serial, else host); '' when there is none."""
    text = _BAD_CHARS.sub("_", str(sensor or "").strip()).replace("/", "_").replace("\\", "_").strip(" .")
    return text[:60] if _component_ok(text) else ""


def local_folder(root: Path | str, remote_path: str, *, sensor: str = "") -> Path:
    """The local folder for a remote file: ``<root>/<dated folder>[/<sensor>]``, inside the root."""
    base = Path(root)
    sub = remote_subfolder(remote_path)
    folder = base / Path(*sub.split("/")) if sub else base
    if sensor:
        name = sensor_folder_name(sensor)
        if name:
            folder = folder / name
    if sub or sensor:
        if not _inside(base, folder):
            return base
    return folder


def local_destination(root: Path | str, remote_path: str, *, sensor: str = "") -> Path:
    """``<root>/<dated folder>/<file>`` for a remote file (``<root>/<file>`` when no folder can be derived)."""
    return local_folder(root, remote_path, sensor=sensor) / remote_name(remote_path)


def candidate_paths(root: Path | str, remote_path: str, *, sensor: str = "") -> list[Path]:
    """Where a copy of the remote file may already be: mirrored folder, sensor folder, then the flat root."""
    out: list[Path] = []
    for path in (
        local_destination(root, remote_path),
        local_destination(root, remote_path, sensor=sensor) if sensor else None,
        Path(root) / remote_name(remote_path),
    ):
        if path is not None and path not in out:
            out.append(path)
    return out


def _size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def existing_copy(root: Path | str, remote_path: str, size: int, *, sensor: str = "") -> Path | None:
    """A local file with the sensor's exact size at any of the known destinations, else None.

    Finds copies saved by earlier versions in the flat root, so they are
    recognised where they are and never downloaded or moved again.
    """
    expected = int(size or 0)
    if expected <= 0:
        return None
    for path in candidate_paths(root, remote_path, sensor=sensor):
        if _size_of(path) == expected:
            return path
    return None


class LocalCopies:
    """Finds local copies for a whole listing with one directory read per folder.

    ``existing_copy`` stats up to three paths per file; a Sensor Files listing
    of hundreds of files would spend most of its refresh in those stats. This
    reads each candidate folder once (``os.scandir``) and answers from the
    snapshot. Make a new instance per refresh so it never goes stale.
    """

    def __init__(self, root: Path | str, *, sensor: str = "") -> None:
        self._root = Path(root)
        self._sensor = sensor
        self._folders: dict[Path, dict[str, int]] = {}
        self._folders_for: dict[str, list[Path]] = {}  # remote directory -> local folders to look in

    def _sizes_in(self, folder: Path) -> dict[str, int]:
        cached = self._folders.get(folder)
        if cached is None:
            cached = {}
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        try:
                            if entry.is_file():
                                cached[entry.name] = entry.stat().st_size
                        except OSError:
                            continue
            except OSError:
                pass
            self._folders[folder] = cached
        return cached

    def find(self, remote_path: str, size: int) -> Path | None:
        """The same answer as ``existing_copy(root, remote_path, size, sensor=...)``."""
        expected = int(size or 0)
        if expected <= 0:
            return None
        text = str(remote_path or "").replace("\\", "/")
        directory, _, name = text.rpartition("/")
        if not name:
            return None
        folders = self._folders_for.get(directory)
        if folders is None:
            # The files of one listing share a remote folder: derive its local folders once.
            folders = []
            for path in candidate_paths(self._root, text, sensor=self._sensor):
                if path.parent not in folders:
                    folders.append(path.parent)
            self._folders_for[directory] = folders
        for folder in folders:
            if self._sizes_in(folder).get(name) == expected:
                return folder / name
        return None


def plan_destinations(
    root: Path | str,
    remote_paths: Iterable[str],
    sizes: dict[str, int] | Callable[[str], int],
    *,
    sensor: str = "",
) -> dict[str, Path]:
    """Destination per remote path for one recording (all parts share one folder).

    The mirrored folder is used unless a part's name is already taken there by
    a different file (size differs from the sensor's): then the whole
    recording goes to the sensor sub-folder, and a name still taken there
    becomes ``name (2)``. A file already present with the sensor's size is
    returned as its own destination (it is the file; the caller verifies it).
    """
    paths = [str(p) for p in remote_paths if p]
    if not paths:
        return {}

    def size_for(path: str) -> int:
        try:
            value = sizes(path) if callable(sizes) else sizes.get(path, 0)  # type: ignore[union-attr]
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    primary = {path: local_destination(root, path) for path in paths}
    clash = False
    for path, dest in primary.items():
        present = _size_of(dest)
        if present is not None and present != size_for(path):
            clash = True
            break
    if not clash or not sensor_folder_name(sensor):
        chosen = primary
    else:
        chosen = {path: local_destination(root, path, sensor=sensor) for path in paths}
    out: dict[str, Path] = {}
    for path, dest in chosen.items():
        present = _size_of(dest)
        if present is not None and present != size_for(path):
            dest = unique_destination(dest)
        out[path] = dest
    return out


__all__ = [
    "LocalCopies",
    "candidate_paths",
    "existing_copy",
    "local_destination",
    "local_folder",
    "plan_destinations",
    "remote_name",
    "remote_subfolder",
    "sensor_folder_name",
]
