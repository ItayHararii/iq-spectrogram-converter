"""The last recording of a workflow: requested next to received, its states, and its files."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .result_view import DOWNLOAD_REQUIRED, ResultView
from .theme import restyle


class ResultCard(QFrame):
    open_folder_clicked = Signal()
    waterfall_clicked = Signal()
    download_clicked = Signal()

    def __init__(self, heading: str = "Last recording", *, downloadable: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("resultCard")
        self._downloadable = downloadable
        self._downloading = False
        self.view: ResultView | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.heading = QLabel(heading.upper())
        self.heading.setObjectName("resultHeading")
        self.title = QLabel("")
        self.title.setObjectName("resultTitle")
        self.title.setWordWrap(True)
        self.tag = QLabel("")
        self.tag.setObjectName("badgeIdle")
        self.tag.setVisible(False)
        head.addWidget(self.heading)
        head.addWidget(self.title, 1)
        head.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self.requested = QLabel("")
        self.received = QLabel("")
        self.recorded = QLabel("")
        self.states = QLabel("")
        self.states.setObjectName("muted")
        for label in (self.requested, self.received, self.recorded, self.states):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.download_btn = self._button("Download", "Copy this recording's files from the sensor into the download folder")
        self.download_btn.clicked.connect(self.download_clicked)
        self.download_btn.setVisible(False)
        self.open_btn = self._button("Open folder", "Show the file in its folder")
        self.open_btn.clicked.connect(self.open_folder_clicked)
        self.waterfall_btn = self._button("View waterfall", "Show the waterfall of the downloaded file")
        self.waterfall_btn.clicked.connect(self.waterfall_clicked)
        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        buttons.addWidget(self.download_btn)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.waterfall_btn)
        buttons.addWidget(self.hint, 1)
        buttons.addStretch(0)  # takes the width while the hint is hidden
        layout.addLayout(buttons)
        self.setVisible(False)

    @staticmethod
    def _button(text: str, tooltip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("ghost")
        button.setAutoDefault(False)
        button.setToolTip(tooltip)
        return button

    def set_downloading(self, busy: bool, text: str = "") -> None:
        self._downloading = bool(busy)
        if self.view is not None:
            if busy:
                self.view.transfer_state = text or "Downloading"
            self.set_view(self.view)

    def set_view(self, view: ResultView | None) -> None:
        self.view = view
        if view is None:
            self.setVisible(False)
            return
        self.title.setText(view.title)
        self.tag.setText(view.tag)
        self.tag.setObjectName(view.tag_style)
        self.tag.setVisible(bool(view.tag))
        restyle(self.tag)
        self.requested.setText(view.requested)
        self.received.setText(view.received)
        self.recorded.setText(view.recorded)
        self.recorded.setVisible(bool(view.remote_paths))
        self.states.setText(view.states_text)
        self.states.setVisible(bool(view.states_text))
        ready = view.local_ready
        if ready:
            reason = ""
        elif not view.remote_paths:
            reason = "No files were received."
        elif self._downloading:
            reason = "Downloading…"
        else:
            reason = DOWNLOAD_REQUIRED
        for button, tip in (
            (self.open_btn, "Show the file in its folder"),
            (self.waterfall_btn, "Show the waterfall of the downloaded file"),
        ):
            button.setEnabled(ready)
            button.setToolTip(tip if ready else reason)
        self.hint.setText(reason)
        self.hint.setVisible(bool(reason))
        can_download = self._downloadable and bool(view.remote_paths) and not ready
        self.download_btn.setVisible(can_download)
        self.download_btn.setEnabled(can_download and not self._downloading)
        self.setVisible(True)


__all__ = ["ResultCard"]
