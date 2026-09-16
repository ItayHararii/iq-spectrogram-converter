"""Sort SFTP listings by recency or by table-column values."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from dataclasses import dataclass

from .recording_history import (
    cluster_key,
    folder_date_from_path,
    iq_group_display_name,
    iq_part_number,
    parse_iq_timestamp,
    recording_group_key,
    remote_directory,
)
from .sftp_client import RemoteEntry

COL_NEW = 0
COL_NAME = 1
COL_SIZE = 2
COL_MODIFIED = 3
COL_START = 4
COL_END = 5
COL_CENTER = 6
COL_BANDWIDTH = 7
COL_DURATION = 8

SORTABLE_COLUMNS = frozenset(
    {
        COL_NAME,
        COL_SIZE,
        COL_MODIFIED,
        COL_START,
        COL_END,
        COL_CENTER,
        COL_BANDWIDTH,
        COL_DURATION,
    }
)

_DATE_FOLDER = re.compile(r"^\d{8}$")

ARROW_DESC = " ▼"
ARROW_ASC = " ▲"


@dataclass(frozen=True)
class ListingNode:
    kind: str
    entries: tuple[RemoteEntry, ...]
    group_key: str = ""
    name: str = ""

    @property
    def lead(self) -> RemoteEntry:
        return self.entries[0]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)


def _sorted_parts(entries: list[RemoteEntry]) -> tuple[RemoteEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (iq_part_number(entry.name) is None, iq_part_number(entry.name) or 0, entry.name.casefold()),
        )
    )


def build_listing_nodes(
    entries: list[RemoteEntry],
    matched: dict[str, Any] | None = None,
) -> list[ListingNode]:
    records = matched or {}
    clusters: dict[tuple[str, str, str], list[RemoteEntry]] = {}
    for entry in entries:
        if entry.is_dir or iq_part_number(entry.name) is None:
            continue
        clusters.setdefault(cluster_key(entry, records), []).append(entry)
    split_keys = {key for key, members in clusters.items() if len(members) >= 2}
    used: set[tuple[str, str, str]] = set()
    nodes: list[ListingNode] = []
    for entry in entries:
        if entry.is_dir:
            nodes.append(ListingNode("dir", (entry,), name=entry.name))
            continue
        key = cluster_key(entry, records)
        if key in split_keys:
            if key in used:
                continue
            used.add(key)
            members = _sorted_parts(clusters[key])
            rec = records.get(members[0].path)
            nodes.append(
                ListingNode(
                    "group",
                    members,
                    group_key=recording_group_key(
                        sensor_id=str(getattr(rec, "sensor_id", "") or "") if rec is not None else "",
                        directory=remote_directory(members[0].path),
                        stem=key[1],
                        record_id=str(getattr(rec, "id", "") or "") if rec is not None else "",
                    ),
                    name=iq_group_display_name(members[0].name),
                )
            )
            continue
        nodes.append(ListingNode("file", (entry,), name=entry.name))
    return nodes


def sort_listing_nodes(
    nodes: list[ListingNode],
    *,
    matched: dict[str, Any] | None = None,
    column: int | None = None,
    descending: bool = True,
) -> list[ListingNode]:
    records = matched or {}

    def rec_of(node: ListingNode) -> Any | None:
        for entry in node.entries:
            rec = records.get(entry.path)
            if rec is not None:
                return rec
        return None

    def newest(node: ListingNode) -> datetime | None:
        times = [entry_newest_time(entry, records.get(entry.path)) for entry in node.entries]
        present = [item for item in times if item is not None]
        return max(present) if present else None

    if column is None or column not in SORTABLE_COLUMNS:
        return _sort_nodes(nodes, newest, descending=True)

    if column == COL_NAME:
        return _sort_nodes(nodes, lambda node: node.name or node.lead.name, descending=descending, text=True)
    if column == COL_SIZE:
        return _sort_nodes(
            nodes,
            lambda node: None if node.kind == "dir" else sum(int(entry.size or 0) for entry in node.entries),
            descending=descending,
        )
    if column == COL_MODIFIED:
        return _sort_nodes(
            nodes,
            lambda node: max((entry.modified for entry in node.entries if entry.modified), default=None),
            descending=descending,
        )
    if column == COL_START:
        return _sort_nodes(nodes, lambda node: _hz(rec_of(node), "start_hz"), descending=descending)
    if column == COL_END:
        return _sort_nodes(nodes, lambda node: _hz(rec_of(node), "end_hz"), descending=descending)
    if column == COL_CENTER:
        return _sort_nodes(nodes, lambda node: _hz(rec_of(node), "center_hz"), descending=descending)
    if column == COL_BANDWIDTH:
        return _sort_nodes(nodes, lambda node: _hz(rec_of(node), "bandwidth_hz"), descending=descending)
    return _sort_nodes(nodes, lambda node: _duration(rec_of(node)), descending=descending)


def _sort_nodes(
    nodes: list[ListingNode],
    value_fn,
    *,
    descending: bool,
    text: bool = False,
) -> list[ListingNode]:
    present: list[tuple[Any, ListingNode]] = []
    missing: list[ListingNode] = []
    for node in nodes:
        value = value_fn(node)
        if value is None or (text and value == ""):
            missing.append(node)
        else:
            present.append((value, node))

    def present_key(item: tuple[Any, ListingNode]) -> tuple:
        value, node = item
        name = (node.name or node.lead.name).casefold()
        if text:
            return (str(value).casefold(), name, node.lead.path)
        if isinstance(value, datetime):
            return (value, name, node.lead.path)
        return (value, name, node.lead.path)

    present.sort(key=present_key, reverse=descending)
    missing.sort(key=lambda node: ((node.name or node.lead.name).casefold(), node.lead.path))
    return [node for _value, node in present] + missing


def parse_date_folder_name(name: str) -> datetime | None:
    """Parse a daily folder named YYYYMMDD into that calendar date at midnight."""
    text = (name or "").strip().rstrip("/")
    if not _DATE_FOLDER.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None


def entry_newest_time(entry: RemoteEntry, rec: Any | None = None) -> datetime | None:
    """Newest-first timestamp: folders by encoded date, files by recording then mtime."""
    if entry.is_dir:
        return parse_date_folder_name(entry.name) or entry.modified
    started = getattr(rec, "started_at", None) if rec is not None else None
    if started is not None:
        return started.replace(tzinfo=None) if started.tzinfo is not None else started
    parsed = parse_iq_timestamp(entry.name, folder_date=folder_date_from_path(entry.path))
    if parsed is not None:
        return parsed
    return entry.modified


def sort_entries_newest_first(
    entries: list[RemoteEntry],
    *,
    matched: dict[str, Any] | None = None,
) -> list[RemoteEntry]:
    """Single list, newest at the top. Missing timestamps stay at the bottom."""
    records = matched or {}
    return _sort_with_missing(
        entries,
        lambda entry: entry_newest_time(entry, records.get(entry.path)),
        descending=True,
    )


def sort_entries(
    entries: list[RemoteEntry],
    *,
    matched: dict[str, Any] | None = None,
    column: int | None = None,
    descending: bool = True,
) -> list[RemoteEntry]:
    """Sort a listing. column=None is newest-first. Missing values stay last."""
    if column is None or column not in SORTABLE_COLUMNS:
        return sort_entries_newest_first(entries, matched=matched)
    records = matched or {}

    def rec_of(entry: RemoteEntry) -> Any | None:
        return records.get(entry.path)

    if column == COL_NAME:
        return _sort_with_missing(
            entries,
            lambda entry: entry.name or "",
            descending=descending,
            text=True,
        )
    if column == COL_SIZE:
        return _sort_with_missing(
            entries,
            lambda entry: None if entry.is_dir else int(entry.size or 0),
            descending=descending,
        )
    if column == COL_MODIFIED:
        return _sort_with_missing(
            entries,
            lambda entry: entry.modified,
            descending=descending,
        )
    if column == COL_START:
        return _sort_with_missing(
            entries,
            lambda entry: _hz(rec_of(entry), "start_hz"),
            descending=descending,
        )
    if column == COL_END:
        return _sort_with_missing(
            entries,
            lambda entry: _hz(rec_of(entry), "end_hz"),
            descending=descending,
        )
    if column == COL_CENTER:
        return _sort_with_missing(
            entries,
            lambda entry: _hz(rec_of(entry), "center_hz"),
            descending=descending,
        )
    if column == COL_BANDWIDTH:
        return _sort_with_missing(
            entries,
            lambda entry: _hz(rec_of(entry), "bandwidth_hz"),
            descending=descending,
        )
    return _sort_with_missing(
        entries,
        lambda entry: _duration(rec_of(entry)),
        descending=descending,
    )


def header_label(base: str, *, active: bool, descending: bool) -> str:
    text = (base or "").replace(ARROW_DESC, "").replace(ARROW_ASC, "").rstrip()
    if not active:
        return text
    return text + (ARROW_DESC if descending else ARROW_ASC)


def _hz(rec: Any | None, field: str) -> int | None:
    if rec is None:
        return None
    value = getattr(rec, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration(rec: Any | None) -> float | None:
    if rec is None:
        return None
    value = getattr(rec, "duration_s", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_with_missing(
    entries: list[RemoteEntry],
    value_fn: Callable[[RemoteEntry], Any],
    *,
    descending: bool,
    text: bool = False,
) -> list[RemoteEntry]:
    present: list[tuple[Any, RemoteEntry]] = []
    missing: list[RemoteEntry] = []
    for entry in entries:
        value = value_fn(entry)
        if value is None or (text and value == ""):
            missing.append(entry)
        else:
            present.append((value, entry))

    def present_key(item: tuple[Any, RemoteEntry]) -> tuple:
        value, entry = item
        if text:
            return (str(value).casefold(), entry.name.casefold(), entry.path)
        if isinstance(value, datetime):
            return (value, entry.name.casefold(), entry.path)
        return (value, entry.name.casefold(), entry.path)

    present.sort(key=present_key, reverse=descending)
    missing.sort(key=lambda entry: (entry.name.casefold(), entry.path))
    return [entry for _value, entry in present] + missing
