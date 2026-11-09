"""openpyxl fidelity for workbooks that hold pictures.

openpyxl keeps a loaded picture's ``<a:hlinkClick r:id=...>`` but not the
relationship that id points to, so a plain load + save leaves a dangling link
Excel repairs by deleting. ``load_workbook_keeping_links`` reads the targets
first and the installed drawing writer re-creates the relationships (and gives
new pictures their name, alt text, ScreenTip and link).

``closed_write_risks`` lists what an openpyxl save would silently drop; a
workbook that has any of it is not rewritten while closed.
"""

from __future__ import annotations

import posixpath
import re
import threading
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_DOC = _R_NS + "/officeDocument"
_HYPERLINK = _R_NS + "/hyperlink"
_DRAWING = _R_NS + "/drawing"

LINK_ATTRS = ("hlinkClick", "hlinkHover")

_install_lock = threading.Lock()
_installed = False


def _rels_part(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", f"{name}.rels")


def _resolve(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _read_rels(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str, str | None]]:
    try:
        data = archive.read(_rels_part(part))
    except KeyError:
        return {}
    rels: dict[str, tuple[str, str, str | None]] = {}
    for rel in ET.fromstring(data).iter(f"{{{_PKG_REL_NS}}}Relationship"):
        rels[rel.get("Id") or ""] = (rel.get("Type") or "", rel.get("Target") or "", rel.get("TargetMode"))
    return rels


def _workbook_part(archive: zipfile.ZipFile) -> str:
    for _rid, (kind, target, _mode) in _read_rels(archive, "").items():
        if kind == _OFFICE_DOC:
            return _resolve("", target)
    return "xl/workbook.xml"


def _sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet title, worksheet part) in workbook order."""
    book = _workbook_part(archive)
    rels = _read_rels(archive, book)
    out: list[tuple[str, str]] = []
    for sheet in ET.fromstring(archive.read(book)).iter(f"{{{_MAIN_NS}}}sheet"):
        rel = rels.get(sheet.get(f"{{{_R_NS}}}id") or "")
        if rel is not None:
            out.append((sheet.get("name") or "", _resolve(book, rel[1])))
    return out


def drawing_hyperlinks(path: Path | str) -> dict[str, dict[str, tuple[str, str | None]]]:
    """{sheet title: {drawing relationship id: (target, target mode)}} for picture links."""
    out: dict[str, dict[str, tuple[str, str | None]]] = {}
    with zipfile.ZipFile(path) as archive:
        for title, sheet_part in _sheet_parts(archive):
            for kind, target, _mode in _read_rels(archive, sheet_part).values():
                if kind != _DRAWING:
                    continue
                drawing_part = _resolve(sheet_part, target)
                for rid, (rel_kind, rel_target, rel_mode) in _read_rels(archive, drawing_part).items():
                    if rel_kind == _HYPERLINK:
                        out.setdefault(title, {})[rid] = (rel_target, rel_mode)
    return out


def picture_of(image: Any):
    """The ``xdr:pic`` element openpyxl will write for an image, if it has one yet."""
    anchor = getattr(image, "anchor", None)
    if anchor is None or isinstance(anchor, str):
        return None
    pic = getattr(anchor, "pic", None)
    if pic is None and getattr(anchor, "groupShape", None) is not None:
        pic = anchor.groupShape.pic
    return pic


def load_workbook_keeping_links(path: Path | str, **kwargs):
    """``openpyxl.load_workbook`` whose pictures keep their hyperlinks when saved."""
    from openpyxl import load_workbook  # noqa: PLC0415

    install()
    workbook = load_workbook(path, **kwargs)
    if kwargs.get("read_only"):
        return workbook
    try:
        links = drawing_hyperlinks(path)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError):
        links = {}
    for sheet in workbook.worksheets:
        table = links.get(sheet.title, {})
        for image in getattr(sheet, "_images", []):
            pic = picture_of(image)
            if pic is None:
                continue
            props = pic.nvPicPr.cNvPr
            for attr in LINK_ATTRS:
                link = getattr(props, attr, None)
                if link is not None and link.id:
                    setattr(image, f"_crfs_{attr}", table.get(link.id))
    return workbook


def set_picture_link(image: Any, target: str, *, tooltip: str = "", name: str = "", descr: str = "") -> None:
    """Give a (new or loaded) openpyxl image a click link, ScreenTip, shape name and alt text."""
    image._crfs_hlinkClick = (target, "External")
    meta = dict(getattr(image, "_crfs_meta", None) or {})
    if tooltip:
        meta["tooltip"] = tooltip
    if name:
        meta["name"] = name
    if descr:
        meta["descr"] = descr
    image._crfs_meta = meta


def picture_name(image: Any) -> str:
    meta = getattr(image, "_crfs_meta", None) or {}
    if meta.get("name"):
        return str(meta["name"])
    pic = picture_of(image)
    if pic is None:
        return ""
    return str(pic.nvPicPr.cNvPr.name or "")


def install() -> None:
    """Patch openpyxl's drawing writer once (idempotent, thread-safe)."""
    global _installed
    with _install_lock:
        if _installed:
            return
        from openpyxl.drawing.spreadsheet_drawing import SpreadsheetDrawing  # noqa: PLC0415

        original = SpreadsheetDrawing._write

        def _write(self):
            return _write_with_links(self, original)

        SpreadsheetDrawing._write = _write
        _installed = True


def _write_with_links(drawing, original):
    from openpyxl.drawing.image import Image  # noqa: PLC0415
    from openpyxl.drawing.spreadsheet_drawing import _check_anchor  # noqa: PLC0415
    from openpyxl.drawing.text import Hyperlink  # noqa: PLC0415
    from openpyxl.packaging.relationship import Relationship  # noqa: PLC0415

    objects = drawing.charts + drawing.images
    extra: list[Relationship] = []
    next_id = len(objects)
    for idx, obj in enumerate(objects, 1):
        if not isinstance(obj, Image):
            continue
        meta = getattr(obj, "_crfs_meta", None) or {}
        targets = {attr: getattr(obj, f"_crfs_{attr}", None) for attr in LINK_ATTRS}
        anchor = getattr(obj, "anchor", None)
        if anchor is None or isinstance(anchor, str):
            if not meta and not any(targets.values()):
                continue
            obj.anchor = anchor = _check_anchor(obj)
        pic = picture_of(obj)
        if pic is None:
            pic = drawing._picture_frame(idx)
            anchor.pic = pic
        props = pic.nvPicPr.cNvPr
        if anchor.pic is pic:
            props.id = idx
        if meta.get("name"):
            props.name = meta["name"]
        if meta.get("descr"):
            props.descr = meta["descr"]
        if meta.get("name") and pic.nvPicPr.cNvPicPr is not None:
            from openpyxl.drawing.picture import PictureLocking  # noqa: PLC0415

            pic.nvPicPr.cNvPicPr.picLocks = PictureLocking(noChangeAspect=True)
        for attr in LINK_ATTRS:
            target = targets[attr]
            current = getattr(props, attr, None)
            if not target:
                if current is not None and current.id:
                    # The target could not be read back: drop the link rather than write a dangling id.
                    setattr(props, attr, None)
                continue
            next_id += 1
            rid = f"rId{next_id}"
            link = current if current is not None else Hyperlink()
            link.id = rid
            if attr == "hlinkClick" and meta.get("tooltip"):
                link.tooltip = meta["tooltip"]
            setattr(props, attr, link)
            extra.append(Relationship(Id=rid, type="hyperlink", Target=target[0], TargetMode=target[1] or "External"))
    tree = original(drawing)
    for rel in extra:
        drawing._rels.append(rel)
    return tree


# ---------------------------------------------------------------- closed-file safety

_LOSSY_PARTS = (
    (re.compile(r"^xl/activeX/"), "ActiveX controls"),
    (re.compile(r"^xl/ctrlProps/"), "form controls"),
    (re.compile(r"^xl/slicers?/|^xl/slicerCaches/"), "slicers"),
    (re.compile(r"^xl/timelines?/|^xl/timelineCaches/"), "timelines"),
    (re.compile(r"^xl/charts/chartEx"), "new-style charts"),
    (re.compile(r"^xl/threadedComments/"), "threaded comments"),
    (re.compile(r"^xl/model/"), "a data model"),
    (re.compile(r"^xl/richData/"), "pictures in cells"),
    (re.compile(r"^xl/media/.*\.(emf|wmf|svg)$", re.IGNORECASE), "vector pictures"),
)
_LOSSY_DRAWING = (
    (b"<xdr:sp>", "shapes or text boxes"),
    (b"<xdr:sp ", "shapes or text boxes"),
    (b"<xdr:grpSp", "grouped drawings"),
    (b"<xdr:cxnSp", "connectors"),
    (b"AlternateContent", "controls or new-style drawings"),
    (b"/diagram", "SmartArt"),
)
_LOSSY_SHEET = (
    (b"<x14:dataValidation", "extended data validation"),
    (b"<x14:conditionalFormatting", "extended conditional formatting"),
    (b"<x14:sparklineGroup", "sparklines"),
    (b"<x14:slicerList", "slicers"),
    (b"legacyDrawingHF", "header/footer pictures"),
)
_VML_CONTROL = re.compile(rb'ObjectType="(?!Note")[A-Za-z]+"')


def closed_write_risks(path: Path | str) -> list[str]:
    """What an openpyxl rewrite of this workbook would lose; empty when a closed write is safe."""
    found: list[str] = []

    def note(text: str) -> None:
        if text not in found:
            found.append(text)

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                for pattern, text in _LOSSY_PARTS:
                    if pattern.search(name):
                        note(text)
                lowered = name.casefold()
                if lowered.startswith("xl/drawings/drawing") and lowered.endswith(".xml"):
                    data = archive.read(name)
                    for marker, text in _LOSSY_DRAWING:
                        if marker in data:
                            note(text)
                    if b"<xdr:graphicFrame" in data and data.count(b"<xdr:graphicFrame") > data.count(b"/chart\""):
                        note("embedded objects")
                elif lowered.startswith("xl/drawings/vmldrawing") and _VML_CONTROL.search(archive.read(name)):
                    note("form controls")
                elif lowered.startswith("xl/worksheets/sheet") and lowered.endswith(".xml"):
                    data = archive.read(name)
                    for marker, text in _LOSSY_SHEET:
                        if marker in data:
                            note(text)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        note(f"unreadable package ({type(exc).__name__})")
    return found
