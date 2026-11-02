"""Reference material for the protocol *template* only.

These numbers come from the allocation chart attached to the planning ticket
(29 Oct 2026). They are chart data, not verified licensing records, and the
importer and planner never read them: a collection runs only on the
allocations written in the workbook the operator loads. The template marks
every row "verify before use" so nobody mistakes it for the protocol.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_HEADERS = (
    "Band",
    "Nominal (MHz)",
    "Duplex",
    "Direction",
    "Network",
    "Technology",
    "Start (MHz)",
    "Stop (MHz)",
    "Duration (s)",
    "Notes",
    # Filled by the collection in the per-round results sheets; leave empty.
    "IQ Recording",
    "Recording Date & Time",
)

VERIFY_NOTE = "Reference chart 29 Oct 2026 - verify before use"

# (band, nominal MHz, duplex, direction, network, technology, start MHz, stop MHz)
REFERENCE_ALLOCATIONS: tuple[tuple[str, int, str, str, str, str, float, float], ...] = (
    ("700", 700, "FDD", "UL", "Pelephone", "", 703, 713),
    ("700", 700, "FDD", "UL", "Cellcom", "", 713, 723),
    ("700", 700, "FDD", "UL", "PHI Partner", "", 723, 728),
    ("700", 700, "FDD", "UL", "PHI HOT", "", 728, 733),
    ("700", 700, "FDD", "DL", "Pelephone", "", 758, 768),
    ("700", 700, "FDD", "DL", "Cellcom", "", 768, 778),
    ("700", 700, "FDD", "DL", "PHI Partner", "", 778, 783),
    ("700", 700, "FDD", "DL", "PHI HOT", "", 783, 788),
    ("800", 800, "FDD", "UL", "Pelephone", "", 832, 842),
    ("800", 800, "FDD", "UL", "Cellcom", "", 842, 852),
    ("800", 800, "FDD", "DL", "Pelephone", "", 791, 801),
    ("800", 800, "FDD", "DL", "Cellcom", "", 801, 811),
    ("850", 850, "FDD", "UL", "Pelephone", "", 825, 830),
    ("850", 850, "FDD", "UL", "Cellcom", "", 830, 835),
    ("850", 850, "FDD", "DL", "Pelephone", "", 870, 875),
    ("850", 850, "FDD", "DL", "Cellcom", "", 875, 880),
    ("900", 900, "FDD", "UL", "PHI Partner", "", 902.2, 910.2),
    ("900", 900, "FDD", "UL", "shared", "", 910.2, 912.6),
    ("900", 900, "FDD", "DL", "PHI Partner", "", 947.2, 955.2),
    ("900", 900, "FDD", "DL", "shared", "", 955.2, 957.6),
    ("1800", 1800, "FDD", "UL", "Cellcom", "", 1710, 1730),
    ("1800", 1800, "FDD", "UL", "PHI Partner", "", 1740, 1755),
    ("1800", 1800, "FDD", "UL", "PHI HOT", "", 1755, 1760),
    ("1800", 1800, "FDD", "UL", "Pelephone", "", 1760, 1780),
    ("1800", 1800, "FDD", "DL", "Cellcom", "", 1805, 1825),
    ("1800", 1800, "FDD", "DL", "PHI Partner", "", 1835, 1850),
    ("1800", 1800, "FDD", "DL", "PHI HOT", "", 1850, 1855),
    ("1800", 1800, "FDD", "DL", "Pelephone", "", 1855, 1875),
    ("2100", 2100, "FDD", "UL", "PHI Partner", "", 1940, 1950),
    ("2100", 2100, "FDD", "UL", "PHI HOT", "", 1950, 1960),
    ("2100", 2100, "FDD", "UL", "Cellcom", "", 1960, 1970),
    ("2100", 2100, "FDD", "UL", "Pelephone", "", 1970, 1980),
    ("2100", 2100, "FDD", "DL", "PHI Partner", "", 2130, 2140),
    ("2100", 2100, "FDD", "DL", "PHI HOT", "", 2140, 2150),
    ("2100", 2100, "FDD", "DL", "Cellcom", "", 2150, 2160),
    ("2100", 2100, "FDD", "DL", "Pelephone", "", 2160, 2170),
    ("2600", 2600, "FDD", "UL", "Cellcom", "", 2500, 2520),
    ("2600", 2600, "FDD", "UL", "PHI Partner", "", 2520, 2530),
    ("2600", 2600, "FDD", "UL", "PHI HOT", "", 2530, 2540),
    ("2600", 2600, "FDD", "UL", "Pelephone", "", 2540, 2560),
    ("2600", 2600, "FDD", "DL", "Cellcom", "", 2620, 2640),
    ("2600", 2600, "FDD", "DL", "PHI Partner", "", 2640, 2650),
    ("2600", 2600, "FDD", "DL", "PHI HOT", "", 2650, 2660),
    ("2600", 2600, "FDD", "DL", "Pelephone", "", 2660, 2680),
    ("3500", 3500, "TDD", "TDD", "PHI Partner", "5G NR", 3500, 3550),
    ("3500", 3500, "TDD", "TDD", "PHI HOT", "5G NR", 3550, 3600),
    ("3500", 3500, "TDD", "TDD", "Cellcom", "5G NR", 3600, 3700),
    ("3500", 3500, "TDD", "TDD", "Pelephone", "5G NR", 3700, 3800),
)


def write_template_workbook(path: str | Path, *, duration_s: float | None = None, include_reference: bool = True) -> Path:
    """Write a protocol workbook the importer understands.

    With ``include_reference`` the rows carry the reference chart values and a
    "verify before use" note in every row; otherwise only the header row and
    one example row are written.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Cellular Protocol"
    ws.append(list(TEMPLATE_HEADERS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    rows = REFERENCE_ALLOCATIONS if include_reference else REFERENCE_ALLOCATIONS[:1]
    note = VERIFY_NOTE if include_reference else "Example row - replace with your allocation"
    for band, nominal, duplex, direction, network, technology, start, stop in rows:
        ws.append([band, nominal, duplex, direction, network, technology, start, stop, duration_s, note, None, None])
    widths = {"A": 8, "B": 14, "C": 8, "D": 10, "E": 14, "F": 12, "G": 12, "H": 12, "I": 12, "J": 48, "K": 40, "L": 22}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A2"
    readme = wb.create_sheet("Read me")
    for line in (
        "How this workbook is read by CRFS IQ Recorder - Cellular Collection",
        "",
        "One row = one allocation to record. Start/Stop are the exact edges of the allocation; the band",
        "name or nominal frequency is only a label and is never used as a centre frequency.",
        "Duplex: FDD or TDD. Direction: DL or UL for FDD rows; TDD rows are recorded once as shared UL/DL.",
        "Units: MHz in the headers shown here; a unit written in the cell (e.g. '3.5 GHz') wins.",
        "Duration (s): optional; when empty the campaign duration in the application is used.",
        "Network / Technology: labels copied into the manifests. Nothing is inferred from the frequency.",
        "IQ Recording / Recording Date & Time: leave empty. The collection fills them in a separate",
        "'Round <date> <time>' sheet per round (file names, seconds held, local date and time); this sheet is never changed.",
        "",
        "Reference rows come from the allocation chart of 29 Oct 2026 and must be verified before use.",
        "Rows whose Status is 'Collected' (if you add such a column) are treated as results, not instructions.",
    ):
        readme.append([line])
    readme.column_dimensions["A"].width = 110
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
