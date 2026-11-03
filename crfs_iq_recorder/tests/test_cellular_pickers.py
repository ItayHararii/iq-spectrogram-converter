"""Graphical date / time pickers (offscreen Qt, locale forced per test)."""

from __future__ import annotations

import os
from datetime import date, time, timedelta

import pytest

from crfs_iq_recorder import cellular_pickers as pickers
from crfs_iq_recorder.cellular_pickers import (
    CalendarPopup,
    DateField,
    TimeField,
    TimePickerPopup,
    TimesField,
    format_date,
    format_time,
    locale_uses_12h,
    short_time_format,
)


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


def _force_locale(monkeypatch, *args):
    from PySide6.QtCore import QLocale

    locale = QLocale(*args)
    monkeypatch.setattr(pickers, "system_locale", lambda: locale)
    return locale


@pytest.fixture
def us_locale(monkeypatch):
    from PySide6.QtCore import QLocale

    return _force_locale(monkeypatch, QLocale.Language.English, QLocale.Country.UnitedStates)


@pytest.fixture
def de_locale(monkeypatch):
    return _force_locale(monkeypatch, "de_DE")


class _Spy:
    def __init__(self, signal) -> None:
        self.values: list = []
        signal.connect(self.values.append)


# ----------------------------------------------------------------------------- locale helpers


def test_formats_follow_the_monkeypatched_locale(qapp, monkeypatch):
    from PySide6.QtCore import QLocale

    _force_locale(monkeypatch, QLocale.Language.English, QLocale.Country.UnitedStates)
    assert format_date(date(2026, 11, 2)) == "11/2/26"
    assert format_time(time(16, 5)) == "4:05 PM"
    assert format_time(time(9, 0)).endswith("AM")
    assert locale_uses_12h() is True

    _force_locale(monkeypatch, "de_DE")
    assert format_date(date(2026, 11, 2)) == "02.11.26"
    assert format_time(time(16, 0)) == "16:00"
    assert locale_uses_12h() is False


def test_short_time_format_drops_seconds_and_zone_names():
    assert short_time_format("h:mm:ss Ap") == "h:mm Ap"
    assert short_time_format("HH:mm:ss") == "HH:mm"
    assert short_time_format("HH:mm:ss t") == "HH:mm"
    assert short_time_format("HH:mm:ss.zzz") == "HH:mm"
    assert short_time_format("H:mm 'Uhr'") == "H:mm 'Uhr'"


# ----------------------------------------------------------------------------- calendar


def test_calendar_initial_selection_navigation_and_clicks(qapp, us_locale):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog

    initial = date(2026, 11, 15)
    dialog = CalendarPopup(initial)
    spy = _Spy(dialog.date_changed)
    try:
        assert dialog.date() == initial
        assert dialog.title.text() == "November 2026"
        selected = dialog._day_button(initial)
        assert selected is not None and selected.property("selected") is True
        # Sunday-first grid for en_US: the first visible cell is Sunday 1 Nov 2026.
        assert dialog._cells[0][1] == date(2026, 11, 1)
        assert dialog._day_button(date(2026, 12, 5)).property("outside") is True

        dialog.next_btn.click()
        assert dialog.title.text() == "December 2026"
        assert dialog._day_button(date(2026, 11, 15)) is None
        assert dialog._day_button(date(2026, 12, 25)) is not None
        assert dialog.date() == initial  # navigating does not change the selection

        dialog._day_button(date(2026, 12, 25)).click()
        assert dialog.date() == date(2026, 12, 25)
        assert spy.values == [date(2026, 12, 25)]
        assert dialog._day_button(date(2026, 12, 25)).property("selected") is True

        dialog.prev_btn.click()
        dialog.prev_btn.click()
        assert dialog.title.text() == "October 2026"

        # Keyboard: arrows move the selection and follow it across months; Enter accepts.
        dialog.show()
        dialog.set_date(date(2026, 10, 31))
        QTest.keyClick(dialog, Qt.Key.Key_Right)
        assert dialog.date() == date(2026, 11, 1)
        assert dialog.title.text() == "November 2026"
        QTest.keyClick(dialog, Qt.Key.Key_Down)
        assert dialog.date() == date(2026, 11, 8)
        QTest.keyClick(dialog, Qt.Key.Key_Up)
        QTest.keyClick(dialog, Qt.Key.Key_Left)
        assert dialog.date() == date(2026, 10, 31)
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_calendar_marks_today_and_respects_minimum(qapp, us_locale):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog

    today = date.today()
    dialog = CalendarPopup(today)
    try:
        cell = dialog._day_button(today)
        assert cell.property("today") is True and cell.property("selected") is True
        other = dialog._day_button(today + timedelta(days=1)) or dialog._day_button(today - timedelta(days=1))
        assert other.property("today") is False
    finally:
        dialog.close()

    minimum = date(2026, 11, 10)
    dialog = CalendarPopup(date(2026, 11, 15), minimum=minimum)
    try:
        assert dialog._day_button(date(2026, 11, 9)).isEnabled() is False
        assert dialog._day_button(minimum).isEnabled() is True
        assert dialog.prev_btn.isEnabled() is False  # nothing selectable in October
        dialog.set_date(date(2026, 11, 1))
        assert dialog.date() == minimum  # clamped
        dialog.show()
        QTest.keyClick(dialog, Qt.Key.Key_Left)
        assert dialog.date() == minimum
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        dialog.close()

    # An initial date before the minimum is lifted to the minimum.
    dialog = CalendarPopup(date(2026, 1, 1), minimum=minimum)
    assert dialog.date() == minimum
    dialog.close()


# ----------------------------------------------------------------------------- time picker


def test_time_picker_in_a_12_hour_locale(qapp, us_locale):
    assert locale_uses_12h()
    dialog = TimePickerPopup(time(10, 10))
    spy = _Spy(dialog.time_changed)
    try:
        assert dialog.hours.texts() == [str(h) for h in range(1, 13)]
        assert dialog.minutes.texts() == [f"{m:02d}" for m in range(60)]
        assert dialog.ampm is not None and dialog.ampm.texts() == ["AM", "PM"]
        assert dialog.time() == time(10, 10)
        assert dialog.display.text() == "10:10 AM"
        assert [chip.text() for chip in dialog.chips] == ["9:00 AM", "12:00 PM", "4:00 PM", "6:00 PM"]

        dialog._chip(time(16, 0)).click()
        assert dialog.time() == time(16, 0)
        assert dialog.display.text().upper().endswith("PM")
        assert dialog.hours.texts()[dialog.hours.index()] == "4"
        assert dialog.ampm.index() == 1
        assert spy.values[-1] == time(16, 0)

        dialog._select(12, 30, pm=False)
        assert dialog.time() == time(0, 30)
        dialog._select(12, 0, pm=True)
        assert dialog.time() == time(12, 0)
        dialog.set_time(time(23, 59))
        assert (dialog.hours.index(), dialog.minutes.index(), dialog.ampm.index()) == (10, 59, 1)
        assert dialog.time().second == 0

        # Wheels wrap like a real drum and keep the display in step.
        dialog.hours.step(1)
        assert dialog.time() == time(12, 59)
        assert dialog.display.text() == "12:59 PM"
        dialog.minutes.step(1)
        assert dialog.time() == time(12, 0)
    finally:
        dialog.close()


def test_time_picker_in_a_24_hour_locale(qapp, de_locale):
    assert not locale_uses_12h()
    dialog = TimePickerPopup(time(8, 0))
    spy = _Spy(dialog.time_changed)
    try:
        dialog.show()
        assert dialog.ampm is None
        assert dialog.hours.texts() == [f"{h:02d}" for h in range(24)]
        assert [chip.text() for chip in dialog.chips] == ["09:00", "12:00", "16:00", "18:00"]
        assert dialog.display.text() == "08:00"

        dialog._chip(time(16, 0)).click()
        assert dialog.time() == time(16, 0)
        assert dialog.display.text() == "16:00"
        assert spy.values == [time(16, 0)]
        # The current row is the value's row (padding rows above keep it in the middle when shown).
        assert dialog.hours.currentRow() == pickers._PAD + dialog.hours.index()
        assert dialog.hours.item(dialog.hours.currentRow()).text() == "16"

        dialog.minutes.step(1)
        assert dialog.time() == time(16, 1)
        dialog._select(0, 0)
        assert dialog.time() == time(0, 0)
        dialog.hours.step(-1)
        assert dialog.time() == time(23, 0)
    finally:
        dialog.close()


# ----------------------------------------------------------------------------- fields


def test_times_field_adds_removes_sorts_and_signals(qapp, de_locale):
    field = TimesField([time(12, 0), time(9, 0), time(9, 0, 30)])
    spy = _Spy(field.changed)
    try:
        assert field.times() == [time(9, 0), time(12, 0)]
        assert field.empty_label.isHidden()

        assert field._add(time(9, 0)) is False
        assert spy.values == []
        assert field._add(time(7, 30)) is True
        assert field.times() == [time(7, 30), time(9, 0), time(12, 0)]
        assert spy.values == [[time(7, 30), time(9, 0), time(12, 0)]]
        assert [chip.text_btn.text() for chip in field._chips] == ["07:30", "09:00", "12:00"]

        field._chip(time(9, 0)).remove_btn.click()
        assert field.times() == [time(7, 30), time(12, 0)]
        assert len(spy.values) == 2

        # Editing through a chip replaces that time.
        dialog = field._open_popup(time(7, 30))
        assert dialog.time() == time(7, 30)
        dialog._select(13, 15)
        dialog.accept()
        assert field.times() == [time(12, 0), time(13, 15)]

        # Cancelling the "+ Add time" popup changes nothing.
        dialog = field._open_popup(None)
        dialog._select(5, 0)
        dialog.reject()
        assert field.times() == [time(12, 0), time(13, 15)]

        field.set_times([])
        assert field.times() == []
        assert not field.empty_label.isHidden()
        assert field._chips == []
    finally:
        field.close()

    small = TimesField([], maximum=2)
    assert small._add(time(1, 0)) and small._add(time(2, 0))
    assert small.add_btn.isEnabled() is False
    assert small._add(time(3, 0)) is False
    assert small.times() == [time(1, 0), time(2, 0)]
    small.close()


def test_times_field_does_not_reserve_a_row_per_chip_when_they_fit_on_one(qapp, de_locale):
    """1.11.0 left ~85 px of empty space under the chips: the chip host could not
    shrink below its size hint, which is the stacked height at its minimum width."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from crfs_iq_recorder.theme import CONTROL_H

    host = QWidget()
    layout = QVBoxLayout(host)
    field = TimesField([time(9, 0), time(12, 0), time(16, 0)])
    layout.addWidget(field)
    layout.addStretch(1)
    host.resize(700, 300)
    host.show()
    try:
        qapp.processEvents()
        one_row = CONTROL_H
        assert field.heightForWidth(600) == one_row
        assert field.minimumSizeHint().height() == one_row
        # The parent layout's minimum height follows the one-row figure, not three stacked chips.
        assert host.layout().totalMinimumSize().height() <= one_row + 2 * 12
        assert field.height() <= one_row + 2
        # Narrow hosts still wrap: three rows when only one chip fits per row.
        assert field.flow.heightForWidth(100) > one_row
    finally:
        host.close()


def test_date_field_takes_the_date_chosen_in_the_popup(qapp, us_locale):
    field = DateField(date(2026, 11, 2))
    spy = _Spy(field.changed)
    try:
        assert field.edit.text() == "11/2/26"
        assert field.edit.isReadOnly()
        dialog = field._open_popup()
        assert isinstance(dialog, CalendarPopup)
        assert field._open_popup() is dialog  # a second click re-raises the same popup
        dialog._day_button(date(2026, 11, 20)).click()
        dialog.accept()
        assert field.date() == date(2026, 11, 20)
        assert field.edit.text() == "11/20/26"
        assert spy.values == [date(2026, 11, 20)]

        dialog = field._open_popup()
        dialog._day_button(date(2026, 11, 3)).click()
        dialog.reject()
        assert field.date() == date(2026, 11, 20)

        field.set_minimum(date(2026, 12, 1))
        dialog = field._open_popup()
        assert dialog.date() == date(2026, 12, 1)
        assert dialog._day_button(date(2026, 11, 30)).isEnabled() is False
        dialog.reject()

        field.setEnabled(False)
        assert field.edit.isEnabled() is False and field.button.isEnabled() is False
    finally:
        field.close()

    empty = DateField()
    assert empty.date() is None
    assert empty.edit.text() == ""
    assert empty.edit.placeholderText() == "Pick a date"
    empty.close()


def test_time_field_round_trips_through_the_popup(qapp, de_locale):
    field = TimeField(time(8, 0))
    spy = _Spy(field.changed)
    try:
        assert field.edit.text() == "08:00"
        dialog = field._open_popup()
        assert isinstance(dialog, TimePickerPopup)
        dialog._select(9, 15)
        dialog.accept()
        assert field.time() == time(9, 15)
        assert field.edit.text() == "09:15"
        assert spy.values == [time(9, 15)]
        field.set_time(time(9, 15, 45))
        assert field.time() == time(9, 15)
        assert len(spy.values) == 1
    finally:
        field.close()
