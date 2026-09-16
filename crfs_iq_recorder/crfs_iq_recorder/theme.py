"""Shared visual system for every CRFS IQ Recorder window."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QWidget

LIGHT_MODE = "light"
DARK_MODE = "dark"


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


def stylesheet_for(p: Palette) -> str:
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
QLabel#badgeOk, QLabel#badgeWait, QLabel#badgeBad, QLabel#badgeIdle {{
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
QDialogButtonBox QPushButton {{
    min-width: 88px;
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
QFrame#inputWrap {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    min-height: 36px;
}}
QFrame#inputWrap:focus-within {{
    border: 1px solid {p.focus};
}}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 32px;
    selection-background-color: {p.accent};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {p.focus};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {p.muted};
    background: {p.bg};
}}
QCheckBox {{
    color: {p.text};
    spacing: 8px;
}}
QCheckBox:disabled {{
    color: {p.muted};
}}
QLineEdit#bareField {{
    border: none;
    background: transparent;
    color: {p.text};
    padding: 7px 8px;
    min-height: 32px;
}}
QComboBox {{
    combobox-popup: 0;
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
QRadioButton {{
    spacing: 8px;
    font-weight: 600;
    padding: 6px 4px;
}}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
}}
QCheckBox {{
    spacing: 8px;
}}
QPushButton {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 8px 14px;
    min-height: 32px;
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
    min-height: 38px;
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
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {p.border};
}}
QPushButton#themeToggle {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 0px;
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    max-height: 36px;
}}
QPushButton#themeToggle:hover {{
    background: {p.button_hover};
    border-color: {p.accent};
}}
QPushButton#themeToggle:pressed {{
    background: {p.border};
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
QSplitter::handle {{
    background: {p.border};
    width: 2px;
    height: 2px;
    margin: 4px;
}}
QToolTip {{
    background: {p.text};
    color: {p.surface};
    border: none;
    padding: 6px 8px;
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
"""


STYLESHEET = stylesheet_for(LIGHT)


def apply_theme(app: QApplication, mode: str | None = None) -> None:
    global _current_mode
    _current_mode = normalize_theme(mode)
    p = current_palette()
    app.setStyle("Fusion")
    try:
        scheme = Qt.ColorScheme.Dark if _current_mode == DARK_MODE else Qt.ColorScheme.Light
        app.styleHints().setColorScheme(scheme)
    except Exception:
        pass
    font = QFont("Segoe UI")
    font.setPixelSize(13)
    app.setFont(font)
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
    app.setPalette(pal)
    app.setStyleSheet(stylesheet_for(p))
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
    view = combo.view()
    view.setPalette(pal)
    view.setAutoFillBackground(True)
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setContentsMargins(0, 0, 0, 0)
    view.setStyleSheet(
        f"background: {p.surface}; color: {p.text}; border: none; outline: 0; padding: 0; margin: 0;"
        f" selection-background-color: {p.accent}; selection-color: #FFFFFF;"
    )
    popup = view.parentWidget()
    if popup is not None:
        popup.setAutoFillBackground(True)
        popup.setPalette(pal)
        try:
            popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        except Exception:
            pass
        popup.setStyleSheet(
            f"QWidget {{ background: {p.surface}; color: {p.text}; "
            f"border: 1px solid {p.border}; padding: 0; margin: 0; }}"
        )


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
    text = f"{value / (1024 * 1024 * 1024):.2f}"
    return f"{text.rstrip('0').rstrip('.')} GB"


def format_display_date(value: datetime) -> str:
    return value.strftime("%d/%m/%Y")


def format_display_datetime(value: datetime) -> str:
    return value.strftime("%d/%m/%Y %H:%M:%S")


def restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


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
