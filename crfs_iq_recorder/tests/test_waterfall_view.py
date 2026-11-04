"""Interactive waterfall (Sensor Files preview): view mapping, coverage, readout and the widget."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from crfs_iq_recorder.spectrogram_preview import WaterfallData
from crfs_iq_recorder.waterfall_view import (
    IDLE_READOUT,
    MAX_ZOOM,
    MIN_VISIBLE_BINS,
    ZOOM_STEP,
    TimeWindow,
    coverage_of,
    format_capture_start,
    format_offset,
    format_readout,
    nice_step,
    readout_at,
    tick_label,
    ticks,
    time_unit,
)

START = datetime(2026, 11, 4, 22, 48, 50, 1000, tzinfo=timezone.utc)


def make_data(
    *,
    n_freq: int = 64,
    n_time: int = 200,
    duration_s: float = 1.0,
    absolute: bool = True,
    freq_lo: float = 700.0,
    freq_hi: float = 716.0,
    requested: tuple[float, float] | None = (703.0, 713.0),
    capture_start: datetime | None = START,
) -> WaterfallData:
    # Column index encodes time, row index encodes frequency: power = -100 + row + time_bin/10.
    power = (-100.0 + np.arange(n_freq)[:, None] + np.arange(n_time)[None, :] / 10.0).astype(np.float32)
    rgb = np.zeros((n_time, n_freq, 3), dtype=np.uint8)
    bar = np.zeros((256, 3), dtype=np.uint8)
    return WaterfallData(
        power_db=power,
        rgb=rgb,
        colorbar_rgb=bar,
        vmin=-100.0,
        vmax=-20.0,
        duration_s=duration_s,
        sample_rate_hz=16e6,
        absolute=absolute,
        freq_lo=freq_lo,
        freq_hi=freq_hi,
        freq_unit="MHz" if absolute else "kHz",
        x_label="Frequency (MHz)" if absolute else "Offset from centre (kHz)",
        capture_start=capture_start,
        covered_mhz=(freq_lo, freq_hi) if absolute else None,
        requested_mhz=requested if absolute else None,
        markers=(),
        legend_lines=(),
        peak_dbfs=-20.0,
        whole_file_read=False,
    )


# ----------------------------------------------------------------------------- TimeWindow
def test_full_window_spans_the_whole_sample_and_is_not_zoomed():
    window = TimeWindow.full(2.0, 1000)
    assert (window.t0, window.t1) == (0.0, 2.0)
    assert not window.zoomed
    assert window.zoom_factor == 1.0
    assert window.min_span_s == pytest.approx(2.0 / MAX_ZOOM)


def test_zoom_keeps_the_anchor_time_fixed():
    window = TimeWindow.full(1.0, 1000)
    anchor_time = window.time_at(0.25)
    zoomed = window.zoomed_at(ZOOM_STEP, 0.25)
    assert zoomed.span == pytest.approx(1.0 / ZOOM_STEP)
    assert zoomed.time_at(0.25) == pytest.approx(anchor_time)
    assert zoomed.zoomed
    again = zoomed.zoomed_at(ZOOM_STEP, 0.25)
    assert again.time_at(0.25) == pytest.approx(anchor_time)
    assert again.zoom_factor == pytest.approx(ZOOM_STEP**2)


def test_zoom_in_is_bounded_by_max_zoom_and_minimum_bins():
    window = TimeWindow.full(1.0, 1000)
    for _ in range(200):
        window = window.zoomed_at(ZOOM_STEP, 0.5)
    assert window.span == pytest.approx(1.0 / MAX_ZOOM)
    assert 0.0 <= window.t0 < window.t1 <= 1.0
    # Few time columns: the minimum span is a handful of columns, not duration / MAX_ZOOM.
    coarse = TimeWindow.full(1.0, 16)
    for _ in range(50):
        coarse = coarse.zoomed_at(ZOOM_STEP, 0.0)
    assert coarse.span == pytest.approx(MIN_VISIBLE_BINS / 16)
    assert coarse.t0 == 0.0


def test_zoom_out_never_exceeds_the_sample_and_restores_the_full_view():
    window = TimeWindow.full(1.0, 1000).zoomed_at(4.0, 0.9)
    assert window.zoomed
    out = window.zoomed_at(1.0 / 100.0, 0.1)
    assert (out.t0, out.t1) == (0.0, 1.0)
    assert not out.zoomed
    assert window.reset() == out


def test_zoom_at_the_edges_clamps_the_window_inside_the_sample():
    window = TimeWindow.full(1.0, 1000)
    top = window.zoomed_at(2.0, 1.0)
    assert top.t1 == pytest.approx(1.0)
    assert top.t0 == pytest.approx(0.5)
    bottom = window.zoomed_at(2.0, 0.0)
    assert bottom.t0 == 0.0
    assert bottom.t1 == pytest.approx(0.5)


def test_pan_is_clamped_to_the_sample():
    window = TimeWindow.full(1.0, 1000).zoomed_at(4.0, 0.5)  # 0.375 .. 0.625
    assert window.panned(0.1).t0 == pytest.approx(0.475)
    assert window.panned(5.0).t1 == pytest.approx(1.0)
    assert window.panned(-5.0).t0 == 0.0
    assert window.panned(5.0).span == pytest.approx(window.span)
    full = TimeWindow.full(1.0, 1000)
    assert full.panned(0.3) == full  # nothing to pan when the whole sample is visible


def test_time_mapping_round_trips():
    window = TimeWindow.full(3.0, 300).zoomed_at(3.0, 0.5)  # 1.0 .. 2.0
    assert window.time_at(0.0) == pytest.approx(1.0)
    assert window.time_at(1.0) == pytest.approx(2.0)
    assert window.frac_of(1.25) == pytest.approx(0.25)
    assert window.time_at(1.7) == pytest.approx(2.0)  # fractions are clamped


def test_empty_sample_is_inert():
    window = TimeWindow.full(0.0, 0)
    assert window.zoomed_at(2.0, 0.5) == window
    assert window.panned(1.0) == window
    assert not window.zoomed


# ----------------------------------------------------------------------------- coverage
def test_coverage_partial_lists_both_missing_sides():
    cov = coverage_of(True, 700.1875, 715.8125, (691.1875, 720.8125))
    assert cov.extent == (691.1875, 720.8125)
    assert cov.covered == (700.1875, 715.8125)
    assert cov.missing == ((691.1875, 700.1875), (715.8125, 720.8125))
    assert cov.partial
    assert cov.note == (
        "File covers 700.1875-715.8125 MHz of the requested 691.1875-720.8125 MHz; "
        "hatched 691.1875-700.1875 MHz and 715.8125-720.8125 MHz not recorded."
    )


def test_coverage_partial_one_side_and_reversed_request():
    cov = coverage_of(True, 700.0, 716.0, (720.0, 710.0))
    assert cov.requested == (710.0, 720.0)
    assert cov.missing == ((716.0, 720.0),)
    assert cov.extent == (700.0, 720.0)
    assert "hatched 716-720 MHz not recorded" in cov.note


def test_coverage_inside_has_no_hatching():
    cov = coverage_of(True, 700.0, 716.0, (703.0, 713.0))
    assert cov.missing == ()
    assert not cov.partial
    assert cov.extent == (700.0, 716.0)
    assert cov.note == "File covers 700-716 MHz; the requested 703-713 MHz lies inside it."


def test_coverage_disjoint_request_is_entirely_hatched():
    cov = coverage_of(True, 700.0, 716.0, (758.0, 768.0))
    assert cov.missing == ((758.0, 768.0),)
    assert cov.extent == (700.0, 768.0)
    assert cov.note.startswith("File covers 700-716 MHz, none of the requested 758-768 MHz")


def test_coverage_without_request_or_centre():
    cov = coverage_of(True, 700.0, 716.0, None)
    assert cov.missing == () and cov.requested is None
    assert cov.note == "File covers 700-716 MHz; no requested range to compare."
    rel = coverage_of(False, -8000.0, 8000.0, (703.0, 713.0))
    assert rel.covered is None and rel.requested is None and rel.missing == ()
    assert rel.extent == (-8000.0, 8000.0)
    assert "cannot be placed" in rel.note


# ----------------------------------------------------------------------------- readout
def test_readout_values_time_frequency_and_level():
    data = make_data()
    window = TimeWindow.full(data.duration_s, data.n_time)
    cov = coverage_of(True, data.freq_lo, data.freq_hi, data.requested_mhz)
    # Cursor at the horizontal centre (708 MHz) and 30 % up from the bottom (0.3 s).
    r = readout_at(data, window, cov, 0.5, 0.3)
    assert r.freq == pytest.approx(708.0)
    assert r.time_s == pytest.approx(0.3)
    assert r.inside_file
    # Row 32 of 64 (708 MHz is halfway through 700..716), time bin 60 of 200.
    assert r.level_dbfs == pytest.approx(-100.0 + 32 + 60 / 10.0)
    assert r.absolute_time == (START + timedelta(seconds=0.3)).astimezone()
    text = format_readout(r, data)
    expected_clock = (START + timedelta(seconds=0.3)).astimezone()
    assert text.startswith(f"{expected_clock:%H:%M:%S}.{expected_clock.microsecond:06d} local (+300.000 ms)")
    assert "708.000 MHz" in text and "-62.0 dBFS/bin" in text


def test_readout_follows_the_zoomed_window():
    data = make_data()
    window = TimeWindow.full(data.duration_s, data.n_time).zoomed_at(4.0, 1.0)  # 0.75 .. 1.0
    cov = coverage_of(True, data.freq_lo, data.freq_hi, data.requested_mhz)
    r = readout_at(data, window, cov, 0.0, 0.0)
    assert r.time_s == pytest.approx(0.75)
    assert r.freq == pytest.approx(700.0)
    assert r.level_dbfs == pytest.approx(-100.0 + 0 + 150 / 10.0)


def test_readout_outside_the_file_has_no_level():
    data = make_data()
    window = TimeWindow.full(data.duration_s, data.n_time)
    cov = coverage_of(True, data.freq_lo, data.freq_hi, (690.0, 720.0))
    assert cov.extent == (690.0, 720.0)
    r = readout_at(data, window, cov, 0.0, 0.5)  # 690 MHz: requested but not recorded
    assert r.freq == pytest.approx(690.0)
    assert not r.inside_file
    assert r.level_dbfs is None
    assert format_readout(r, data).endswith("not recorded")


def test_readout_without_capture_start_or_centre():
    data = make_data(capture_start=None, absolute=False, freq_lo=-8000.0, freq_hi=8000.0, requested=None)
    window = TimeWindow.full(data.duration_s, data.n_time)
    cov = coverage_of(False, data.freq_lo, data.freq_hi, None)
    r = readout_at(data, window, cov, 0.75, 0.001)
    assert r.absolute_time is None
    text = format_readout(r, data)
    assert text.startswith("+1.000 ms (start time unknown)")
    assert "+4000.000 kHz" in text


def test_format_helpers():
    assert format_offset(0.0000125) == "12.5 us"
    assert format_offset(0.00321) == "3.210 ms"
    assert format_offset(2.5) == "2.500 s"
    assert time_unit(0.0005) == (1e6, "us")
    assert time_unit(0.5) == (1e3, "ms")
    assert time_unit(5.0) == (1.0, "s")
    local = START.astimezone()
    assert format_capture_start(START) == f"start {local:%d %b %Y %H:%M:%S}.001 local (file metadata)"
    assert "unknown" in format_capture_start(None)


def test_ticks_are_round_numbers_inside_the_range():
    assert nice_step(16.0, 6) == 5.0
    assert nice_step(0.0084, 5) == pytest.approx(0.002)
    values = ticks(691.1875, 720.8125, 6)
    assert values == [695.0, 700.0, 705.0, 710.0, 715.0, 720.0]
    assert all(691.1875 <= v <= 720.8125 for v in values)
    assert tick_label(705.0, 5.0) == "705"
    assert tick_label(0.0025, 0.0005) == "0.0025"
    assert ticks(1.0, 1.0) == [1.0]


# ----------------------------------------------------------------------------- widget
@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wheel(view, x: float, y: float, notches: int) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    pos = QPointF(x, y)
    event = QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view, event)


def _mouse(view, kind: str, x: float, y: float, buttons=None) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    types = {
        "press": QEvent.Type.MouseButtonPress,
        "move": QEvent.Type.MouseMove,
        "release": QEvent.Type.MouseButtonRelease,
    }
    button = Qt.MouseButton.LeftButton if kind != "move" else Qt.MouseButton.NoButton
    held = buttons if buttons is not None else (Qt.MouseButton.LeftButton if kind == "press" else Qt.MouseButton.NoButton)
    pos = QPointF(x, y)
    event = QMouseEvent(types[kind], pos, pos, pos, button, held, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, event)


def _render(view, qapp) -> None:
    view.grab()  # forces a paint, which lays out the plot rectangle
    qapp.processEvents()


def test_widget_wheel_zooms_at_the_cursor_and_reset_restores(qapp):
    from PySide6.QtCore import Qt

    from crfs_iq_recorder.waterfall_view import WaterfallView

    view = WaterfallView()
    view.resize(900, 300)
    changes: list[int] = []
    view.view_changed.connect(lambda: changes.append(1))
    view.set_data(make_data(duration_s=0.5, n_time=400))
    _render(view, qapp)
    rect = view.plot_rect()
    assert rect.width() > 300 and rect.height() > 100
    assert not view.zoomed
    # Zoom at 25 % up from the bottom: that time stays under the cursor.
    y = rect.bottom() - rect.height() * 0.25
    anchor_before = view.window.time_at(0.25)
    _wheel(view, rect.center().x(), y, 1)
    assert view.zoomed
    assert view.window.span == pytest.approx(0.5 / ZOOM_STEP)
    assert view.window.time_at(0.25) == pytest.approx(anchor_before)
    for _ in range(100):
        _wheel(view, rect.center().x(), y, 1)
    assert view.window.span == pytest.approx(0.5 / MAX_ZOOM)
    assert view.timings["wheel"] > 0 and view.max_timings["wheel"] >= view.timings["wheel"]
    # Wheel down (zoom out) far enough returns to the full view.
    _wheel(view, rect.center().x(), y, -40)
    assert not view.zoomed
    _wheel(view, rect.center().x(), y, 3)
    assert view.zoomed
    view.reset_view()
    assert not view.zoomed and (view.window.t0, view.window.t1) == (0.0, 0.5)
    _wheel(view, rect.center().x(), y, 3)
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Home, Qt.KeyboardModifier.NoModifier))
    assert not view.zoomed
    assert len(changes) >= 5


def test_widget_drag_pans_and_is_clamped(qapp):
    from PySide6.QtCore import Qt

    from crfs_iq_recorder.waterfall_view import WaterfallView

    view = WaterfallView()
    view.resize(900, 300)
    view.set_data(make_data(duration_s=1.0, n_time=400))
    _render(view, qapp)
    rect = view.plot_rect()
    view.zoom_by(4.0, 0.5)  # 0.375 .. 0.625
    t0 = view.window.t0
    x = rect.center().x()
    y_start = rect.center().y()
    _mouse(view, "press", x, y_start)
    # Dragging down (towards the bottom = earlier) by 10 % of the plot height moves the window by 10 % of its span.
    _mouse(view, "move", x, y_start + rect.height() * 0.1, buttons=Qt.MouseButton.LeftButton)
    assert view.window.t0 == pytest.approx(t0 + 0.1 * 0.25, abs=1e-6)
    assert view.timings["drag"] > 0
    # Keep dragging far beyond the end: clamped to the sample.
    _mouse(view, "move", x, y_start + rect.height() * 5, buttons=Qt.MouseButton.LeftButton)
    assert view.window.t1 == pytest.approx(1.0)
    assert view.window.span == pytest.approx(0.25)
    _mouse(view, "release", x, y_start + rect.height() * 5)
    # Not zoomed: dragging does nothing.
    view.reset_view()
    _mouse(view, "press", x, y_start)
    _mouse(view, "move", x, y_start + 50, buttons=Qt.MouseButton.LeftButton)
    _mouse(view, "release", x, y_start + 50)
    assert (view.window.t0, view.window.t1) == (0.0, 1.0)


def test_widget_readout_signal_and_hatched_coverage(qapp):
    from crfs_iq_recorder.waterfall_view import WaterfallView

    view = WaterfallView()
    view.resize(1000, 320)
    data = make_data(requested=(691.0, 721.0))
    texts: list[str] = []
    view.readout_changed.connect(texts.append)
    view.set_data(data)
    _render(view, qapp)
    assert view.coverage is not None and view.coverage.partial
    assert view.coverage.missing == ((691.0, 700.0), (716.0, 721.0))
    rect = view.plot_rect()
    # Hover over the recorded part: time, frequency and level.
    _mouse(view, "move", rect.center().x(), rect.bottom() - rect.height() * 0.3)
    assert texts and "MHz" in texts[-1] and "dBFS/bin" in texts[-1] and "local (+300.000 ms)" in texts[-1]
    assert view.readout_text() == texts[-1]
    # Hover over the hatched (not recorded) part on the left.
    _mouse(view, "move", rect.left() + 2, rect.center().y())
    assert texts[-1].endswith("not recorded")
    assert view.timings["move"] >= 0
    # Leaving clears the readout.
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(view, QEvent(QEvent.Type.Leave))
    assert view.readout_text() == ""
    assert texts[-1] == ""
    # The scene is painted at the widget's device pixel ratio and the paint timing is recorded.
    pixmap = view.grab()
    assert pixmap.width() == view.width() and pixmap.height() == view.height()
    assert view.timings["paint"] > 0 and view.timings["render"] > 0
    view.clear()
    assert view.data is None and view.window is None and not view.zoomed


def test_widget_large_size_handlers_stay_fast(qapp):
    """A 2000 x 900 px view (what a 200 % display shows): wheel and drag handlers well under a frame."""
    from PySide6.QtCore import Qt

    from crfs_iq_recorder.waterfall_view import WaterfallView

    view = WaterfallView()
    view.resize(2000, 900)
    view.set_data(make_data(n_freq=256, n_time=1024, duration_s=0.0084, requested=(691.0, 721.0)))
    _render(view, qapp)
    rect = view.plot_rect()
    for _ in range(10):
        _wheel(view, rect.center().x(), rect.center().y(), 1)
        view.grab()
    _mouse(view, "press", rect.center().x(), rect.center().y())
    for i in range(10):
        _mouse(view, "move", rect.center().x(), rect.center().y() + i * 5, buttons=Qt.MouseButton.LeftButton)
        view.grab()
    _mouse(view, "release", rect.center().x(), rect.center().y() + 50)
    assert view.max_timings["wheel"] < 16.0
    assert view.max_timings["drag"] < 16.0
    assert view.max_timings["render"] < 50.0  # one cached scene per view change, off the hot path of mouse moves


def test_idle_readout_mentions_the_gestures():
    assert "wheel" in IDLE_READOUT and "drag" in IDLE_READOUT and "Home" in IDLE_READOUT
