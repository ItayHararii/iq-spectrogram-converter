"""Map Node model text to the collection workbook's Sensor names."""

from __future__ import annotations

import re

_MISSING = {"", "-", "—", "–"}


def workbook_sensor_name(model: str, existing: list[str] | tuple[str, ...] = ()) -> str:
    """Turn `R100-18` into `CRFS 100-18` when that is how the workbook names sensors."""
    raw = " ".join(str(model or "").split()).strip()
    if raw in _MISSING:
        for item in existing:
            if str(item).strip():
                return str(item).strip()
        return ""
    text = re.sub(r"(?i)^(rfeye\s+)?node\s+", "", raw).strip()
    text = re.sub(r"^R(?=\d)", "", text).strip()
    text = re.sub(r"(?i)^crfs\s+", "", text).strip()
    if not text:
        return ""
    prefix_crfs = True
    named = [str(item).strip() for item in existing if str(item).strip()]
    if named and not all(item.casefold().startswith("crfs ") for item in named):
        prefix_crfs = named[0].casefold().startswith("crfs ")
    if prefix_crfs:
        return f"CRFS {text}"
    return text
