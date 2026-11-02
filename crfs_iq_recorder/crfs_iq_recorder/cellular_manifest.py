"""Versioned JSON manifests for cellular captures and the daily handover summary.

One manifest per logical capture (all split parts of one recording) sits
beside the downloaded files as ``<stem>.cellular.json``. Every field that
could not be determined says ``"unknown"`` rather than guessing. The
handover summary lists what a session completed, missed and failed with
sizes, checksums and verification state so the next shift can continue.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

MANIFEST_SCHEMA = 1
MANIFEST_SUFFIX = ".cellular.json"
HANDOVER_SCHEMA = 1
UNKNOWN = "unknown"


def _iso(value: datetime | None) -> str:
    if value is None:
        return UNKNOWN
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def _or_unknown(value: Any) -> Any:
    if value is None:
        return UNKNOWN
    if isinstance(value, str) and not value.strip():
        return UNKNOWN
    return value


def _encoding_text(recording_format: Any) -> str:
    """What the files hold, from the EMP recording_format that was actually sent."""
    fmt = str(recording_format or "").strip().upper()
    if not fmt:
        return UNKNOWN
    if fmt == "WAVE":
        return "WAVE, 16-bit PCM, I left / Q right (sensor format)"
    return f"{fmt} (sensor format; on-disk layout not published, content not inspected by this application)"


def sha256_of_file(path: Path, *, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_path(folder: Path, stem: str, *, fallback_name: str) -> Path:
    """Beside the recording when its stem is known; otherwise in a manifests folder."""
    folder = Path(folder)
    if stem:
        return folder / f"{stem}{MANIFEST_SUFFIX}"
    sub = folder / "cellular_manifests"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{fallback_name}{MANIFEST_SUFFIX}"


def build_capture_manifest(
    *,
    campaign: dict[str, Any],
    round_record: dict[str, Any],
    task: dict[str, Any],
    task_state: dict[str, Any],
    sensor: dict[str, Any],
    protocol_version: str,
    plan_window_hz: int,
    timezone_label: str,
    app_version: str = __version__,
) -> dict[str, Any]:
    """Assemble the manifest for one logical capture from the session's records."""
    summary = task_state.get("summary") or {}
    files = task_state.get("files") or []
    held = summary.get("held_s")
    requested = summary.get("requested_s") or task.get("duration_s")
    sample_rate = task.get("sample_rate_hz")
    total_bytes = sum(int(f.get("size") or 0) for f in files) or summary.get("total_bytes") or 0
    sample_count: Any = UNKNOWN
    if files and sample_rate and held is not None:
        # 16-bit I and Q: 4 bytes per complex sample; headers removed by the size model.
        sample_count = int(round(float(held) * float(sample_rate)))
    started = _parse(summary.get("started_at"))
    finished = _parse(summary.get("finished_at"))
    return {
        "schema": MANIFEST_SCHEMA,
        "kind": "cellular_capture",
        "written_at": datetime.now().astimezone().isoformat(),
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "application": {"name": "CRFS IQ Recorder", "version": app_version},
        "identifiers": {
            "campaign_id": _or_unknown(campaign.get("campaign_id")),
            "campaign": _or_unknown(campaign.get("campaign")),
            "site_id": _or_unknown(campaign.get("site_id")),
            "site": _or_unknown(campaign.get("site")),
            "visit_id": _or_unknown(campaign.get("visit_id")),
            "round_id": _or_unknown(round_record.get("round_id")),
            "protocol_version": _or_unknown(protocol_version),
            "protocol_row": task.get("row"),
            "protocol_entry_id": _or_unknown(task.get("entry_id")),
            "task_key": _or_unknown(task.get("task_key")),
            "capture_id": _or_unknown(task_state.get("record_id")),
            "recording_stem": _or_unknown(summary.get("stem")),
        },
        "schedule": {
            "scheduled_local": _or_unknown(round_record.get("scheduled_local")),
            "scheduled_utc": _or_unknown(round_record.get("scheduled_utc")),
            "round_actual_start_utc": _or_unknown(round_record.get("actual_start_utc")),
            "round_status": _or_unknown(round_record.get("status")),
            "capture_started_local": _iso(started),
            "capture_started_utc": _iso(started.astimezone(timezone.utc)) if started else UNKNOWN,
            "capture_finished_local": _iso(finished),
            "capture_finished_utc": _iso(finished.astimezone(timezone.utc)) if finished else UNKNOWN,
            "time_zone": timezone_label,
        },
        "target": {
            "band": _or_unknown(task.get("band")),
            "band_label": _or_unknown(task.get("band_label")),
            "nominal_frequency_hz": _or_unknown(task.get("nominal_hz")),
            "duplex": _or_unknown(task.get("duplex")),
            "direction": _or_unknown(task.get("direction")),
            "target_class": _or_unknown(task.get("target_class")),
            "network_from_workbook": _or_unknown(task.get("network")),
            "technology_from_workbook": _or_unknown(task.get("technology")),
            "identification_note": (
                "Network, technology and cell identity are taken from the workbook and the operator's notes; "
                "nothing is inferred from the frequency."
            ),
        },
        "allocation": {
            "requested_start_hz": task.get("requested_start_hz"),
            "requested_stop_hz": task.get("requested_stop_hz"),
            "window_start_hz": task.get("window_start_hz"),
            "window_stop_hz": task.get("window_stop_hz"),
            "window_index": task.get("window_index"),
            "window_count": task.get("window_count"),
            "overlap_with_previous_window_hz": task.get("overlap_prev_hz"),
            "coverage": (
                "partial: one sequential window of a wider allocation"
                if int(task.get("window_count") or 1) > 1
                else "whole allocation"
            ),
            "capture_window_limit_hz": plan_window_hz,
            "adjustments": list(task.get("adjustments") or []),
        },
        "recording": {
            "center_hz": task.get("center_hz"),
            "bandwidth_hz": task.get("bandwidth_hz"),
            "sample_rate_hz_model": _or_unknown(sample_rate),
            "sample_rate_note": (
                "Complex sample rate from the sensor's bandwidth model (125 MS/s / 2^k); the WAVE header of "
                "each part carries the actual value. The EMP 'rate' field is the task repeat interval, not this."
            ),
            "recording_format": _or_unknown(summary.get("recording_format")),
            "encoding": _encoding_text(summary.get("recording_format")),
            "requested_duration_s": _or_unknown(requested),
            "held_duration_s": _or_unknown(held),
            "duration_source": _or_unknown(task.get("duration_source")),
            "sample_count_estimate": sample_count,
            "total_bytes": total_bytes,
            "parts": int(summary.get("parts") or len(files) or 0),
            "sensor_finished": summary.get("sensor_finished"),
            "stream_lost": summary.get("stream_lost"),
            "outcome": _or_unknown(summary.get("outcome")),
            "short_capture": bool(task_state.get("short", False)),
            "sensor_warnings": list(summary.get("sensor_warnings") or []),
        },
        "sensor": {
            "model": _or_unknown(sensor.get("model")),
            "serial": _or_unknown(sensor.get("serial")),
            "firmware": _or_unknown(sensor.get("firmware")),
            "host_label": _or_unknown(sensor.get("host_label")),
            "clock_status": _or_unknown(sensor.get("clock_status")),
            "gnss_status": _or_unknown(sensor.get("gnss_status")),
            "antenna": _or_unknown(sensor.get("antenna")),
            "rf_port": _or_unknown(sensor.get("rf_port")),
            "gain": _or_unknown(sensor.get("gain")),
            "attenuation": _or_unknown(sensor.get("attenuation")),
            "configuration_changes": list(sensor.get("configuration_changes") or []),
        },
        "scenario": dict(campaign.get("scenario") or {}),
        "quality": dict(task_state.get("quality") or {}),
        "files": [
            {
                "name": f.get("name"),
                "remote_path": f.get("remote_path"),
                "size": f.get("size"),
                # Size the sensor listed when the capture was confirmed, when it
                # differed from the downloaded file (the sensor finishes headers
                # and sidecars a few seconds after reporting the recording done).
                "size_listed_at_confirmation": f.get("listed_size", f.get("size")),
                "local_path": _or_unknown(f.get("local_path")),
                "download_status": _or_unknown(f.get("download_status")),
                "sha256": _or_unknown(f.get("sha256")),
            }
            for f in files
        ],
        "merged_output": _or_unknown(task_state.get("merged_output")),
        "derived_images": [
            p for p in [(task_state.get("quality") or {}).get("preview_path")] if p
        ],
        "excel_status": _or_unknown(task_state.get("excel_status")),
        "notes": list(task_state.get("notes") or []),
    }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def write_manifest(path: Path, data: dict[str, Any]) -> Path:
    from .json_store import write_json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data, backup=False)
    return path


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_handover(
    *,
    campaign: dict[str, Any],
    rounds: list[dict[str, Any]],
    tasks_by_round: dict[str, list[dict[str, Any]]],
    plan_tasks: dict[str, dict[str, Any]],
    sensor: dict[str, Any],
    protocol_version: str,
    timezone_label: str,
) -> dict[str, Any]:
    """Session summary: completed / missing / failed items, counts, sizes, checksums."""
    completed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    logical_captures = 0
    file_count = 0
    total_bytes = 0
    held_total = 0.0
    warnings: list[str] = []
    not_inspected: list[str] = []
    verified_files = 0
    derived_images = 0
    merged_outputs = 0
    for round_record in rounds:
        rid = round_record.get("round_id")
        for state in tasks_by_round.get(rid, []):
            task = plan_tasks.get(state.get("task_key"), {})
            item = {
                "round_id": rid,
                "task_key": state.get("task_key"),
                "label": task.get("band_label", "") + " " + task.get("direction", ""),
                "window_hz": [task.get("window_start_hz"), task.get("window_stop_hz")],
                "status": state.get("status"),
                "record_id": state.get("record_id"),
                "stem": (state.get("summary") or {}).get("stem") or UNKNOWN,
                "manifest": state.get("manifest_path") or UNKNOWN,
                "files": [
                    {
                        "name": f.get("name"),
                        "size": f.get("size"),
                        "sha256": f.get("sha256") or UNKNOWN,
                        "download_status": f.get("download_status") or UNKNOWN,
                        "local_path": f.get("local_path") or UNKNOWN,
                    }
                    for f in state.get("files") or []
                ],
                "merged_output": state.get("merged_output") or UNKNOWN,
                "preview": (state.get("quality") or {}).get("preview_path") or UNKNOWN,
                "quality_level": (state.get("quality") or {}).get("level") or UNKNOWN,
                "held_s": (state.get("summary") or {}).get("held_s"),
                "requested_s": (state.get("summary") or {}).get("requested_s") or task.get("duration_s"),
            }
            status = state.get("status")
            if status == "confirmed":
                completed.append(item)
                logical_captures += 1
                files = state.get("files") or []
                file_count += len(files)
                total_bytes += sum(int(f.get("size") or 0) for f in files)
                verified_files += sum(1 for f in files if f.get("download_status") == "verified")
                if (state.get("quality") or {}).get("preview_path"):
                    derived_images += 1
                if state.get("merged_output"):
                    merged_outputs += 1
                held = (state.get("summary") or {}).get("held_s")
                held_total += float(held) if held is not None else float(task.get("duration_s") or 0)
                if state.get("short"):
                    warnings.append(f"{item['label']} in {rid}: short capture ({held} s of {item['requested_s']} s).")
                quality = state.get("quality") or {}
                for flag in quality.get("flags") or []:
                    if flag == "not_wave":
                        continue
                    warnings.append(f"{item['label']} in {rid}: {flag}")
                if "not_wave" in (quality.get("flags") or []) or quality.get("level") == "not_checked":
                    formats = sorted({str(f.get("container") or "") for f in quality.get("files") or [] if f.get("container")})
                    label = ", ".join(formats) or _or_unknown(campaign.get("recording_format"))
                    not_inspected.append(
                        f"{item['label']} in {rid}: {label} — sizes and SHA-256 verified by the download; "
                        "sample content, duration and previews are checked for WAVE recordings only."
                    )
            elif status in {"unknown", "uncertain", "submitting", "recording", "sensor_finished"}:
                uncertain.append(item)
            elif status in {"failed", "not_sent", "sensor_error"}:
                failed.append(item)
            else:
                missing.append(item)
    missed_rounds = [r for r in rounds if r.get("status") == "missed"]
    return {
        "schema": HANDOVER_SCHEMA,
        "kind": "cellular_handover",
        "written_at": datetime.now().astimezone().isoformat(),
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "application": {"name": "CRFS IQ Recorder", "version": __version__},
        "campaign": {
            "campaign_id": _or_unknown(campaign.get("campaign_id")),
            "campaign": _or_unknown(campaign.get("campaign")),
            "site_id": _or_unknown(campaign.get("site_id")),
            "site": _or_unknown(campaign.get("site")),
            "visit_id": _or_unknown(campaign.get("visit_id")),
            "protocol_version": _or_unknown(protocol_version),
            "time_zone": timezone_label,
        },
        "sensor": {k: _or_unknown(sensor.get(k)) for k in ("model", "serial", "firmware", "host_label")},
        "rounds": rounds,
        "counts": {
            "rounds_total": len(rounds),
            "rounds_finished": sum(1 for r in rounds if r.get("status") in {"done", "partial", "failed", "stopped"}),
            "rounds_missed": len(missed_rounds),
            "logical_captures_confirmed": logical_captures,
            "files": file_count,
            "files_verified_locally": verified_files,
            "derived_images": derived_images,
            "merged_outputs": merged_outputs,
            "total_bytes": total_bytes,
            "held_seconds_total": round(held_total, 3),
            "uncertain": len(uncertain),
            "failed": len(failed),
            "not_recorded": len(missing),
            "content_not_inspected": len(not_inspected),
        },
        "completed": completed,
        "uncertain": uncertain,
        "failed": failed,
        "not_recorded": missing,
        "quality_warnings": warnings,
        "content_not_inspected": not_inspected,
        "transfer_note": (
            "Original recordings stay on the sensor until their local copies are verified (size and SHA-256); "
            "nothing is uploaded or deleted automatically."
        ),
    }


def handover_markdown(data: dict[str, Any]) -> str:
    """A short human-readable version of the handover summary."""
    counts = data.get("counts", {})
    camp = data.get("campaign", {})
    lines = [
        f"# Cellular collection handover — {camp.get('campaign', UNKNOWN)} / {camp.get('site', UNKNOWN)}",
        "",
        f"Written {data.get('written_at')} ({camp.get('time_zone', UNKNOWN)}); protocol {camp.get('protocol_version', UNKNOWN)}.",
        "",
        f"- Rounds: {counts.get('rounds_finished', 0)} finished of {counts.get('rounds_total', 0)}, "
        f"{counts.get('rounds_missed', 0)} missed",
        f"- Logical captures confirmed: {counts.get('logical_captures_confirmed', 0)} "
        f"({counts.get('files', 0)} files, {counts.get('files_verified_locally', 0)} verified locally, "
        f"{counts.get('total_bytes', 0):,} bytes, {counts.get('held_seconds_total', 0)} s of IQ held)",
        f"- Derived images (previews): {counts.get('derived_images', 0)}  ·  Merged outputs: {counts.get('merged_outputs', 0)} "
        "(counted separately from the original recordings)",
        f"- Uncertain: {counts.get('uncertain', 0)}  ·  Failed: {counts.get('failed', 0)}  ·  Not recorded: {counts.get('not_recorded', 0)}",
        "",
    ]
    for title, key in (("Completed", "completed"), ("Uncertain", "uncertain"), ("Failed", "failed"), ("Not recorded", "not_recorded")):
        items = data.get(key) or []
        lines.append(f"## {title} ({len(items)})")
        for item in items:
            files = ", ".join(f"{f.get('name')} ({f.get('download_status')})" for f in item.get("files") or []) or "no files"
            lines.append(f"- {item.get('round_id')}: {item.get('label')} — {item.get('status')} — {files}")
        lines.append("")
    warnings = data.get("quality_warnings") or []
    lines.append(f"## Quality warnings ({len(warnings)})")
    lines.extend(f"- {w}" for w in warnings)
    lines.append("")
    not_inspected = data.get("content_not_inspected") or []
    if not_inspected:
        lines.append(f"## Content not inspected ({len(not_inspected)})")
        lines.extend(f"- {n}" for n in not_inspected)
        lines.append("")
    lines.append(data.get("transfer_note", ""))
    return "\n".join(lines) + "\n"
