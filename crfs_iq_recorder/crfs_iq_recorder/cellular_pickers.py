"""Graphical date and time pickers for the Cellular Collection panel.

A Material-style month calendar, a mobile-style time picker with wheels and
preset chips, and the compact fields that open them. Nothing here is typed by
the operator: the fields are read-only and the popups are the only way to set
a value. All text follows the Windows regional settings through
``system_locale()``; no UTC offset or zone name is ever shown.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QDate, QEvent, QLocale, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTime, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import Palette, available_screen_rect, current_palette

CELL = 36  # day cell edge, px
WHEEL_ROW = 36  # one wheel row, px
WHEEL_ROWS = 5  # rows visible in a wheel
_PAD = WHEEL_ROWS // 2  # blank rows so the first/last value can sit in the middle
_THEME_EVENTS = (QEvent.Type.StyleChange, QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
_PRESETS = (time(9, 0), time(12, 0), time(16, 0), time(18, 0))


# ----------------------------------------------------------------------------- locale


def system_locale() -> QLocale:
    """The Windows regional settings. Tests monkeypatch this."""
    return QLocale.system()


def short_time_format(source: str | None = None) -> str:
    """Locale short time format without seconds, milliseconds or zone names."""
    fmt = system_locale().timeFormat(QLocale.FormatType.ShortFormat) if source is None else source
    out: list[str] = []
    quoted = False
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch in "szt":
            while i + 1 < len(fmt) and fmt[i + 1] == ch:
                i += 1
            # Drop the separator that led into the token (':' in 'mm:ss', ' ' in 'mm t').
            while out and not out[-1].isalpha() and out[-1] != "'":
                out.pop()
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def locale_uses_12h() -> bool:
    """True when the short time format carries an AM/PM marker (``AP``, ``ap``, ``Ap`` or ``a``)."""
    fmt = short_time_format()
    unquoted = "".join(part for index, part in enumerate(fmt.split("'")) if index % 2 == 0)
    return any(ch in "aA" for ch in unquoted)


def _plain_spaces(text: str) -> str:
    # Recent CLDR data puts a narrow no-break space before AM/PM; a plain space
    # reads the same and keeps the text predictable in logs and Excel.
    return text.replace("\u202f", " ").replace("\u00a0", " ")


def format_date(d: date) -> str:
    return _plain_spaces(system_locale().toString(QDate(d.year, d.month, d.day), QLocale.FormatType.ShortFormat))


def format_time(t: time) -> str:
    return _plain_spaces(system_locale().toString(QTime(t.hour, t.minute), short_time_format()))


def format_datetime(value: datetime) -> str:
    """Date and time as the regional settings show them (local wall clock, no zone text)."""
    return f"{format_date(value.date())} {format_time(value.time())}"


def _first_day_of_week() -> int:
    """ISO weekday (Monday=1 … Sunday=7) the locale starts its week on."""
    value = system_locale().firstDayOfWeek()
    return int(getattr(value, "value", value)) or 1


def _now_time() -> time:
    now = datetime.now()
    return time(now.hour, now.minute)


# ----------------------------------------------------------------------------- styling


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


def picker_stylesheet(p: Palette) -> str:
    """Rules for the custom parts (day cells, wheels, chips); everything else uses the app sheet."""
    faint = _mix(p.muted, p.surface, 0.55)
    return f"""
QLabel#calTitle {{ color: {p.text}; font-size: 15px; font-weight: 700; background: transparent; }}
QLabel#weekday {{ color: {p.muted}; font-size: 11px; font-weight: 700; background: transparent; }}
QPushButton#navBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 16px;
    padding: 0; color: {p.text}; font-size: 20px; font-weight: 600;
}}
QPushButton#navBtn:hover {{ background: {p.button_hover}; }}
QPushButton#navBtn:pressed {{ background: {p.border}; }}
QPushButton#navBtn:disabled {{ color: {faint}; background: transparent; }}
QPushButton#dayCell {{
    background: transparent; border: 1px solid transparent; border-radius: {CELL // 2}px;
    padding: 0; color: {p.text}; font-size: 13px; font-weight: 500;
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
QFrame#timeChip {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 15px; }}
QFrame#timeChip:hover {{ border-color: {p.accent}; }}
QPushButton#chipText {{
    background: transparent; border: none; padding: 0 2px 0 10px; min-height: 26px; max-height: 26px;
    color: {p.text}; font-weight: 600;
}}
QPushButton#chipText:hover {{ color: {p.accent}; background: transparent; }}
QPushButton#chipText:disabled {{ color: {p.muted}; }}
QPushButton#chipRemove {{
    background: transparent; border: none; border-radius: 11px; padding: 0;
    color: {p.muted}; font-size: 15px; font-weight: 700;
}}
QPushButton#chipRemove:hover {{ background: {p.button_hover}; color: {p.danger}; }}
QPushButton#fieldBtn {{ background: transparent; border: none; border-radius: 6px; padding: 0; }}
QPushButton#fieldBtn:hover {{ background: {p.button_hover}; }}
QPushButton#fieldBtn:pressed {{ background: {p.border}; }}
QLineEdit#bareField:disabled {{ color: {p.muted}; background: transparent; }}
QFrame#inputWrap:disabled {{ background: {p.bg}; }}
"""


class _Themed:
    """Mixin: keep the palette-derived stylesheet in step with light/dark toggles."""

    def _refresh_style(self) -> None:
        sheet = picker_stylesheet(current_palette())
        if self.styleSheet() != sheet:  # type: ignore[attr-defined]
            self.setStyleSheet(sheet)  # type: ignore[attr-defined]

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)  # type: ignore[misc]
        if event.type() in _THEME_EVENTS:
            self._refresh_style()


def _glyph_icon(kind: str, color: QColor, size: int = 18) -> QIcon:
    """Calendar or clock outline drawn in the given colour (no emoji font needed)."""
    px = size * 4
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color)
    pen.setWidthF(px * 0.075)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "calendar":
        painter.drawRoundedRect(QRectF(px * 0.14, px * 0.2, px * 0.72, px * 0.66), px * 0.1, px * 0.1)
        painter.drawLine(QPointF(px * 0.14, px * 0.4), QPointF(px * 0.86, px * 0.4))
        for x in (0.34, 0.66):
            painter.drawLine(QPointF(px * x, px * 0.1), QPointF(px * x, px * 0.28))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(px * 0.5, px * 0.63), px * 0.075, px * 0.075)
    else:
        painter.drawEllipse(QPointF(px / 2, px / 2), px * 0.37, px * 0.37)
        painter.drawLine(QPointF(px / 2, px / 2), QPointF(px / 2, px * 0.26))
        painter.drawLine(QPointF(px / 2, px / 2), QPointF(px * 0.68, px * 0.6))
    painter.end()
    return QIcon(QPixmap.fromImage(image))


def _flat(button: QPushButton, name: str, size: int | None = None) -> QPushButton:
    button.setObjectName(name)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if size is not None:
        button.setFixedSize(size, size)
    return button


def _place_under(dialog: QDialog, anchor: QWidget) -> None:
    """Drop the dialog just below the field, flipped above it when the screen ends."""
    width, height = dialog.width(), dialog.height()
    screen = available_screen_rect(anchor)
    below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
    x = min(max(below.x(), screen.left()), max(screen.left(), screen.right() - width))
    y = below.y()
    if y + height > screen.bottom():
        y = max(screen.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - height - 40)
    dialog.move(x, y)


# ----------------------------------------------------------------------------- flow layout


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to new rows when the width runs out."""

    def __init__(self, parent: QWidget | None = None, *, h_spacing: int = 6, v_spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize(0, 0)
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if row_height and x + hint.width() > area.right() + 1:
                x, y, row_height = area.x(), y + row_height + self._v, 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


# ----------------------------------------------------------------------------- calendar


class CalendarPopup(_Themed, QDialog):
    """Month calendar: ‹ › navigation, weekday initials, 6×7 day grid, Cancel / OK."""

    date_changed = Signal(object)

    def __init__(self, initial: date | None = None, *, minimum: date | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick a date")
        self._minimum = minimum
        selected = initial or date.today()
        if minimum is not None and selected < minimum:
            selected = minimum
        self._selected = selected
        self._visible = (selected.year, selected.month)
        self._cells: list[tuple[QPushButton, date]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        head = QHBoxLayout()
        self.prev_btn = _flat(QPushButton("‹"), "navBtn", 32)
        self.next_btn = _flat(QPushButton("›"), "navBtn", 32)
        for button, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, s=step: self._shift_month(s))
        self.title = QLabel()
        self.title.setObjectName("calTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.prev_btn)
        head.addWidget(self.title, 1)
        head.addWidget(self.next_btn)
        layout.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        locale = system_locale()
        first = _first_day_of_week()
        for col in range(7):
            dow = (first - 1 + col) % 7 + 1
            text = locale.dayName(dow, QLocale.FormatType.NarrowFormat) or locale.dayName(dow, QLocale.FormatType.ShortFormat)[:1]
            label = QLabel(text)
            label.setObjectName("weekday")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFixedWidth(CELL)
            grid.addWidget(label, 0, col)
        for index in range(42):
            cell = _flat(QPushButton(), "dayCell", CELL)
            cell.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cell.clicked.connect(lambda _checked=False, i=index: self._on_cell(i))
            grid.addWidget(cell, 1 + index // 7, index % 7)
            self._cells.append((cell, selected))
        grid_host = QHBoxLayout()
        grid_host.addStretch(1)
        grid_host.addLayout(grid)
        grid_host.addStretch(1)
        layout.addLayout(grid_host)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        for button in (self.cancel_btn, self.ok_btn):
            button.installEventFilter(self)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self._refresh_style()
        self._rebuild()
        self.resize(320, 360)

    # ------------------------------------------------------------------ api
    def date(self) -> date:
        return self._selected

    def set_date(self, d: date) -> None:
        if self._minimum is not None and d < self._minimum:
            d = self._minimum
        changed = d != self._selected
        self._selected = d
        self._visible = (d.year, d.month)
        self._rebuild()
        if changed:
            self.date_changed.emit(d)

    def _day_button(self, d: date) -> QPushButton | None:
        """The visible cell for ``d`` (test hook); None when the day is not on the grid."""
        for cell, cell_date in self._cells:
            if cell_date == d:
                return cell
        return None

    # ------------------------------------------------------------------ internals
    def _shift_month(self, step: int) -> None:
        year, month = self._visible
        month += step
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        self._visible = (year, month)
        self._rebuild()

    def _on_cell(self, index: int) -> None:
        self.set_date(self._cells[index][1])

    def _rebuild(self) -> None:
        year, month = self._visible
        locale = system_locale()
        self.title.setText(f"{locale.standaloneMonthName(month, QLocale.FormatType.LongFormat)} {year}")
        first_of_month = date(year, month, 1)
        start = first_of_month - timedelta(days=(first_of_month.isoweekday() - _first_day_of_week()) % 7)
        today = date.today()
        minimum = self._minimum
        for index, (cell, _old) in enumerate(self._cells):
            d = start + timedelta(days=index)
            self._cells[index] = (cell, d)
            cell.setText(str(d.day))
            cell.setEnabled(minimum is None or d >= minimum)
            cell.setProperty("outside", d.month != month)
            cell.setProperty("today", d == today)
            cell.setProperty("selected", d == self._selected)
            cell.style().unpolish(cell)
            cell.style().polish(cell)
        self.prev_btn.setEnabled(minimum is None or (year, month) > (minimum.year, minimum.month))

    def _handle_key(self, key: int) -> bool:
        delta = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}.get(key)
        if delta is not None:
            self.set_date(self._selected + timedelta(days=delta))
            return True
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self._shift_month(-1 if key == Qt.Key.Key_PageUp else 1)
            return True
        return False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._handle_key(event.key()):
            event.accept()
            return
        super().keyPressEvent(event)  # Enter → default (OK), Escape → reject

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress and self._handle_key(event.key()):
            return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta:
            self._shift_month(-1 if delta > 0 else 1)
        event.accept()


# ----------------------------------------------------------------------------- time picker


class _Wheel(QListWidget):
    """One vertical column of values; the current one sits in the middle row."""

    changed = Signal()

    def __init__(self, texts: list[str], *, min_width: int = 64, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("wheel")
        self._count = len(texts)
        self._accum = 0
        font = QFont(self.font())
        font.setPixelSize(18)
        self.setFont(font)
        width = max(min_width, max(QFontMetrics(font).horizontalAdvance(t) for t in texts) + 28)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAutoScroll(False)
        self.setUniformItemSizes(True)
        self.setFixedSize(width, WHEEL_ROW * WHEEL_ROWS)
        for text in [""] * _PAD + texts + [""] * _PAD:
            item = QListWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setSizeHint(QSize(width, WHEEL_ROW))
            if not text:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(item)
        self.currentRowChanged.connect(self._on_row)
        self.setCurrentRow(_PAD)

    def texts(self) -> list[str]:
        return [self.item(_PAD + i).text() for i in range(self._count)]

    def index(self) -> int:
        return min(max(self.currentRow() - _PAD, 0), self._count - 1)

    def set_index(self, index: int) -> None:
        self.setCurrentRow(_PAD + index % self._count)

    def step(self, delta: int) -> None:
        self.set_index(self.index() + delta)

    def _on_row(self, row: int) -> None:
        if row < _PAD or row >= _PAD + self._count:
            self.setCurrentRow(_PAD + self.index())
            return
        for i in range(self._count):
            font = QFont(self.font())
            font.setBold(i == row - _PAD)
            self.item(_PAD + i).setFont(font)
        self._center()
        self.changed.emit()

    def _center(self) -> None:
        item = self.currentItem()
        if item is not None:
            self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._center()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._center()

    def wheelEvent(self, event) -> None:  # noqa: N802
        self._accum += event.angleDelta().y()
        steps = int(self._accum / 120)
        if steps:
            self._accum -= steps * 120
            self.step(-steps)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        moves = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1, Qt.Key.Key_PageUp: -5, Qt.Key.Key_PageDown: 5}
        if key in moves:
            self.step(moves[key])
        elif key == Qt.Key.Key_Home:
            self.set_index(0)
        elif key == Qt.Key.Key_End:
            self.set_index(self._count - 1)
        else:
            super().keyPressEvent(event)  # digits still jump via keyboard search
            return
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.setCurrentRow(self.row(item))
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.mousePressEvent(event)


class TimePickerPopup(_Themed, QDialog):
    """Large time display, hour / minute (/ AM-PM) wheels, preset chips and Done."""

    time_changed = Signal(object)

    def __init__(self, initial: time | None = None, *, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick a time")
        self._twelve = locale_uses_12h()
        self._last: time | None = None
        locale = system_locale()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.display = QLabel()
        self.display.setObjectName("timeDisplay")
        self.display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.display)

        wheels = QHBoxLayout()
        wheels.setSpacing(4)
        wheels.addStretch(1)
        hour_texts = [str(h) for h in range(1, 13)] if self._twelve else [f"{h:02d}" for h in range(24)]
        self.hours = _Wheel(hour_texts)
        self.minutes = _Wheel([f"{m:02d}" for m in range(60)])
        colon = QLabel(":")
        colon.setObjectName("wheelColon")
        colon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wheels.addWidget(self.hours)
        wheels.addWidget(colon)
        wheels.addWidget(self.minutes)
        self.ampm: _Wheel | None = None
        if self._twelve:
            self.ampm = _Wheel([locale.amText(), locale.pmText()], min_width=72)
            wheels.addSpacing(8)
            wheels.addWidget(self.ampm)
        wheels.addStretch(1)
        layout.addLayout(wheels)

        chips_host = QWidget()
        chips = FlowLayout(chips_host)
        self.chips: list[QPushButton] = []
        for preset in _PRESETS:
            chip = _flat(QPushButton(format_time(preset)), "chip")
            chip.setFixedHeight(28)
            chip.setProperty("preset", preset)
            chip.clicked.connect(lambda _checked=False, t=preset: self.set_time(t))
            chips.addWidget(chip)
            self.chips.append(chip)
        layout.addWidget(chips_host)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.done_btn = QPushButton("Done")
        self.done_btn.setObjectName("primary")
        self.done_btn.setDefault(True)
        self.done_btn.clicked.connect(self.accept)
        buttons.addWidget(self.done_btn)
        layout.addLayout(buttons)

        self._refresh_style()
        self.set_time(initial or _now_time())
        for wheel in (self.hours, self.minutes, self.ampm):
            if wheel is not None:
                wheel.changed.connect(self._sync)
        self.resize(self.sizeHint())
        self.hours.setFocus()

    # ------------------------------------------------------------------ api
    def time(self) -> time:
        if self._twelve:
            hour = (self.hours.index() + 1) % 12 + (12 if self.ampm is not None and self.ampm.index() == 1 else 0)
        else:
            hour = self.hours.index()
        return time(hour, self.minutes.index())

    def set_time(self, t: time) -> None:
        self._select(t.hour, t.minute)

    def _select(self, hour: int, minute: int, pm: bool | None = None) -> None:
        """Test hook. ``hour`` is 0–23 when ``pm`` is None, else 1–12 on the 12-hour clock."""
        h24 = hour % 24 if pm is None else hour % 12 + (12 if pm else 0)
        for wheel in (self.hours, self.minutes, self.ampm):
            if wheel is not None:
                wheel.blockSignals(True)
        try:
            self.hours.set_index((h24 % 12 or 12) - 1 if self._twelve else h24)
            self.minutes.set_index(minute % 60)
            if self.ampm is not None:
                self.ampm.set_index(1 if h24 >= 12 else 0)
        finally:
            for wheel in (self.hours, self.minutes, self.ampm):
                if wheel is not None:
                    wheel.blockSignals(False)
        self._sync()

    def _chip(self, t: time) -> QPushButton | None:
        for chip in self.chips:
            if chip.property("preset") == t:
                return chip
        return None

    def _sync(self) -> None:
        t = self.time()
        self.display.setText(format_time(t))
        if t != self._last:
            self._last = t
            self.time_changed.emit(t)


# ----------------------------------------------------------------------------- fields


class _PopupField(_Themed, QWidget):
    """Read-only line plus a glyph button; both open the picker dialog under the field."""

    changed = Signal(object)
    _glyph = "calendar"
    _placeholder = ""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value = None
        self._dialog: QDialog | None = None
        self.frame = QFrame()
        self.frame.setObjectName("inputWrap")
        row = QHBoxLayout(self.frame)
        row.setContentsMargins(0, 0, 3, 0)
        row.setSpacing(0)
        self.edit = QLineEdit()
        self.edit.setObjectName("bareField")
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText(self._placeholder)
        self.edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit.installEventFilter(self)
        self.button = _flat(QPushButton(), "fieldBtn", 30)
        self.button.setIconSize(QSize(18, 18))
        self.button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.button.clicked.connect(self._open_popup)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._refresh_style()

    def _refresh_style(self) -> None:
        super()._refresh_style()
        button = getattr(self, "button", None)
        if button is not None:
            button.setIcon(_glyph_icon(self._glyph, QColor(current_palette().text)))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.edit and self.isEnabled():
            kind = event.type()
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._open_popup()
                return True
            if kind == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Down):
                self._open_popup()
                return True
        return super().eventFilter(watched, event)

    def _open_popup(self) -> QDialog:
        """Open (or re-raise) the picker; returns it so tests can drive it."""
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return self._dialog
        dialog = self._make_popup()
        self._dialog = dialog
        dialog.accepted.connect(lambda: self._take(dialog))
        dialog.finished.connect(lambda _result: self._closed(dialog))
        _place_under(dialog, self)
        dialog.open()
        return dialog

    def _closed(self, dialog: QDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    def _make_popup(self) -> QDialog:  # pragma: no cover - subclasses
        raise NotImplementedError

    def _take(self, dialog: QDialog) -> None:  # pragma: no cover - subclasses
        raise NotImplementedError


class DateField(_PopupField):
    """One-line date field; the calendar popup is the only way to change it."""

    _glyph = "calendar"
    _placeholder = "Pick a date"

    def __init__(self, initial: date | None = None, *, minimum: date | None = None, parent=None) -> None:
        super().__init__(parent)
        self._minimum = minimum
        self.set_date(initial)

    def date(self) -> date | None:
        return self._value

    def set_date(self, d: date | None) -> None:
        changed = d != self._value
        self._value = d
        self.edit.setText(format_date(d) if d is not None else "")
        if changed:
            self.changed.emit(d)

    def set_minimum(self, d: date | None) -> None:
        self._minimum = d

    def _make_popup(self) -> QDialog:
        return CalendarPopup(self._value, minimum=self._minimum, parent=self.window())

    def _take(self, dialog: QDialog) -> None:
        self.set_date(dialog.date())


class TimeField(_PopupField):
    """One-line time-of-day field opening the wheel picker."""

    _glyph = "clock"
    _placeholder = "Pick a time"

    def __init__(self, initial: time | None = None, *, parent=None) -> None:
        super().__init__(parent)
        self.set_time(initial)

    def time(self) -> time | None:
        return self._value

    def set_time(self, t: time | None) -> None:
        if t is not None:
            t = time(t.hour, t.minute)
        changed = t != self._value
        self._value = t
        self.edit.setText(format_time(t) if t is not None else "")
        if changed:
            self.changed.emit(t)

    def _make_popup(self) -> QDialog:
        return TimePickerPopup(self._value or _now_time(), parent=self.window())

    def _take(self, dialog: QDialog) -> None:
        self.set_time(dialog.time())


class _Chip(QFrame):
    """A time chip: the text edits the time, the × removes it."""

    def __init__(self, t: time, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("timeChip")
        self.value = t
        self.setFixedHeight(30)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 3, 0)
        row.setSpacing(0)
        self.text_btn = _flat(QPushButton(format_time(t)), "chipText")
        self.text_btn.setToolTip("Change this time")
        self.remove_btn = _flat(QPushButton("×"), "chipRemove", 22)
        self.remove_btn.setToolTip("Remove this time")
        row.addWidget(self.text_btn)
        row.addWidget(self.remove_btn)


class TimesField(_Themed, QWidget):
    """A wrapping row of time chips plus '+ Add time'; values stay unique and sorted."""

    changed = Signal(object)

    def __init__(self, initial: list[time] | None = None, *, maximum: int = 12, parent=None) -> None:
        super().__init__(parent)
        self._times: list[time] = []
        self._maximum = max(1, int(maximum))
        self._chips: list[_Chip] = []
        self._dialog: QDialog | None = None
        self.flow = FlowLayout(self)
        self.empty_label = QLabel("No start time yet")
        self.empty_label.setObjectName("muted")
        self.empty_label.setFixedHeight(30)
        self.add_btn = QPushButton("+ Add time")
        self.add_btn.setObjectName("ghost")
        self.add_btn.setAutoDefault(False)
        self.add_btn.setFixedHeight(30)
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: self._open_popup(None))
        self.flow.addWidget(self.empty_label)
        self.flow.addWidget(self.add_btn)
        self._refresh_style()
        self.set_times(initial or [])

    # ------------------------------------------------------------------ api
    def times(self) -> list[time]:
        return list(self._times)

    def set_times(self, times: list[time]) -> None:
        cleaned = sorted({time(t.hour, t.minute) for t in times})[: self._maximum]
        changed = cleaned != self._times
        self._times = cleaned
        self._rebuild()
        if changed:
            self.changed.emit(self.times())

    def _add(self, t: time) -> bool:
        """Add one time (test hook); False when it was already present or the field is full."""
        t = time(t.hour, t.minute)
        if t in self._times or len(self._times) >= self._maximum:
            return False
        self.set_times(self._times + [t])
        return True

    def _remove(self, t: time) -> None:
        self.set_times([x for x in self._times if x != time(t.hour, t.minute)])

    def _chip(self, t: time) -> _Chip | None:
        for chip in self._chips:
            if chip.value == t:
                return chip
        return None

    # ------------------------------------------------------------------ internals
    def _rebuild(self) -> None:
        for widget in (*self._chips, self.empty_label, self.add_btn):
            self.flow.removeWidget(widget)
        for chip in self._chips:
            chip.hide()
            chip.deleteLater()
        self._chips = []
        for t in self._times:
            chip = _Chip(t)
            chip.text_btn.clicked.connect(lambda _checked=False, value=t: self._open_popup(value))
            chip.remove_btn.clicked.connect(lambda _checked=False, value=t: self._remove(value))
            self.flow.addWidget(chip)
            self._chips.append(chip)
        self.flow.addWidget(self.empty_label)
        self.flow.addWidget(self.add_btn)
        self.empty_label.setVisible(not self._times)
        full = len(self._times) >= self._maximum
        self.add_btn.setEnabled(not full)
        self.add_btn.setToolTip(f"Up to {self._maximum} start times" if full else "Add a start time")
        self.flow.invalidate()
        self.updateGeometry()

    def _open_popup(self, replacing: time | None) -> QDialog:
        """Open the wheel picker; a chip's time is replaced, otherwise a new one is added."""
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return self._dialog
        dialog = TimePickerPopup(replacing or _now_time(), parent=self.window())
        self._dialog = dialog

        def accepted() -> None:
            kept = [t for t in self._times if t != replacing]
            self.set_times(kept + [dialog.time()])

        def closed(_result: int) -> None:
            if self._dialog is dialog:
                self._dialog = None
            dialog.deleteLater()

        dialog.accepted.connect(accepted)
        dialog.finished.connect(closed)
        _place_under(dialog, self.add_btn if replacing is None else self)
        dialog.open()
        return dialog
