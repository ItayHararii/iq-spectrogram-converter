"""Shared visual system for every CRFS IQ Recorder window.

One palette per theme, one application stylesheet built from it, and a few
glyphs (check mark, chevrons, calendar, clock) drawn in code so that check
boxes, radio buttons and drop-down arrows are crisp and visible in both
themes at any Windows scaling. Controls share one height (``CONTROL_H``);
primary buttons are a little taller.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QWidget

LIGHT_MODE = "light"
DARK_MODE = "dark"

CONTROL_H = 36  # line edits, combos, spin boxes, ordinary buttons (px, logical)
PRIMARY_H = 40  # primary action buttons
CELL = 36  # calendar day cell edge
LABEL_W = 150  # caption column shared by the recording form and the Cellular section
_INNER_H = CONTROL_H - 2  # content + padding inside a 1 px border


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    border: str
    focus: str
    danger: str
    ok_bg: str
    ok_fg: str
    wait_bg: str
    wait_fg: str
    bad_bg: str
    bad_fg: str
    log_bg: str
    log_text: str
    new_bg: str
    new_fg: str
    primary_disabled_bg: str
    primary_disabled_fg: str
    button_hover: str
    scrollbar: str


LIGHT = Palette(
    bg="#F4F6F8",
    surface="#FFFFFF",
    text="#0F172A",
    muted="#64748B",
    accent="#0F766E",
    accent_hover="#0D9488",
    accent_pressed="#115E59",
    border="#E2E8F0",
    focus="#0F766E",
    danger="#B91C1C",
    ok_bg="#D1FAE5",
    ok_fg="#065F46",
    wait_bg="#FEF3C7",
    wait_fg="#92400E",
    bad_bg="#FEE2E2",
    bad_fg="#991B1B",
    log_bg="#F8FAFC",
    log_text="#334155",
    new_bg="#16A34A",
    new_fg="#FFFFFF",
    primary_disabled_bg="#99F6E4",
    primary_disabled_fg="#0F766E",
    button_hover="#E2E8F0",
    scrollbar="#CBD5E1",
)

DARK = Palette(
    bg="#0B1220",
    surface="#1E293B",
    text="#F8FAFC",
    muted="#94A3B8",
    accent="#14B8A6",
    accent_hover="#2DD4BF",
    accent_pressed="#0F766E",
    border="#334155",
    focus="#2DD4BF",
    danger="#F87171",
    ok_bg="#064E3B",
    ok_fg="#6EE7B7",
    wait_bg="#78350F",
    wait_fg="#FDE68A",
    bad_bg="#7F1D1D",
    bad_fg="#FECACA",
    log_bg="#0F172A",
    log_text="#CBD5E1",
    new_bg="#16A34A",
    new_fg="#FFFFFF",
    primary_disabled_bg="#134E4A",
    primary_disabled_fg="#5EEAD4",
    button_hover="#334155",
    scrollbar="#475569",
)

THEMES = {LIGHT_MODE: LIGHT, DARK_MODE: DARK}

# Compat aliases used by older imports / screenshots.
BG = LIGHT.bg
SURFACE = LIGHT.surface
TEXT = LIGHT.text
MUTED = LIGHT.muted
ACCENT = LIGHT.accent
BORDER = LIGHT.border

_current_mode = LIGHT_MODE


def normalize_theme(mode: str | None) -> str:
    return DARK_MODE if str(mode or "").strip().casefold() == DARK_MODE else LIGHT_MODE


def current_mode() -> str:
    return _current_mode


def current_palette() -> Palette:
    return THEMES[_current_mode]


def mix(a: str, b: str, t: float) -> str:
    """Colour ``t`` of the way from ``a`` to ``b`` (hex)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


def theme_toggle_tooltip(mode: str | None = None) -> str:
    current = normalize_theme(mode or current_mode())
    if current == DARK_MODE:
        return "Switch to Light Mode"
    return "Switch to Dark Mode"


def theme_toggle_icon(mode: str | None = None, *, size: int = 20) -> QIcon:
    """Moon in light mode (switch to dark); sun in dark mode (switch to light)."""
    current = normalize_theme(mode or current_mode())
    kind = "sun" if current == DARK_MODE else "moon"
    source = max(64, int(size) * 3)
    image = QImage(source, source, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    _paint_theme_glyph(painter, kind, source, QColor(current_palette().text))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    icon = QIcon()
    icon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(pixmap, QIcon.Mode.Active, QIcon.State.Off)
    return icon


def _paint_theme_glyph(painter: QPainter, kind: str, pixel: int, color: QColor) -> None:
    cx = pixel / 2.0
    cy = pixel / 2.0
    if kind == "sun":
        pen = QPen(color)
        pen.setWidthF(max(3.0, pixel * 0.07))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        inner = pixel * 0.30
        outer = pixel * 0.44
        for index in range(8):
            angle = math.radians(index * 45.0)
            painter.drawLine(
                QPointF(cx + inner * math.cos(angle), cy + inner * math.sin(angle)),
                QPointF(cx + outer * math.cos(angle), cy + outer * math.sin(angle)),
            )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        radius = pixel * 0.16
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        return
    body = QPainterPath()
    body.addEllipse(QPointF(cx - pixel * 0.04, cy), pixel * 0.28, pixel * 0.28)
    cut = QPainterPath()
    cut.addEllipse(QPointF(cx + pixel * 0.12, cy - pixel * 0.06), pixel * 0.24, pixel * 0.24)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(body.subtracted(cut))


# ----------------------------------------------------------------------------- glyphs


def _paint_glyph(painter: QPainter, kind: str, px: int, color: QColor) -> None:
    """Outline glyphs on a ``px`` square: chevrons, check, cross, plus, calendar, clock."""
    pen = QPen(color)
    pen.setWidthF(px * 0.11 if kind in {"check", "chevron-down", "chevron-up", "chevron-right", "cross", "plus"} else px * 0.075)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    p = px / 64.0
    if kind == "check":
        painter.drawPolyline([QPointF(16 * p, 34 * p), QPointF(27 * p, 45 * p), QPointF(49 * p, 20 * p)])
    elif kind == "chevron-down":
        painter.drawPolyline([QPointF(18 * p, 26 * p), QPointF(32 * p, 40 * p), QPointF(46 * p, 26 * p)])
    elif kind == "chevron-up":
        painter.drawPolyline([QPointF(18 * p, 38 * p), QPointF(32 * p, 24 * p), QPointF(46 * p, 38 * p)])
    elif kind == "chevron-right":
        painter.drawPolyline([QPointF(26 * p, 18 * p), QPointF(40 * p, 32 * p), QPointF(26 * p, 46 * p)])
    elif kind == "cross":
        painter.drawLine(QPointF(20 * p, 20 * p), QPointF(44 * p, 44 * p))
        painter.drawLine(QPointF(44 * p, 20 * p), QPointF(20 * p, 44 * p))
    elif kind == "plus":
        painter.drawLine(QPointF(32 * p, 16 * p), QPointF(32 * p, 48 * p))
        painter.drawLine(QPointF(16 * p, 32 * p), QPointF(48 * p, 32 * p))
    elif kind == "calendar":
        painter.drawRoundedRect(QRectF(px * 0.14, px * 0.2, px * 0.72, px * 0.66), px * 0.1, px * 0.1)
        painter.drawLine(QPointF(px * 0.14, px * 0.4), QPointF(px * 0.86, px * 0.4))
        for x in (0.34, 0.66):
            painter.drawLine(QPointF(px * x, px * 0.1), QPointF(px * x, px * 0.28))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(px * 0.5, px * 0.63), px * 0.075, px * 0.075)
    elif kind == "clock":
        painter.drawEllipse(QPointF(px / 2, px / 2), px * 0.37, px * 0.37)
        painter.drawLine(QPointF(px / 2, px / 2), QPointF(px / 2, px * 0.26))
        painter.drawLine(QPointF(px / 2, px / 2), QPointF(px * 0.68, px * 0.6))
    elif kind == "grip":
        # Three dots down the middle of a tall strip (splitter handle); the strip
        # is 3/8 of ``px`` wide and 1.5 ``px`` tall.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        cx = px * 3 / 16
        for cy in (px * 0.75 - px * 0.5, px * 0.75, px * 0.75 + px * 0.5):
            painter.drawEllipse(QPointF(cx, cy), px * 0.07, px * 0.07)


_GLYPH_REV = 2  # bump when a glyph drawing changes so cached PNGs are redrawn


def glyph_image(kind: str, color: QColor | str, px: int = 64) -> QImage:
    size = QSize(px * 3 // 8, px * 3 // 2) if kind == "grip" else QSize(px, px)
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    _paint_glyph(painter, kind, px, QColor(color))
    painter.end()
    return image


_GLYPH_ICONS: dict[tuple[str, str, int], QIcon] = {}


def glyph_icon(kind: str, color: QColor | str, size: int = 18) -> QIcon:
    """A glyph as a QIcon (drawn at 4x so it stays crisp at 125 / 150 % scaling).

    Cached per kind, colour and size: theme switches ask for the same few icons
    from every chip and section header."""
    key = (kind, QColor(color).name().lower(), int(size))
    icon = _GLYPH_ICONS.get(key)
    if icon is None:
        icon = QIcon(QPixmap.fromImage(glyph_image(kind, color, max(64, int(size) * 4))))
        _GLYPH_ICONS[key] = icon
    return icon


_GLYPH_URLS: dict[tuple[str, str], str] = {}


def _glyph_dir() -> Path:
    return Path(tempfile.gettempdir()) / "CRFS IQ Recorder" / "ui"


def glyph_url(kind: str, color: str) -> str:
    """``url(...)`` of a PNG glyph for stylesheets, or '' when no file can be written.

    Qt style sheets can only take images from files or resources; the frozen
    build ships no Qt SVG plugin, so the glyphs are rendered once per colour
    into the temp folder and scaled down by the style (never up).
    """
    key = (kind, QColor(color).name().lower())
    cached = _GLYPH_URLS.get(key)
    if cached is not None:
        return cached
    if QGuiApplication.instance() is None:
        return ""  # nothing to paint with yet; computed again once the app exists
    url = ""
    try:
        folder = _glyph_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{kind}-{key[1].lstrip('#')}-r{_GLYPH_REV}.png"
        if not path.is_file():
            tmp = path.with_suffix(f".{os.getpid()}.tmp.png")
            if glyph_image(kind, color).save(str(tmp), "PNG"):
                os.replace(tmp, path)
        if path.is_file():
            url = "url(\"" + path.as_posix().replace('"', "") + "\")"
    except Exception:  # noqa: BLE001 - styling must never stop the app
        url = ""
    _GLYPH_URLS[key] = url
    return url


def _image_rule(kind: str, color: str) -> str:
    url = glyph_url(kind, color)
    return f"image: {url};" if url else ""


# ----------------------------------------------------------------------------- stylesheet


def picker_rules(p: Palette) -> str:
    """Calendar, time wheels, chips and the picker fields (part of the app sheet)."""
    faint = mix(p.muted, p.surface, 0.55)
    return f"""
QLabel#calTitle {{ color: {p.text}; font-size: 15px; font-weight: 700; background: transparent; }}
QLabel#weekday {{ color: {p.muted}; font-size: 11px; font-weight: 700; background: transparent; }}
QPushButton#navBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 16px;
    padding: 0; color: {p.text}; font-size: 20px; font-weight: 600;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
}}
QPushButton#navBtn:hover {{ background: {p.button_hover}; }}
QPushButton#navBtn:pressed {{ background: {p.border}; }}
QPushButton#navBtn:disabled {{ color: {faint}; background: transparent; }}
QPushButton#dayCell {{
    background: transparent; border: 1px solid transparent; border-radius: {CELL // 2}px;
    padding: 0; color: {p.text}; font-size: 13px; font-weight: 500;
    min-width: {CELL - 2}px; max-width: {CELL - 2}px; min-height: {CELL - 2}px; max-height: {CELL - 2}px;
}}
QPushButton#dayCell:hover {{ background: {p.button_hover}; }}
QPushButton#dayCell[outside="true"] {{ color: {p.muted}; }}
QPushButton#dayCell:disabled {{ color: {faint}; background: transparent; }}
QPushButton#dayCell[today="true"] {{ border: 1px solid {p.accent}; color: {p.accent}; font-weight: 700; }}
QPushButton#dayCell[selected="true"] {{ background: {p.accent}; border-color: {p.accent}; color: #FFFFFF; font-weight: 700; }}
QPushButton#dayCell[selected="true"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QLabel#timeDisplay {{ color: {p.text}; font-size: 32px; font-weight: 700; background: transparent; padding: 4px 0; }}
QLabel#wheelColon {{ color: {p.muted}; font-size: 24px; font-weight: 700; background: transparent; padding-bottom: 4px; }}
QListWidget#wheel {{ background: transparent; border: none; outline: 0; color: {p.muted}; }}
QListWidget#wheel::item {{ color: {p.muted}; border-radius: 8px; margin: 0 2px; background: transparent; }}
QListWidget#wheel::item:hover {{ background: {p.button_hover}; color: {p.text}; }}
QListWidget#wheel::item:selected {{ background: {p.accent}; color: #FFFFFF; }}
QListWidget#wheel::item:selected:hover {{ background: {p.accent_hover}; color: #FFFFFF; }}
QPushButton#chip {{
    background: transparent; border: 1px solid {p.border}; border-radius: 14px;
    padding: 0 12px; min-height: 26px; max-height: 26px; color: {p.text}; font-weight: 600;
}}
QPushButton#chip:hover {{ border-color: {p.accent}; color: {p.accent}; background: transparent; }}
QPushButton#chip:pressed {{ background: {p.button_hover}; }}
QFrame#timeChip {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; }}
QFrame#timeChip:hover {{ border-color: {p.accent}; }}
QFrame#timeChip:disabled {{ background: {p.bg}; }}
QPushButton#chipText {{
    background: transparent; border: none; padding: 0 4px 0 10px; min-height: {CONTROL_H - 2}px; max-height: {CONTROL_H - 2}px;
    color: {p.text}; font-weight: 600;
}}
QPushButton#chipText:hover {{ color: {p.accent}; background: transparent; }}
QPushButton#chipText:disabled {{ color: {p.muted}; }}
QPushButton#chipRemove {{
    background: transparent; border: none; border-radius: 11px; padding: 0;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
}}
QPushButton#chipRemove:hover {{ background: {p.bad_bg}; }}
QPushButton#fieldBtn {{
    background: transparent; border: none; border-radius: 6px; padding: 0;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
}}
QPushButton#fieldBtn:hover {{ background: {p.button_hover}; }}
QPushButton#fieldBtn:pressed {{ background: {p.border}; }}
QLineEdit#bareField:disabled {{ color: {p.muted}; background: transparent; }}
QFrame#inputWrap:disabled {{ background: {p.bg}; }}
"""


def stylesheet_for(p: Palette) -> str:
    check = _image_rule("check", "#FFFFFF")
    arrow = _image_rule("chevron-down", p.text)
    arrow_muted = _image_rule("chevron-down", p.muted)
    arrow_up = _image_rule("chevron-up", p.text)
    arrow_up_muted = _image_rule("chevron-up", p.muted)
    faint_border = mix(p.border, p.bg, 0.4)
    return f"""
* {{
    font-family: "Segoe UI", "Segoe UI Variable Text", "Segoe UI Variable", sans-serif;
}}
QMainWindow, QDialog, QWidget#central, QWidget#sftpRoot {{
    background: {p.bg};
    color: {p.text};
    font-size: 13px;
}}
QLabel, QCheckBox, QRadioButton, QStatusBar, QMenuBar, QMenu {{
    color: {p.text};
    background: transparent;
}}
QLabel#muted, QLabel#emptyState {{
    color: {p.muted};
}}
QLabel#error {{
    color: {p.danger};
    font-weight: 500;
}}
QLabel#warn {{
    color: {p.wait_fg};
    font-weight: 600;
}}
QLabel#summary {{
    color: {p.text};
    font-weight: 600;
}}
QLabel#headerLogo {{
    background: transparent;
}}
QLabel#headerTitle {{
    color: {p.text};
    font-size: 18px;
    font-weight: 700;
    background: transparent;
}}
QLabel#headerSub {{
    color: {p.muted};
    font-size: 12px;
    background: transparent;
}}
QLabel#sectionTitle {{
    color: {p.text};
    font-size: 14px;
    font-weight: 700;
}}
QLabel#sensorFactLabel {{
    color: {p.muted};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QLabel#sensorFactValue {{
    color: {p.text};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#storageValueWarn {{
    color: {p.wait_fg};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#storageValueCritical {{
    color: {p.danger};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#storageNoteWarn {{
    color: {p.wait_fg};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#storageNoteCritical {{
    color: {p.danger};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#storageNoteMuted {{
    color: {p.muted};
    font-size: 12px;
    background: transparent;
}}
QLabel#badgeOk, QLabel#badgeWait, QLabel#badgeBad, QLabel#badgeIdle, QLabel#badgeAccent {{
    font-size: 12px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 11px;
    qproperty-alignment: AlignCenter;
}}
QLabel#badgeOk {{ background: {p.ok_bg}; color: {p.ok_fg}; }}
QLabel#badgeWait {{ background: {p.wait_bg}; color: {p.wait_fg}; }}
QLabel#badgeBad {{ background: {p.bad_bg}; color: {p.bad_fg}; }}
QLabel#badgeIdle {{ background: {p.border}; color: {p.muted}; }}
QLabel#badgeAccent {{ background: {p.accent}; color: #FFFFFF; }}
QLabel#linkStatusOk {{
    color: {p.ok_fg};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#linkStatusWait {{
    color: {p.wait_fg};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#linkStatusBad {{
    color: {p.danger};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#linkStatusIdle {{
    color: {p.muted};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#unitSuffix {{
    color: {p.muted};
    font-weight: 600;
    padding-right: 10px;
    background: transparent;
}}
QLabel#sizeHint {{
    color: {p.text};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#phaseBusy {{ color: {p.accent}; font-weight: 600; }}
QLabel#phaseOk {{ color: {p.ok_fg}; font-weight: 600; }}
QLabel#phaseBad {{ color: {p.danger}; font-weight: 600; }}
QLabel#newBadge {{
    background: {p.new_bg};
    color: {p.new_fg};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.4px;
    border: none;
    border-radius: 6px;
    padding: 3px 8px;
}}
QLabel#pathLabel {{
    color: {p.text};
    font-weight: 600;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 12px;
}}
QLabel#previewFile {{
    color: {p.text};
    font-weight: 600;
}}
QFrame#toolbar, QFrame#previewPane {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QLabel#previewImage {{
    background: {p.surface};
    color: {p.muted};
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QLabel#emptyState {{
    color: {p.muted};
    font-size: 14px;
    padding: 32px;
}}
QProgressBar#thin {{
    background: {p.border};
    border: none;
    border-radius: 2px;
    min-height: 4px;
    max-height: 4px;
    text-align: center;
}}
QProgressBar#thin::chunk {{
    background: {p.accent};
    border-radius: 2px;
}}
QFrame#card, QFrame#headerBar, QFrame#sensorStrip {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QFrame#headerBar {{
    border-radius: 0;
    border: none;
    border-bottom: 1px solid {p.border};
}}
QFrame#actionBar {{
    background: {mix(p.surface, p.bg, 0.5)};
    border: none;
    border-top: 1px solid {p.border};
    border-bottom-left-radius: 12px;
    border-bottom-right-radius: 12px;
}}
QFrame#actionBar QLabel {{ background: transparent; }}
QLabel#reasonText {{
    color: {p.muted};
    font-size: 12px;
}}
QFrame#resultCard {{
    background: {mix(p.surface, p.bg, 0.5)};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QLabel#resultHeading {{
    color: {p.muted};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
}}
QLabel#resultTitle {{
    color: {p.text};
    font-weight: 600;
}}
QTabWidget#workflowTabs::pane {{
    border: none;
    background: transparent;
}}
QTabBar#workflowTabBar {{
    background: transparent;
}}
QTabBar#workflowTabBar::tab {{
    background: transparent;
    color: {p.muted};
    border: none;
    border-bottom: 3px solid transparent;
    padding: 8px 18px;
    margin-right: 6px;
    font-size: 14px;
    font-weight: 700;
}}
QTabBar#workflowTabBar::tab:selected {{
    color: {p.text};
    border-bottom: 3px solid {p.accent};
}}
QTabBar#workflowTabBar::tab:hover:!selected {{
    color: {p.text};
}}
QFrame#section, QWidget#section {{
    background: transparent;
    border: none;
}}
QFrame#divider {{
    background: {p.border};
    border: none;
    min-height: 1px;
    max-height: 1px;
}}
QFrame#notice {{
    background: {p.wait_bg};
    border: none;
    border-radius: 8px;
}}
QFrame#notice QLabel {{
    color: {p.wait_fg};
    background: transparent;
}}
QFrame#inputWrap {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    min-height: {CONTROL_H}px;
    max-height: {CONTROL_H}px;
}}
QFrame#inputWrap:focus-within {{
    border: 1px solid {p.focus};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 0 10px;
    min-height: {_INNER_H}px;
    max-height: {_INNER_H}px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
}}
QPlainTextEdit {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {p.focus};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {p.muted};
    background: {p.bg};
}}
QLineEdit#bareField {{
    border: none;
    background: transparent;
    color: {p.text};
    padding: 0 8px;
    min-height: {_INNER_H}px;
    max-height: {_INNER_H}px;
}}
QComboBox {{
    combobox-popup: 0;
    padding-right: 30px;
}}
QComboBox::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    {arrow}
    width: 12px;
    height: 12px;
}}
QComboBox::down-arrow:disabled {{
    {arrow_muted}
}}
QComboBox QAbstractItemView {{
    background: {p.surface};
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
    border: 1px solid {p.border};
    outline: 0;
    padding: 0;
    margin: 0;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px;
    padding: 6px 10px;
    color: {p.text};
    background: {p.surface};
}}
QComboBox QAbstractItemView::item:selected {{
    background: {p.accent};
    color: #FFFFFF;
}}
QComboBoxPrivateContainer {{
    background: {p.surface};
    border: 1px solid {p.border};
    margin: 0;
    padding: 0;
}}
QComboBoxPrivateContainer QScrollBar:vertical {{
    background: {p.surface};
}}
QSpinBox, QDoubleSpinBox {{
    padding-right: 28px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 24px;
    height: {CONTROL_H // 2 - 1}px;
    border: none;
    border-left: 1px solid {faint_border};
    border-top-right-radius: 8px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 24px;
    height: {CONTROL_H // 2 - 1}px;
    border: none;
    border-left: 1px solid {faint_border};
    border-bottom-right-radius: 8px;
    background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {p.button_hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    {arrow_up}
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    {arrow}
    width: 10px;
    height: 10px;
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off {{
    {arrow_up_muted}
}}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off {{
    {arrow_muted}
}}
QCheckBox, QRadioButton {{
    color: {p.text};
    spacing: 8px;
    padding: 4px 2px;
    min-height: 20px;
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {p.muted};
}}
QRadioButton {{
    font-weight: 600;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 2px solid {p.muted};
    background: {p.surface};
}}
QCheckBox::indicator {{
    border-radius: 5px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    {check}
}}
QCheckBox::indicator:checked:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QRadioButton::indicator:checked {{
    border-color: {p.accent};
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
        stop: 0 {p.accent}, stop: 0.52 {p.accent}, stop: 0.6 {p.surface}, stop: 1 {p.surface});
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {p.border};
    background: {p.bg};
}}
QCheckBox::indicator:checked:disabled {{
    background: {p.primary_disabled_bg};
    border-color: {p.primary_disabled_bg};
}}
QRadioButton::indicator:checked:disabled {{
    border-color: {p.primary_disabled_bg};
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
        stop: 0 {p.primary_disabled_fg}, stop: 0.52 {p.primary_disabled_fg}, stop: 0.6 {p.bg}, stop: 1 {p.bg});
}}
QPushButton {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 0 14px;
    min-height: {_INNER_H}px;
    max-height: {_INNER_H}px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {p.button_hover};
    border-color: {p.border};
}}
QPushButton:pressed {{
    background: {p.border};
}}
QPushButton:disabled {{
    color: {p.muted};
    background: {p.bg};
}}
QPushButton:focus {{
    border: 1px solid {p.focus};
}}
QPushButton#primary {{
    background: {p.accent};
    color: #FFFFFF;
    font-weight: 700;
    min-height: {PRIMARY_H - 2}px;
    max-height: {PRIMARY_H - 2}px;
    padding: 0 20px;
    font-size: 14px;
    border: 1px solid {p.accent};
}}
QPushButton#primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#primary:pressed {{ background: {p.accent_pressed}; }}
QPushButton#primary:disabled {{
    background: {p.primary_disabled_bg};
    color: {p.primary_disabled_fg};
    border-color: {p.primary_disabled_bg};
}}
QPushButton#accent {{
    background: {p.accent};
    color: #FFFFFF;
    border: 1px solid {p.accent};
}}
QPushButton#accent:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#accent:pressed {{ background: {p.accent_pressed}; }}
QPushButton#accent:disabled {{
    background: {p.primary_disabled_bg};
    color: {p.primary_disabled_fg};
    border-color: {p.primary_disabled_bg};
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {p.border};
}}
QPushButton#ghost:hover {{
    background: {p.button_hover};
}}
QPushButton#ghost:disabled {{
    background: transparent;
    color: {p.muted};
}}
QPushButton#linkBtn {{
    background: transparent;
    border: none;
    color: {p.accent};
    padding: 0 4px;
    min-height: 24px;
    max-height: 24px;
    font-weight: 600;
}}
QPushButton#linkBtn:hover {{ color: {p.accent_hover}; background: transparent; }}
QPushButton#linkBtn:pressed {{ background: transparent; }}
QPushButton#linkBtn:disabled {{ color: {p.muted}; background: transparent; }}
QPushButton#sectionToggle {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {p.text};
    font-size: 14px;
    font-weight: 700;
    text-align: left;
    padding: 0 8px 0 4px;
    min-height: 32px;
    max-height: 32px;
}}
QPushButton#sectionToggle:hover {{ background: {p.button_hover}; }}
QPushButton#sectionToggle:pressed {{ background: {p.border}; }}
QPushButton#iconBtn {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 0;
    min-width: {_INNER_H}px;
    max-width: {_INNER_H}px;
}}
QPushButton#iconBtn:hover {{ background: {p.button_hover}; }}
QPushButton#themeToggle {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 0px;
    min-width: {_INNER_H}px;
    max-width: {_INNER_H}px;
    min-height: {_INNER_H}px;
    max-height: {_INNER_H}px;
}}
QPushButton#themeToggle:hover {{
    background: {p.button_hover};
    border-color: {p.accent};
}}
QPushButton#themeToggle:pressed {{
    background: {p.border};
}}
QDialogButtonBox QPushButton {{
    min-width: 88px;
}}
QPlainTextEdit#log, QPlainTextEdit#log:read-only {{
    background: {p.log_bg};
    color: {p.log_text};
    border: 1px solid {p.border};
    border-radius: 8px;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 12px;
    padding: 8px;
}}
QStatusBar {{
    background: {p.surface};
    color: {p.muted};
    border-top: 1px solid {p.border};
}}
QHeaderView::section {{
    background: {p.bg};
    color: {p.muted};
    font-weight: 700;
    font-size: 11px;
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 8px 10px;
}}
QHeaderView::section:hover {{
    color: {p.text};
    background: {p.button_hover};
}}
QHeaderView::section:pressed {{
    color: {p.text};
    background: {p.border};
}}
QTableWidget, QTreeWidget {{
    background: {p.surface};
    alternate-background-color: {p.log_bg};
    color: {p.text};
    gridline-color: {p.border};
    border: 1px solid {p.border};
    border-radius: 8px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
}}
QTableWidget::item, QTreeWidget::item {{
    padding: 6px 8px;
}}
QTableWidget::item:selected, QTreeWidget::item:selected {{
    background: {p.accent};
    color: #FFFFFF;
}}
QTreeWidget::branch {{
    background: {p.surface};
}}
QTreeWidget::branch:selected {{
    background: {p.accent};
}}
QProgressBar {{
    background: {p.bg};
    border: 1px solid {p.border};
    border-radius: 6px;
    text-align: center;
    color: {p.text};
    min-height: 12px;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 5px;
}}
QSplitter::handle:horizontal {{
    background: transparent;
    border-radius: 5px;
    {_image_rule("grip", p.muted)}
}}
QSplitter::handle:horizontal:hover {{
    background: {p.button_hover};
    {_image_rule("grip", p.accent)}
}}
QSplitter::handle:vertical {{
    background: transparent;
}}
QSplitter::handle:vertical:hover {{
    background: {p.button_hover};
}}
QToolTip {{
    background: {p.text};
    color: {p.surface};
    border: none;
    padding: 6px 8px;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.scrollbar};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {p.scrollbar};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
""" + picker_rules(p)


def _app_palette(p: Palette) -> QPalette:
    pal = QPalette()
    text = QColor(p.text)
    pal.setColor(QPalette.ColorRole.Window, QColor(p.bg))
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, QColor(p.surface))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p.log_bg))
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, QColor(p.surface))
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.muted))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.text))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(p.surface))
    pal.setColor(QPalette.ColorRole.Light, QColor(p.surface))
    pal.setColor(QPalette.ColorRole.Midlight, QColor(p.border))
    pal.setColor(QPalette.ColorRole.Dark, QColor(p.border))
    pal.setColor(QPalette.ColorRole.Mid, QColor(p.border))
    pal.setColor(QPalette.ColorRole.Shadow, QColor(p.border))
    return pal


def apply_theme(app: QApplication, mode: str | None = None) -> None:
    """Apply a theme to the whole application.

    Setting the application style sheet re-polishes every widget of every
    top-level window, which is the one expensive step of a theme switch (and
    of every test that creates a window); it is done only when the sheet
    really changes. The style object and the font are set once.
    """
    global _current_mode
    _current_mode = normalize_theme(mode)
    p = current_palette()
    if not app.property("_crfs_fusion"):
        # Once: with a style sheet active app.style() is the sheet proxy, so the
        # style's own name cannot be used to tell whether Fusion is already set.
        app.setStyle("Fusion")
        app.setProperty("_crfs_fusion", True)
    try:
        scheme = Qt.ColorScheme.Dark if _current_mode == DARK_MODE else Qt.ColorScheme.Light
        app.styleHints().setColorScheme(scheme)
    except Exception:
        pass
    current_font = app.font()
    if current_font.family() != "Segoe UI" or current_font.pixelSize() != 13:
        font = QFont("Segoe UI")
        font.setPixelSize(13)
        app.setFont(font)
    app.setPalette(_app_palette(p))
    sheet = stylesheet_for(p)
    if app.styleSheet() == sheet:
        return
    app.setStyleSheet(sheet)
    for widget in app.topLevelWidgets():
        polish_combo_popups(widget)
        restyle(widget)


def apply_combo_popup_palette(combo: QComboBox, *, dark: bool | None = None) -> None:
    """Keep dropdown lists themed, without native black popup chrome."""
    del dark
    polish_combo(combo)


def polish_combo(combo: QComboBox) -> None:
    _style_combo_popup(combo)
    if getattr(combo, "_crfs_popup_hooked", False):
        return
    original = combo.showPopup

    def wrapped_show() -> None:
        _style_combo_popup(combo)
        original()
        _style_combo_popup(combo)

    combo.showPopup = wrapped_show  # type: ignore[method-assign]
    combo._crfs_popup_hooked = True  # type: ignore[attr-defined]


def _style_combo_popup(combo: QComboBox) -> None:
    p = current_palette()
    bg = QColor(p.surface)
    fg = QColor(p.text)
    pal = QPalette(combo.palette())
    for role in (
        QPalette.ColorRole.Base,
        QPalette.ColorRole.Window,
        QPalette.ColorRole.Button,
        QPalette.ColorRole.AlternateBase,
        QPalette.ColorRole.Light,
        QPalette.ColorRole.Midlight,
    ):
        pal.setColor(role, bg)
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(role, fg)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    combo.setPalette(pal)
    # The popup's colours come from the application sheet ("QComboBox
    # QAbstractItemView", "QComboBoxPrivateContainer"); only the palette is set
    # here, for the parts Fusion paints from the palette. Per-widget style
    # sheets on the popup cost ~12 ms each (a style proxy and a re-polish).
    view = combo.view()
    view.setPalette(pal)
    view.setAutoFillBackground(True)
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setContentsMargins(0, 0, 0, 0)
    popup = view.parentWidget()
    if popup is not None:
        popup.setAutoFillBackground(True)
        popup.setPalette(pal)
        if not popup.property("_crfs_no_shadow"):
            try:
                popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
            except Exception:
                pass
            popup.setProperty("_crfs_no_shadow", True)


def polish_combo_popups(root: QWidget) -> None:
    for combo in root.findChildren(QComboBox):
        polish_combo(combo)


def format_bytes(size: int) -> str:
    value = float(max(int(size or 0), 0))
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        text = f"{value / 1024:.1f}"
        return f"{text.rstrip('0').rstrip('.')} KB"
    if value < 1024 * 1024 * 1024:
        text = f"{value / (1024 * 1024):.1f}"
        return f"{text.rstrip('0').rstrip('.')} MB"
    if value < 1024 * 1024 * 1024 * 1024:
        text = f"{value / (1024 * 1024 * 1024):.2f}"
        return f"{text.rstrip('0').rstrip('.')} GB"
    text = f"{value / (1024 * 1024 * 1024 * 1024):.2f}"
    return f"{text.rstrip('0').rstrip('.')} TB"


def format_display_date(value: datetime) -> str:
    return value.strftime("%d/%m/%Y")


def format_display_datetime(value: datetime) -> str:
    return value.strftime("%d/%m/%Y %H:%M:%S")


def restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def divider(parent: QWidget | None = None) -> QFrame:
    """A 1 px horizontal rule in the border colour (sections inside one card)."""
    line = QFrame(parent)
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    return line


def available_screen_rect(widget: QWidget | None = None) -> QRect:
    screen = None
    if widget is not None:
        screen = widget.screen()
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QRect(0, 0, 1280, 720)
    return screen.availableGeometry()


def fit_window_to_screen(
    widget: QWidget,
    width: int,
    height: int,
    *,
    min_width: int = 480,
    min_height: int = 360,
    margin: int = 24,
) -> None:
    """Open inside the usable desktop, still resizable."""
    geo = available_screen_rect(widget)
    max_w = max(320, geo.width() - margin)
    max_h = max(280, geo.height() - margin)
    min_w = min(min_width, max_w)
    min_h = min(min_height, max_h)
    widget.setMinimumSize(min_w, min_h)
    widget.setMaximumSize(16777215, 16777215)
    w = min(max(width, min_w), max_w)
    h = min(max(height, min_h), max_h)
    widget.resize(w, h)
    x = geo.x() + max(0, (geo.width() - w) // 2)
    y = geo.y() + max(0, (geo.height() - h) // 2)
    widget.move(x, y)
