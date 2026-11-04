"""Interactive waterfall for Sensor Files: zoom and pan on the time axis, a cursor
readout and the coverage of the requested range.

Everything works from the array the preview worker computed (``WaterfallData``);
nothing is decoded again on a wheel or drag event. The functions above the widget
(time window arithmetic, readout, coverage, ticks) have no Qt in them and are
unit-tested on their own.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from .cellular_quality import _mhz
from .spectrogram_preview import WaterfallData
from .theme import current_palette

ZOOM_STEP = 1.25  # per wheel notch of 120 units
MAX_ZOOM = 64.0  # the visible span never gets smaller than duration / MAX_ZOOM ...
MIN_VISIBLE_BINS = 4  # ... nor than this many time columns
IDLE_READOUT = "Point at the waterfall for time, frequency and level  ·  wheel zooms time  ·  drag pans  ·  Home resets"


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


# ----------------------------------------------------------------------------- time window
@dataclass(frozen=True)
class TimeWindow:
    """The visible part [t0, t1] of a sample lasting ``duration_s`` seconds."""

    duration_s: float
    t0: float
    t1: float
    min_span_s: float

    @classmethod
    def full(cls, duration_s: float, n_time_bins: int = 1) -> "TimeWindow":
        duration = max(float(duration_s), 0.0)
        bins = max(int(n_time_bins), 1)
        min_span = max(duration / MAX_ZOOM, duration / bins * MIN_VISIBLE_BINS)
        return cls(duration, 0.0, duration, min(min_span, duration))

    @property
    def span(self) -> float:
        return self.t1 - self.t0

    @property
    def zoomed(self) -> bool:
        eps = self.duration_s * 1e-9
        return self.t0 > eps or self.t1 < self.duration_s - eps

    @property
    def zoom_factor(self) -> float:
        return self.duration_s / self.span if self.span > 0 else 1.0

    def zoomed_at(self, factor: float, anchor_frac: float) -> "TimeWindow":
        """Zoom by ``factor`` (>1 in, <1 out) keeping the time at ``anchor_frac`` of the window fixed."""
        if self.duration_s <= 0 or factor <= 0:
            return self
        anchor = _clamp(float(anchor_frac), 0.0, 1.0)
        anchor_time = self.t0 + anchor * self.span
        new_span = _clamp(self.span / float(factor), self.min_span_s, self.duration_s)
        t0 = _clamp(anchor_time - anchor * new_span, 0.0, self.duration_s - new_span)
        return replace(self, t0=t0, t1=t0 + new_span)

    def panned(self, dt: float) -> "TimeWindow":
        """Shift the window by ``dt`` seconds; it never leaves [0, duration]."""
        if self.duration_s <= 0:
            return self
        t0 = _clamp(self.t0 + float(dt), 0.0, self.duration_s - self.span)
        return replace(self, t0=t0, t1=t0 + self.span)

    def reset(self) -> "TimeWindow":
        return replace(self, t0=0.0, t1=self.duration_s)

    def time_at(self, frac_from_bottom: float) -> float:
        return self.t0 + _clamp(float(frac_from_bottom), 0.0, 1.0) * self.span

    def frac_of(self, t: float) -> float:
        return (float(t) - self.t0) / self.span if self.span > 0 else 0.0


# ----------------------------------------------------------------------------- coverage
@dataclass(frozen=True)
class Coverage:
    """What the frequency axis shows and which part of the requested range the file lacks."""

    extent: tuple[float, float]
    covered: tuple[float, float] | None
    requested: tuple[float, float] | None
    missing: tuple[tuple[float, float], ...]
    note: str

    @property
    def partial(self) -> bool:
        return bool(self.missing)


def _range_text(lo: float, hi: float) -> str:
    return f"{_mhz(lo * 1e6)}-{_mhz(hi * 1e6)} MHz"


def coverage_of(
    absolute: bool,
    freq_lo: float,
    freq_hi: float,
    requested_mhz: tuple[float, float] | None,
) -> Coverage:
    """The axis extent is the union of what the file covers and what was requested; the
    requested parts outside the file are listed as missing (drawn hatched)."""
    lo, hi = float(freq_lo), float(freq_hi)
    if not absolute:
        note = (
            "Centre unknown (no frequency metadata in the file): the requested range cannot be placed on this picture."
            if requested_mhz
            else "Centre unknown (no frequency metadata in the file)."
        )
        return Coverage((lo, hi), None, None, (), note)
    if not requested_mhz:
        return Coverage((lo, hi), (lo, hi), None, (), f"File covers {_range_text(lo, hi)}; no requested range to compare.")
    r_lo, r_hi = sorted((float(requested_mhz[0]), float(requested_mhz[1])))
    missing: list[tuple[float, float]] = []
    if r_lo < lo:
        missing.append((r_lo, min(r_hi, lo)))
    if r_hi > hi:
        missing.append((max(r_lo, hi), r_hi))
    extent = (min(lo, r_lo), max(hi, r_hi))
    if not missing:
        note = f"File covers {_range_text(lo, hi)}; the requested {_range_text(r_lo, r_hi)} lies inside it."
    elif r_hi <= lo or r_lo >= hi:
        note = f"File covers {_range_text(lo, hi)}, none of the requested {_range_text(r_lo, r_hi)} (hatched: not recorded)."
    else:
        gaps = " and ".join(_range_text(a, b) for a, b in missing)
        note = f"File covers {_range_text(lo, hi)} of the requested {_range_text(r_lo, r_hi)}; hatched {gaps} not recorded."
    return Coverage(extent, (lo, hi), (r_lo, r_hi), tuple(missing), note)


# ----------------------------------------------------------------------------- readout
@dataclass(frozen=True)
class Readout:
    time_s: float
    freq: float
    level_dbfs: float | None
    absolute_time: datetime | None  # local wall clock of the sample under the cursor
    inside_file: bool


def readout_at(
    data: WaterfallData,
    window: TimeWindow,
    coverage: Coverage,
    x_frac: float,
    y_frac_from_bottom: float,
) -> Readout:
    """Values under a cursor given as fractions of the plot area (x left->right, y bottom->top)."""
    e_lo, e_hi = coverage.extent
    freq = e_lo + _clamp(float(x_frac), 0.0, 1.0) * (e_hi - e_lo)
    t = window.time_at(y_frac_from_bottom)
    inside = data.freq_lo <= freq <= data.freq_hi and 0.0 <= t <= data.duration_s
    level = None
    if inside and data.n_freq and data.n_time:
        f_span = data.freq_hi - data.freq_lo
        col = int((freq - data.freq_lo) / f_span * data.n_freq) if f_span > 0 else 0
        row = int(t / data.duration_s * data.n_time) if data.duration_s > 0 else 0
        col = min(max(col, 0), data.n_freq - 1)
        row = min(max(row, 0), data.n_time - 1)
        value = float(data.power_db[col, row])
        level = value if math.isfinite(value) else None
    absolute = None
    if data.capture_start is not None:
        try:
            absolute = (data.capture_start + timedelta(seconds=t)).astimezone()
        except (OverflowError, ValueError):
            absolute = None
    return Readout(time_s=t, freq=freq, level_dbfs=level, absolute_time=absolute, inside_file=inside)


def format_offset(seconds: float) -> str:
    value = float(seconds)
    if abs(value) < 1e-3:
        return f"{value * 1e6:.1f} us"
    if abs(value) < 1.0:
        return f"{value * 1e3:.3f} ms"
    return f"{value:.3f} s"


def format_readout(readout: Readout, data: WaterfallData) -> str:
    if readout.absolute_time is not None:
        stamp = readout.absolute_time
        when = f"{stamp:%H:%M:%S}.{stamp.microsecond:06d} local (+{format_offset(readout.time_s)})"
    else:
        when = f"+{format_offset(readout.time_s)} (start time unknown)"
    if data.absolute:
        freq = f"{readout.freq:.3f} MHz"
    else:
        freq = f"{readout.freq:+.3f} {data.freq_unit}"
    if readout.level_dbfs is not None:
        level = f"{readout.level_dbfs:.1f} dBFS/bin"
    else:
        level = "not recorded" if not readout.inside_file else "no value"
    return f"{when}  ·  {freq}  ·  {level}"


# ----------------------------------------------------------------------------- ticks
def nice_step(span: float, max_ticks: int = 6) -> float:
    span = abs(float(span))
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(int(max_ticks), 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = mult * magnitude
        if step >= raw:
            return step
    return 10.0 * magnitude


def ticks(lo: float, hi: float, max_ticks: int = 6) -> list[float]:
    lo, hi = float(lo), float(hi)
    if not (hi > lo) or not math.isfinite(hi - lo):
        return [lo]
    step = nice_step(hi - lo, max_ticks)
    first = math.ceil(lo / step - 1e-9) * step
    out: list[float] = []
    value = first
    while value <= hi + step * 1e-6 and len(out) < 64:
        out.append(round(value, 12))
        value += step
    return out


def tick_label(value: float, step: float) -> str:
    decimals = max(0, -int(math.floor(math.log10(step)))) if step > 0 else 0
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def time_unit(span_s: float) -> tuple[float, str]:
    if span_s < 1e-3:
        return 1e6, "us"
    if span_s < 1.0:
        return 1e3, "ms"
    return 1.0, "s"


def format_capture_start(start: datetime | None) -> str:
    if start is None:
        return "start time unknown (none in the file's metadata)"
    local = start.astimezone()
    return f"start {local:%d %b %Y %H:%M:%S}.{local.microsecond // 1000:03d} local (file metadata)"


# ----------------------------------------------------------------------------- widget
class WaterfallView(QWidget):
    """Draws a ``WaterfallData`` and lets the operator zoom (wheel) and pan (drag) its time axis.

    The picture with its axes, markers and hatching is rendered once per view change into a
    cached pixmap; moving the mouse only redraws the crosshair over it. Handler durations are
    kept in ``timings`` (last) and ``max_timings`` (worst) in milliseconds for the QA scripts.
    """

    view_changed = Signal()
    readout_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("waterfallView")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._data: WaterfallData | None = None
        self._image: QImage | None = None
        self._buffer = None
        self._bar_image: QImage | None = None
        self._bar_buffer = None
        self._window: TimeWindow | None = None
        self._coverage: Coverage | None = None
        self._scene: tuple[tuple, QPixmap] | None = None
        self._plot_rect = QRectF()
        self._drag: tuple[float, TimeWindow] | None = None
        self._dragged = False
        self._cursor: QPointF | None = None
        self._readout_text = ""
        self.timings: dict[str, float] = {"wheel": 0.0, "drag": 0.0, "move": 0.0, "paint": 0.0, "render": 0.0}
        self.max_timings: dict[str, float] = dict(self.timings)

    # ------------------------------------------------------------------ data
    @property
    def data(self) -> WaterfallData | None:
        return self._data

    @property
    def window(self) -> TimeWindow | None:
        return self._window

    @property
    def coverage(self) -> Coverage | None:
        return self._coverage

    @property
    def zoomed(self) -> bool:
        return bool(self._window is not None and self._window.zoomed)

    def set_data(self, data: WaterfallData | None) -> None:
        self._data = data
        self._scene = None
        self._drag = None
        self._cursor = None
        if data is None:
            self._image = None
            self._buffer = None
            self._bar_image = None
            self._bar_buffer = None
            self._window = None
            self._coverage = None
        else:
            rows, cols = int(data.rgb.shape[0]), int(data.rgb.shape[1])
            self._buffer = data.rgb.tobytes()
            self._image = QImage(self._buffer, cols, rows, cols * 3, QImage.Format.Format_RGB888)
            bar = data.colorbar_rgb
            self._bar_buffer = bar.tobytes()
            self._bar_image = QImage(self._bar_buffer, 1, int(bar.shape[0]), 3, QImage.Format.Format_RGB888)
            self._window = TimeWindow.full(data.duration_s, data.n_time)
            self._coverage = coverage_of(data.absolute, data.freq_lo, data.freq_hi, data.requested_mhz)
        self._set_readout("")
        self.update()
        self.view_changed.emit()

    def clear(self) -> None:
        self.set_data(None)

    def reset_view(self) -> None:
        if self._window is None or not self._window.zoomed:
            return
        self._window = self._window.reset()
        self._scene = None
        self.update()
        self.view_changed.emit()
        self._refresh_readout()

    def zoom_by(self, factor: float, anchor_frac: float = 0.5) -> None:
        if self._window is None:
            return
        new = self._window.zoomed_at(factor, anchor_frac)
        if new != self._window:
            self._window = new
            self._scene = None
            self.update()
            self.view_changed.emit()
            self._refresh_readout()

    def pan_by(self, dt: float) -> None:
        if self._window is None:
            return
        new = self._window.panned(dt)
        if new != self._window:
            self._window = new
            self._scene = None
            self.update()
            self.view_changed.emit()
            self._refresh_readout()

    def plot_rect(self) -> QRectF:
        return QRectF(self._plot_rect)

    def readout_text(self) -> str:
        return self._readout_text

    # ------------------------------------------------------------------ events
    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._data is None or self._window is None:
            event.ignore()
            return
        started = time.perf_counter()
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y() * 4
        if not delta:
            event.ignore()
            return
        factor = ZOOM_STEP ** (delta / 120.0)
        rect = self._plot_rect
        pos = event.position()
        frac = 0.5
        if rect.height() > 0:
            frac = _clamp((rect.bottom() - pos.y()) / rect.height(), 0.0, 1.0)
        self._cursor = QPointF(pos)
        self.zoom_by(factor, frac)
        self._note("wheel", started)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._window is not None and self._window.zoomed:
            self._drag = (event.position().y(), self._window)
            self._dragged = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._window is not None:
            # Not zoomed: nothing to pan, but remember the press so a drag never selects text elsewhere.
            self._drag = (event.position().y(), self._window)
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._data is None or self._window is None:
            super().mouseMoveEvent(event)
            return
        started = time.perf_counter()
        pos = event.position()
        self._cursor = QPointF(pos)
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            y0, start_window = self._drag
            height = self._plot_rect.height()
            if height > 0 and start_window.zoomed:
                dt = (pos.y() - y0) / height * start_window.span
                new = start_window.panned(dt)
                if new != self._window:
                    self._window = new
                    self._scene = None
                    self._dragged = True
                    self.view_changed.emit()
            self._refresh_readout()
            self.update()
            self._note("drag", started)
            event.accept()
            return
        self._refresh_readout()
        self.update(self._plot_rect.toAlignedRect().adjusted(-1, -1, 1, 1))
        self._note("move", started)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._cursor = None
        self._set_readout("")
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Home:
            self.reset_view()
            event.accept()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_by(ZOOM_STEP, 0.5)
            event.accept()
            return
        if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_by(1.0 / ZOOM_STEP, 0.5)
            event.accept()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._window is not None:
            step = self._window.span * 0.1
            self.pan_by(step if key == Qt.Key.Key_Up else -step)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._scene = None
        super().resizeEvent(event)

    # ------------------------------------------------------------------ readout
    def _refresh_readout(self) -> None:
        if self._data is None or self._window is None or self._coverage is None or self._cursor is None:
            self._set_readout("")
            return
        rect = self._plot_rect
        pos = self._cursor
        if rect.width() <= 0 or rect.height() <= 0 or not rect.contains(pos):
            self._set_readout("")
            return
        x_frac = (pos.x() - rect.left()) / rect.width()
        y_frac = (rect.bottom() - pos.y()) / rect.height()
        readout = readout_at(self._data, self._window, self._coverage, x_frac, y_frac)
        self._set_readout(format_readout(readout, self._data))

    def _set_readout(self, text: str) -> None:
        if text != self._readout_text:
            self._readout_text = text
            self.readout_changed.emit(text)

    def _note(self, name: str, started: float) -> None:
        ms = (time.perf_counter() - started) * 1000.0
        self.timings[name] = ms
        if ms > self.max_timings.get(name, 0.0):
            self.max_timings[name] = ms

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802
        started = time.perf_counter()
        painter = QPainter(self)
        try:
            if self._data is None or self._window is None or self._coverage is None:
                painter.fillRect(self.rect(), QColor(current_palette().surface))
                return
            key = (self.width(), self.height(), round(self.devicePixelRatioF(), 3), self._window.t0, self._window.t1, id(self._data), current_palette().surface)
            if self._scene is None or self._scene[0] != key:
                render_started = time.perf_counter()
                self._scene = (key, self._render_scene())
                self._note("render", render_started)
            painter.drawPixmap(0, 0, self._scene[1])
            self._draw_crosshair(painter)
        finally:
            painter.end()
            self._note("paint", started)

    def _render_scene(self) -> QPixmap:
        data, window, coverage = self._data, self._window, self._coverage
        assert data is not None and window is not None and coverage is not None
        palette = current_palette()
        dpr = self.devicePixelRatioF()
        width, height = max(self.width(), 1), max(self.height(), 1)
        pixmap = QPixmap(int(width * dpr), int(height * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QColor(palette.surface))
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            font = QFont(self.font())
            font.setPointSizeF(max(self.font().pointSizeF() - 1.0, 7.0))
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            text_color = QColor(palette.text)
            muted = QColor(palette.muted)
            line_h = metrics.height()

            # Layout: time labels on the left, frequency labels and the axis title at the bottom,
            # colour bar with its two values on the right.
            scale, unit = time_unit(window.span)
            t_ticks = ticks(window.t0 * scale, window.t1 * scale, max_ticks=5)
            t_step = nice_step(window.span * scale, 5)
            t_labels = [f"{tick_label(v, t_step)} {unit}" for v in t_ticks]
            left = max((metrics.horizontalAdvance(t) for t in t_labels), default=30.0) + 10.0
            bottom = line_h * 2 + 10.0
            bar_w = 10.0
            bar_labels = (f"{data.vmax:.0f}", f"{data.vmin:.0f}", "dBFS")
            right = bar_w + 8.0 + max(metrics.horizontalAdvance(t) for t in bar_labels) + 6.0
            top = 8.0
            plot = QRectF(left, top, max(width - left - right, 10.0), max(height - top - bottom, 10.0))
            self._plot_rect = plot

            e_lo, e_hi = coverage.extent
            e_span = max(e_hi - e_lo, 1e-12)

            def x_of(freq: float) -> float:
                return plot.left() + (freq - e_lo) / e_span * plot.width()

            # Not-recorded parts of the requested range: grey with hatching.
            painter.fillRect(plot, QColor(palette.bg))
            if coverage.missing:
                grey = QColor(muted)
                grey.setAlpha(70)
                hatch = QColor(text_color)
                hatch.setAlpha(110)
                for a, b in coverage.missing:
                    gap = QRectF(x_of(a), plot.top(), max(x_of(b) - x_of(a), 1.0), plot.height())
                    painter.fillRect(gap, grey)
                    painter.fillRect(gap, QBrush(hatch, Qt.BrushStyle.BDiagPattern))

            # The picture: the visible time window of the file's own span.
            if self._image is not None and data.duration_s > 0:
                img_h = float(self._image.height())
                src_top = (data.duration_s - window.t1) / data.duration_s * img_h
                src_bottom = (data.duration_s - window.t0) / data.duration_s * img_h
                source = QRectF(0.0, src_top, float(self._image.width()), max(src_bottom - src_top, 1e-6))
                target = QRectF(x_of(data.freq_lo), plot.top(), x_of(data.freq_hi) - x_of(data.freq_lo), plot.height())
                painter.save()
                painter.setClipRect(plot)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, source.height() > target.height() or source.width() > target.width())
                painter.drawImage(target, self._image, source)
                painter.restore()

            # Requested start / stop markers.
            painter.save()
            painter.setClipRect(plot)
            marker_pen = QPen(QColor("white"))
            marker_pen.setStyle(Qt.PenStyle.DashLine)
            marker_pen.setWidthF(1.0)
            for index, (x_mhz, text) in enumerate(data.markers):
                x = x_of(x_mhz)
                if x < plot.left() - 1 or x > plot.right() + 1:
                    continue
                painter.setPen(marker_pen)
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                label_w = metrics.horizontalAdvance(text) + 8.0
                label_x = x + 4.0 if index == 0 or x + label_w < plot.right() else x - label_w - 4.0
                label_y = plot.top() + 4.0 + index * (line_h + 2.0)
                box = QRectF(label_x, label_y, label_w, line_h + 2.0)
                painter.fillRect(box, QColor(0, 0, 0, 150))
                painter.setPen(QColor("white"))
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()

            # Zoom badge.
            if window.zoomed:
                badge = f"x{window.zoom_factor:.1f}  {format_offset(window.t0)} - {format_offset(window.t1)}"
                bw = metrics.horizontalAdvance(badge) + 10.0
                box = QRectF(plot.right() - bw - 4.0, plot.bottom() - line_h - 6.0, bw, line_h + 2.0)
                painter.fillRect(box, QColor(0, 0, 0, 150))
                painter.setPen(QColor("white"))
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, badge)

            # Frame and axes.
            axis_pen = QPen(QColor(palette.border))
            axis_pen.setWidthF(1.0)
            painter.setPen(axis_pen)
            painter.drawRect(plot)
            painter.setPen(text_color)
            f_ticks = ticks(e_lo, e_hi, max_ticks=max(3, int(plot.width() // 90)))
            f_step = nice_step(e_hi - e_lo, max(3, int(plot.width() // 90)))
            last_right = -1e9
            for value in f_ticks:
                x = x_of(value)
                if x < plot.left() - 0.5 or x > plot.right() + 0.5:
                    continue
                painter.setPen(axis_pen)
                painter.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 4.0))
                label = tick_label(value, f_step)
                w = metrics.horizontalAdvance(label)
                if x - w / 2 > last_right + 6.0:
                    painter.setPen(text_color)
                    painter.drawText(QRectF(x - w / 2, plot.bottom() + 5.0, w, line_h), Qt.AlignmentFlag.AlignCenter, label)
                    last_right = x + w / 2
            painter.setPen(muted)
            painter.drawText(QRectF(plot.left(), plot.bottom() + 5.0 + line_h, plot.width(), line_h), Qt.AlignmentFlag.AlignCenter, data.x_label)
            for value, label in zip(t_ticks, t_labels):
                t = value / scale
                y = plot.bottom() - window.frac_of(t) * plot.height()
                if y < plot.top() - 0.5 or y > plot.bottom() + 0.5:
                    continue
                painter.setPen(axis_pen)
                painter.drawLine(QPointF(plot.left() - 4.0, y), QPointF(plot.left(), y))
                painter.setPen(text_color)
                painter.drawText(QRectF(0.0, y - line_h / 2, plot.left() - 6.0, line_h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

            # Colour bar.
            if self._bar_image is not None:
                bar = QRectF(plot.right() + 8.0, plot.top(), bar_w, plot.height())
                painter.drawImage(bar, self._bar_image, QRectF(0.0, 0.0, 1.0, float(self._bar_image.height())))
                painter.setPen(axis_pen)
                painter.drawRect(bar)
                painter.setPen(text_color)
                painter.drawText(QRectF(bar.right() + 4.0, bar.top() - line_h / 2, right, line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, bar_labels[0])
                painter.drawText(QRectF(bar.right() + 4.0, bar.bottom() - line_h / 2, right, line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, bar_labels[1])
                painter.setPen(muted)
                painter.drawText(QRectF(bar.right() + 4.0, bar.center().y() - line_h / 2, right, line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, bar_labels[2])
        finally:
            painter.end()
        return pixmap

    def _draw_crosshair(self, painter: QPainter) -> None:
        if self._cursor is None or self._data is None:
            return
        rect = self._plot_rect
        pos = self._cursor
        if not rect.contains(pos):
            return
        pen = QPen(QColor(255, 255, 255, 190))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(rect.left(), pos.y()), QPointF(rect.right(), pos.y()))
        painter.drawLine(QPointF(pos.x(), rect.top()), QPointF(pos.x(), rect.bottom()))


__all__ = [
    "IDLE_READOUT",
    "MAX_ZOOM",
    "ZOOM_STEP",
    "Coverage",
    "Readout",
    "TimeWindow",
    "WaterfallView",
    "coverage_of",
    "format_capture_start",
    "format_offset",
    "format_readout",
    "nice_step",
    "readout_at",
    "tick_label",
    "ticks",
    "time_unit",
]
