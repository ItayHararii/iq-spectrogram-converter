"""Recording lifecycle labels.

EMP POST acceptance is not file completion. Confirmed means SFTP listed
matching WAVE files whose sizes stayed the same between two refreshes.
"""

from __future__ import annotations

IDLE = "idle"
SUBMITTING = "submitting"
RECORDING = "recording"
FINALIZING = "finalizing"
CONFIRMED = "confirmed"
UNKNOWN = "unknown"
WAITING = "waiting"

PHASE_LABELS = {
    IDLE: "Idle",
    SUBMITTING: "Sending request…",
    RECORDING: "Recording…",
    FINALIZING: "Time elapsed - waiting for files",
    CONFIRMED: "WAVE file found",
    UNKNOWN: "Time elapsed - file not confirmed",
    WAITING: "Next recording",
}

NO_EMP_COMPLETION_API = (
    "No EMP completion event. HTTP accept is not file-done. "
    "Confirmed = matching WAVE files with stable SFTP size."
)
