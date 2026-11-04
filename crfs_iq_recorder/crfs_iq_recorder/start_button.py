"""Buttons that start recordings: only a deliberate click or key press presses them."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

_ACTIVATE_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Select)
_ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
_TAB_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
# How long after a mouse release / key press a click still counts as caused by it.
_INPUT_WINDOW_S = 1.0


class StartButton(QPushButton):
    """A Start that nothing but the operator presses.

    It is never a default button (Enter anywhere does not press it) and a mouse
    click does not leave the keyboard focus on it (a later Space does not start
    again). Space presses it only after the operator moved the focus onto it
    with Tab; focus that lands on it because another control was disabled or
    hidden, or that comes back when the window is activated again, does not arm it.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._tab_focus_in = False
        self._armed = False
        self._input = ""
        self._input_at = 0.0
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)

    @property
    def keyboard_armed(self) -> bool:
        return self._armed

    def take_trigger(self) -> str:
        """How the latest click came in: mouse, keyboard, or program (code or accessibility)."""
        recent = self._input and time.monotonic() - self._input_at <= _INPUT_WINDOW_S
        trigger = self._input if recent else "program"
        self._input = ""
        return trigger

    def _note_input(self, kind: str) -> None:
        self._input = kind
        self._input_at = time.monotonic()

    def _on_focus_changed(self, old, now) -> None:
        if now is self:
            moved_by_operator = old is not None and old.isEnabled() and old.isVisible()
            self._armed = self._tab_focus_in and moved_by_operator
        elif old is self:
            self._armed = False

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._tab_focus_in = event.reason() in _TAB_REASONS
        self._armed = False
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._tab_focus_in = False
        self._armed = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in _ENTER_KEYS or (key in _ACTIVATE_KEYS and not self._armed):
            event.ignore()
            return
        if key in _ACTIVATE_KEYS and not event.isAutoRepeat():
            self._note_input("keyboard")
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() in _ACTIVATE_KEYS and not self._armed:
            event.ignore()
            return
        super().keyReleaseEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._note_input("mouse")
        super().mouseReleaseEvent(event)
