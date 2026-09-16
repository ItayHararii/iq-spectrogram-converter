"""Suggest a collection class from the recording frequency window."""

from __future__ import annotations

from dataclasses import dataclass

from .collection_catalog import ClassMapping


@dataclass(frozen=True)
class ClassMatch:
    mapping: ClassMapping | None
    candidates: tuple[ClassMapping, ...]
    reason: str
    unique: bool
    needs_choice: bool


def _exact(record_start: int, record_end: int, item: ClassMapping) -> bool:
    return item.start_hz == record_start and item.end_hz == record_end


def _contains(record_start: int, record_end: int, item: ClassMapping) -> bool:
    return item.start_hz <= record_start and record_end <= item.end_hz


def _width(item: ClassMapping) -> int:
    return item.end_hz - item.start_hz


def _match_range(
    record_start: int,
    record_end: int,
    items: tuple[ClassMapping, ...],
) -> ClassMatch:
    if record_start >= record_end or not items:
        return ClassMatch(None, items, "Select a target class and worksheet.", False, True)
    exact = [item for item in items if _exact(record_start, record_end, item)]
    if len(exact) == 1:
        return ClassMatch(exact[0], tuple(exact), "exact", True, False)
    if len(exact) > 1:
        return ClassMatch(None, tuple(exact), "Select the target class and worksheet.", False, True)
    containing = [item for item in items if _contains(record_start, record_end, item)]
    if not containing:
        return ClassMatch(None, items, "Select a target class and worksheet.", False, True)
    narrowest = min(_width(item) for item in containing)
    tight = [item for item in containing if _width(item) == narrowest]
    if len(tight) == 1:
        return ClassMatch(tight[0], tuple(tight), "narrowest", True, False)
    return ClassMatch(None, tuple(tight), "Select the target class and worksheet.", False, True)


def mapping_for_selection(
    mappings: tuple[ClassMapping, ...] | list[ClassMapping],
    *,
    target_class: str,
    worksheet: str = "",
    record_start: int | None = None,
    record_end: int | None = None,
) -> ClassMatch:
    class_name = (target_class or "").strip()
    sheet = (worksheet or "").strip()
    items = tuple(mappings)
    if not class_name:
        return ClassMatch(None, items, "Select a target class.", False, True)
    hits = [item for item in items if item.target_class == class_name]
    if sheet:
        hits = [item for item in hits if item.worksheet == sheet]
    if not hits:
        return ClassMatch(None, items, "Select a target class and worksheet.", False, True)
    if len(hits) == 1:
        return ClassMatch(hits[0], tuple(hits), "user", True, False)
    if record_start is not None and record_end is not None:
        refined = _match_range(record_start, record_end, tuple(hits))
        if refined.unique:
            return ClassMatch(refined.mapping, refined.candidates, "user", True, False)
    return ClassMatch(None, tuple(hits), "Select the worksheet for this class.", False, True)


def suggest_class(
    record_start: int,
    record_end: int,
    mappings: tuple[ClassMapping, ...] | list[ClassMapping],
    *,
    selected_class: str = "",
    selected_sheet: str = "",
) -> ClassMatch:
    """Match the full recording range.

    Explicit class/worksheet first, then an exact range, then the unique
    narrowest mapped range that fully contains the recording.
    """
    items = tuple(mappings)
    if (selected_class or "").strip():
        return mapping_for_selection(
            items,
            target_class=selected_class,
            worksheet=selected_sheet,
            record_start=record_start,
            record_end=record_end,
        )
    return _match_range(record_start, record_end, items)
