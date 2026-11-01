"""Run merges in the background and remember which recordings were merged.

One merge runs at a time (the work is disk bound); further requests queue.
A registry in the user config folder records every finished merge so a
refresh of Sensor Files, a reconnect, or a restart does not merge the same
parts again. Recording, download and merge are tracked separately: nothing
here touches the recording state, and a failed merge only reports itself.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .iq_merge import (
    PARTIAL_SUFFIX,
    MergeCancelled,
    MergeError,
    MergePlan,
    MergeResult,
    canonical_part_name,
    is_merged_name,
    merge_parts,
    merged_output_path,
    plan_merge,
    read_sidecar,
)
from .paths import user_config_dir
from .worker_threads import track as track_worker_thread


def merge_history_path() -> Path:
    return user_config_dir() / "merge_history.json"


class MergeRegistry:
    """Which part sets have already been merged, and where the output went."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        path = self._path or merge_history_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self._data = raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        path = self._path or merge_history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def signature(paths: list[Path]) -> list[list]:
        """[sensor part name, size] per part. A second local download of the
        same sensor file ("name (2).wav") counts as the same part."""
        out = []
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            out.append([canonical_part_name(path.name), size])
        out.sort(key=lambda item: item[0])
        return out

    def find(self, group_key: str, paths: list[Path]) -> Path | None:
        """The existing merged file for exactly these parts, if it is still there."""
        entry = self._data.get(group_key)
        signature = self.signature(paths)
        if isinstance(entry, dict) and entry.get("parts") == signature:
            output = Path(str(entry.get("output") or ""))
            if output.is_file():
                return output
        # The registry may have been cleared: trust a sidecar next to the parts.
        if paths:
            folder = paths[0].parent
            for candidate in sorted(folder.glob("*_merged*.wav")):
                side = read_sidecar(candidate)
                if not side:
                    continue
                listed = sorted(
                    ([canonical_part_name(str(p.get("name") or "")), p.get("bytes")] for p in side.get("parts", [])),
                    key=lambda item: item[0],
                )
                if listed == signature and is_merged_name(candidate.name):
                    return candidate
        return None

    def record(self, group_key: str, paths: list[Path], output: Path) -> None:
        self._data[group_key] = {
            "output": str(output),
            "parts": self.signature(paths),
            "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save()


@dataclass
class MergeRequest:
    group_key: str
    stem: str
    paths: list[Path]
    folder: Path
    auto: bool
    plan: MergePlan | None = None
    output: Path | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


class MergeJob(QObject):
    # Byte counts: a merged file is routinely larger than 2 GiB, which does not
    # fit the C++ int that Signal(int, int) would marshal through.
    progress = Signal(object, object)
    finished = Signal(object)
    failed = Signal(str, bool)

    def __init__(self, request: MergeRequest) -> None:
        super().__init__()
        self._request = request
        self._last_emit = 0.0

    def abort(self) -> None:
        self._request.cancel.set()

    @Slot()
    def run(self) -> None:
        request = self._request
        try:
            assert request.plan is not None and request.output is not None

            def progress(done: int, total: int) -> None:
                now = time.monotonic()
                if done >= total or now - self._last_emit >= 0.2:
                    self._last_emit = now
                    self.progress.emit(done, total)

            result = merge_parts(request.plan, request.output, progress=progress, cancel=request.cancel)
        except MergeCancelled:
            self.failed.emit("Merge cancelled.", True)
        except MergeError as exc:
            self.failed.emit(str(exc), False)
        except Exception as exc:  # noqa: BLE001 - report, never crash the UI thread
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
        else:
            self.finished.emit(result)
        finally:
            thread = QThread.currentThread()
            if thread is not None:
                thread.quit()


class MergeController(QObject):
    """Queue of merge requests, one running at a time, with UI-facing signals."""

    status_changed = Signal(str, bool)  # text, is_error
    progress_changed = Signal(int)  # 0-100, or -1 to hide
    busy_changed = Signal(bool)
    log = Signal(str, str)  # message, level
    merged = Signal(str, object)  # stem, MergeResult
    merge_failed = Signal(str, str)  # stem, reason

    def __init__(self, parent: QObject | None = None, *, registry: MergeRegistry | None = None) -> None:
        super().__init__(parent)
        self.enabled = False
        self._registry = registry if registry is not None else MergeRegistry()
        self._queue: list[MergeRequest] = []
        self._active: tuple[QThread, MergeJob, MergeRequest] | None = None
        self._seen: set[str] = set()  # group keys queued/running/merged in this session
        self.live_stem: Callable[[], str | None] = lambda: None
        self.status_text = ""

    # ------------------------------------------------------------------ queries
    def is_busy(self) -> bool:
        return self._active is not None or bool(self._queue)

    def active_stem(self) -> str | None:
        return self._active[2].stem if self._active else None

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.log.emit(f"Merge recording parts: {'on' if enabled else 'off'}.", "info")
        if not enabled:
            dropped = [item for item in self._queue if item.auto]
            self._queue = [item for item in self._queue if not item.auto]
            for item in dropped:
                self._seen.discard(item.group_key)
            if dropped:
                self.log.emit(f"Dropped {len(dropped)} queued automatic merge(s).", "info")

    def already_merged(self, group_key: str, paths: list[Path]) -> Path | None:
        return self._registry.find(group_key, paths)

    def sweep_partials(self, folder: Path) -> list[Path]:
        """Delete half-written outputs left by an interrupted merge (restart recovery)."""
        removed: list[Path] = []
        try:
            for leftover in folder.glob(f"*_merged*.wav{PARTIAL_SUFFIX}"):
                try:
                    leftover.unlink()
                    removed.append(leftover)
                except OSError:
                    pass
        except OSError:
            pass
        for item in removed:
            self.log.emit(f"Removed unfinished merge output {item.name}.", "info")
        return removed

    # ------------------------------------------------------------------ requests
    def request(self, group_key: str, stem: str, paths: list[Path], folder: Path, *, auto: bool) -> str | None:
        """Queue a merge. Returns None when queued, otherwise the reason it was not."""
        if auto and not self.enabled:
            return "merging is off"
        live = self.live_stem()
        if auto and live and live == stem:
            self.log.emit(f"{stem}: not merged yet; the recording is still in progress.", "info")
            return "the recording is still in progress"
        if self._active is not None and self._active[2].group_key == group_key:
            return "already merging"
        if any(item.group_key == group_key for item in self._queue):
            return "already queued"
        existing = self.already_merged(group_key, paths)
        if existing is not None and auto:
            # Same parts, output still present: a refresh, a reconnect, a second
            # download or a restart must not produce a second merged file.
            self.log.emit(f"{stem}: already merged -> {existing.name}.", "info")
            return f"already merged ({existing.name})"
        if existing is None and auto and group_key in self._seen:
            # Merged earlier in this session but the output is gone (or the parts
            # changed): merge again only when the operator asks.
            self.log.emit(f"{stem}: merged earlier in this session; not merging again automatically.", "info")
            return "merged earlier in this session"
        # A manual request for already merged parts gets a fresh, uniquely named file.
        request = MergeRequest(group_key=group_key, stem=stem, paths=list(paths), folder=Path(folder), auto=auto)
        self._seen.add(group_key)
        self._queue.append(request)
        self.log.emit(
            f"{'Auto-merge' if auto else 'Merge'} queued for {stem} ({len(paths)} parts).",
            "info",
        )
        self._pump()
        return None

    def cancel(self) -> None:
        """Stop the running merge and drop everything queued behind it."""
        for item in self._queue:
            self._seen.discard(item.group_key)
        if self._queue:
            self.log.emit(f"Dropped {len(self._queue)} queued merge(s).", "info")
        self._queue.clear()
        if self._active is not None:
            _thread, job, request = self._active
            request.cancel.set()
            job.abort()
            self._set_status(f"Cancelling merge of {request.stem}…", False)

    # ------------------------------------------------------------------ internals
    def _set_status(self, text: str, error: bool) -> None:
        self.status_text = text
        self.status_changed.emit(text, error)

    def _pump(self) -> None:
        if self._active is not None or not self._queue:
            return
        request = self._queue.pop(0)
        try:
            plan = plan_merge(request.paths)
        except Exception as exc:  # noqa: BLE001
            self._finish_failed(request, f"{type(exc).__name__}: {exc}", cancelled=False)
            return
        if not plan.ok:
            self._finish_failed(request, "; ".join(plan.errors), cancelled=False)
            return
        for warning in plan.warnings:
            self.log.emit(f"{request.stem}: {warning}", "error")
        request.plan = plan
        request.output = merged_output_path(request.stem, request.folder)
        self.log.emit(
            f"Merging {request.stem}: {plan.describe()} -> {request.output.name}",
            "info",
        )
        thread = QThread()
        job = MergeJob(request)
        job.moveToThread(thread)
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        thread.started.connect(job.run)
        thread.finished.connect(self._on_thread_finished)
        self._active = (thread, job, request)
        track_worker_thread(thread, job)
        self.busy_changed.emit(True)
        self._set_status(f"Merging {request.stem} ({len(plan.parts)} parts, {plan.duration_s:.2f} s)… 0 %", False)
        self.progress_changed.emit(0)
        thread.start()

    @Slot(object, object)
    def _on_progress(self, done: object, total: object) -> None:
        if self._active is None:
            return
        request = self._active[2]
        if request.cancel.is_set():
            return  # "Cancelling…" stays until the worker confirms
        done_b, total_b = int(done), int(total)
        pct = max(0, min(100, int(done_b * 100 / total_b))) if total_b > 0 else 0
        plan = request.plan
        parts = len(plan.parts) if plan else 0
        secs = f", {plan.duration_s:.2f} s" if plan else ""
        self.progress_changed.emit(pct)
        self._set_status(f"Merging {request.stem} ({parts} parts{secs})… {pct} %", False)

    @Slot(object)
    def _on_finished(self, result: object) -> None:
        if self._active is None or not isinstance(result, MergeResult):
            return
        request = self._active[2]
        self._registry.record(request.group_key, request.paths, result.output)
        note = ""
        if result.continuity != "verified":
            note = " Continuity across the joins is not verified (see the .merge.json file)."
        self.log.emit(
            f"Merged {request.stem}: {result.summary()} Written in {result.elapsed_s:.1f} s "
            f"next to the parts; the originals are kept.{note}",
            "info" if not note else "error",
        )
        self._set_status(f"Merged {request.stem} -> {result.output.name} ({result.duration_s:.2f} s, {result.container}).", bool(note))
        self.progress_changed.emit(-1)
        self.merged.emit(request.stem, result)

    @Slot(str, bool)
    def _on_failed(self, message: str, cancelled: bool) -> None:
        if self._active is None:
            return
        request = self._active[2]
        self._finish_failed(request, message, cancelled=cancelled)

    def _finish_failed(self, request: MergeRequest, message: str, *, cancelled: bool) -> None:
        self._seen.discard(request.group_key)
        level = "info" if cancelled else "error"
        if cancelled:
            self.log.emit(f"Merge cancelled for {request.stem}. The recording itself is not affected.", level)
            self._set_status(f"Merge cancelled: {request.stem}.", False)
        else:
            self.log.emit(f"Merge failed for {request.stem}: {message} The recording itself is not affected.", level)
            self._set_status(f"Merge failed: {request.stem}. {message}", True)
        self.progress_changed.emit(-1)
        self.merge_failed.emit(request.stem, message)
        if self._active is None:
            # Failed before a thread was started (planning stage): move on.
            self.busy_changed.emit(self.is_busy())
            self._pump()

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._active is not None:
            thread, job, _request = self._active
            self._active = None
            job.deleteLater()
            thread.deleteLater()
        self.busy_changed.emit(self.is_busy())
        self._pump()
        if not self.is_busy():
            self.busy_changed.emit(False)
