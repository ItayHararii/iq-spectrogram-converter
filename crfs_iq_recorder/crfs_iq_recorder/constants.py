"""Defaults and verified/unverified format catalog. Do not invent EMP identifiers."""

from __future__ import annotations

from dataclasses import dataclass

APP_NAME = "CRFS IQ Recorder"
APP_ID = "Sensorz.CRFSIQRecorder"

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "pass"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PORT = 80
EMP_PATH = "/emp/"
CONNECTION_TEST_PATH = "/"
NODE_JSON_PATH = "/api/node.json"
VERSIONS_JSON_PATH = "/api/values/versions.json"
SOFTWARE_MANAGER_API_PATH = "/software-manager/api"

DEFAULT_TASK_ID = "iq-testing"
DEFAULT_DURATION = 0.1
DEFAULT_RATE = 0.1
DEFAULT_CAPTURE_LENGTH = 0.1
DEFAULT_RECORDING_FORMAT = "WAVE"

# Integer Hz, matching the supplied EMP example.
DEFAULT_CENTER_HZ = 800_000_000
DEFAULT_BANDWIDTH_HZ = 12_500_000
DEFAULT_START_HZ = 793_750_000
DEFAULT_END_HZ = 806_250_000

DEFAULT_RECORDED_TIME_S = 0.1
DEFAULT_REPEAT_WAIT_S = 5.0
DEFAULT_REPEAT_WAIT_UNIT = "seconds"
DEFAULT_SFTP_PORT = 22
# SFTP uses a different account from HTTP Basic Auth.
DEFAULT_SFTP_USERNAME = "root"
DEFAULT_SFTP_PASSWORD = "rf3y3"
SFTP_REMDATA_ROOT = "/mnt/1/remdata/"

EMPIRICAL_REF_SIZE_MB = "61.051"
EMPIRICAL_REF_BW_HZ = 10_000_000
EMPIRICAL_REF_TIME_S = 1.0

# Node datasheets: 16-bit I and 16-bit Q. Not proven for every recording_format.
DATASHEET_BYTES_PER_COMPLEX_SAMPLE = 4
DATASHEET_CHANNELS = 1
DATASHEET_FULLRATE_COMPLEX_SPS = 125_000_000

FREQ_UNITS = ("kHz", "MHz", "GHz")
DEFAULT_FREQ_UNIT = "MHz"

SETTINGS_FILENAME = ".crfs_iq_recorder_settings.json"
RECORDINGS_FILENAME = ".crfs_iq_recorder_recordings.json"
PRESET_FILTER = "Recording presets (*.json);;All files (*.*)"
EXCEL_LOG_FILENAME = ".crfs_iq_recorder_excel_log.json"
DOWNLOAD_APP_FOLDER = "CRFS IQ Recorder"
DOWNLOAD_SUBFOLDER = "Recordings"


@dataclass(frozen=True)
class RecordingFormatInfo:
    identifier: str
    emp_verified: bool
    short_label: str
    tooltip: str
    enabled: bool = True


# WAVE / XDAT / NCP / HDF5: Sensorz RF Streamer HLD EMP enum.
# BIN / JSON: requested by the user; not in that enum or public EMP docs.
RECORDING_FORMATS: tuple[RecordingFormatInfo, ...] = (
    RecordingFormatInfo(
        identifier="WAVE",
        emp_verified=True,
        short_label="WAVE",
        tooltip=(
            "Verified EMP recording_format identifier. "
            "RIFF/WAVE IQ is typically 16-bit stereo PCM (I/Q). "
            "Standard WAV metadata is limited (sample rate, sample count); "
            "WAV-E is a different Site/DeepView export and is not sent."
        ),
    ),
    RecordingFormatInfo(
        identifier="XDAT",
        emp_verified=True,
        short_label="XDAT",
        tooltip=(
            "Verified EMP recording_format identifier (internal Sensorz EMP mapping). "
            "Also used as an RFeye Site snippet / DeepView export format. "
            "Sample layout for EMP XDAT is not published in public CRFS docs."
        ),
    ),
    RecordingFormatInfo(
        identifier="NCP",
        emp_verified=True,
        short_label="NCP",
        tooltip=(
            "Verified EMP recording_format identifier (internal Sensorz EMP mapping). "
            "NCP is CRFS Node Control Protocol; DeepView lists NCP among exports. "
            "On-disk layout for EMP NCP recordings is not published publicly."
        ),
    ),
    RecordingFormatInfo(
        identifier="HDF5",
        emp_verified=True,
        short_label="HDF5",
        tooltip=(
            "Verified EMP recording_format identifier (internal Sensorz EMP mapping). "
            "Also an RFeye Site snippet / DeepView export format."
        ),
    ),
    RecordingFormatInfo(
        identifier="BIN",
        emp_verified=False,
        short_label="BIN (unverified)",
        tooltip=(
            "Requested identifier - not present in the documented EMP enum "
            "(WAVE, XDAT, NCP, HDF5) or public CRFS format lists. "
            "The sensor may reject this value. This is not a file-extension conversion."
        ),
    ),
    RecordingFormatInfo(
        identifier="JSON",
        emp_verified=False,
        short_label="JSON (unverified IQ format)",
        tooltip=(
            "Requested identifier - not a verified EMP IQ sample format. "
            "CRFS uses JSON for REST APIs and configuration files. "
            "Preset export and request preview JSON are separate from this field. "
            "Do not treat a .json extension as format conversion."
        ),
    ),
)

FORMAT_BY_ID = {item.identifier: item for item in RECORDING_FORMATS}
VERIFIED_RECORDING_FORMATS: tuple[RecordingFormatInfo, ...] = tuple(
    item for item in RECORDING_FORMATS if item.emp_verified
)

UNVERIFIED_TIMING_TOOLTIPS = {
    "duration": (
        "EMP field name: duration. Unit and meaning are not defined in public CRFS "
        "EMP documentation. Internal Sensorz mapping includes it as a float on IQ "
        "jobs; the SWORD UI treated 'recording length' as seconds. "
        "Not assumed to be the only captured-time field. Not used for capture count."
    ),
    "rate": (
        "EMP field name: rate. Unconfirmed. Not assumed to be the IQ sample rate "
        "(Node datasheets quote 125 MS/s as full-rate ADC I&Q). Sweep jobs type "
        "rate as int; IQ jobs type it as float. Not used to compute capture count."
    ),
    "capture_length": (
        "EMP field name: capture_length. Unconfirmed unit and relationship to "
        "duration and rate. Sent exactly as entered. Not used as sample count."
    ),
}

MODEL_INFO_TEXT = (
    "Published CRFS receiver ranges differ by model and are shown for reference only. "
    "This application does not clamp or rewrite your request to fit a model.\n\n"
    "Node 40-8: 9 kHz-8 GHz, 40 MHz IBW, ~20 MHz sustained local I/Q.\n"
    "Node 100-8: 9 kHz-8 GHz, 100 MHz IBW, ~25 MHz sustained local I/Q.\n"
    "Node 100-18 / LW: 9 kHz-18 GHz, 100 MHz IBW, ~25 MHz sustained local I/Q.\n"
    "Node Plus 100-18: 9 kHz-18 GHz, 100 MHz IBW, 100 MHz sustained local I/Q.\n"
    "Node 100-40: 9 kHz-40 GHz, 100 MHz IBW, ~25 MHz sustained local I/Q.\n\n"
    "Node 100-x can record 100 MHz I/Q for a few seconds (not sustained gapless). "
    "Tuning resolution on the Node 100-18 datasheet is 1 Hz. "
    "Firmware and options vary. Confirm on the specific sensor."
)
