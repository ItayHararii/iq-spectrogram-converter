"""Map Node model text to the collection workbook's Sensor names."""

from __future__ import annotations

import re

_MISSING = {"", "-", "—", "–"}


def _compact(value: str) -> str:
    """Letters and digits only, lower case: 'CRFS-R408' -> 'crfsr408', '40-8' -> '408'."""
    return re.sub(r"[^0-9a-z]", "", str(value).casefold())


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
    named = [str(item).strip() for item in existing if str(item).strip()]
    # The workbook may already name this very sensor in its own style, for
    # example "CRFS-R408" for an R40-8. Reuse that spelling instead of
    # inventing a second one ("40-8") for the same device.
    compact = _compact(text)
    for item in named:
        if compact and _compact(item).endswith(compact):
            return item
    prefix_crfs = True
    if named and not all(item.casefold().startswith("crfs ") for item in named):
        prefix_crfs = named[0].casefold().startswith("crfs ")
    if prefix_crfs:
        return f"CRFS {text}"
    return text
