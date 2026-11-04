"""The last recording of a workflow, as a compact three-line summary.

    LAST RECORDING  Row 19 of 20 · 3700-3800 MHz                [Complete]
    1 file · 0.503 s · 251.4 MB
    Recording complete · Download saved · Excel updated
    [Open folder]

The requested, received and recorded values stay on the ResultView for
validation and the diagnostic log; only the facts line, the states line and a
short coverage note (when the sensor reported another range) are shown here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .result_view import ResultView
from .theme import restyle


class ResultCard(QFrame):
    open_folder_clicked = Signal()
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

        self.facts = QLabel("")
        self.facts.setToolTip("Files, measured duration and size read from the downloaded or listed files; \"unknown\" when not read.")
        self.coverage = QLabel("")
        self.coverage.setObjectName("warn")
        self.coverage.setToolTip("The range CRFS stored in the file differs from the requested range in the title.")
        self.states = QLabel("")
        self.states.setObjectName("muted")
        self.states.setToolTip("Recording, download and Excel are reported separately; a slow download never means the recording is still running.")
        for label in (self.facts, self.coverage, self.states):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        self.coverage.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.download_btn = self._button("Download", "Copy this recording's files from the sensor into the download folder")
        self.download_btn.clicked.connect(self.download_clicked)
        self.download_btn.setVisible(False)
        self.open_btn = self._button("Open folder", "Show the downloaded file in its folder")
        self.open_btn.clicked.connect(self.open_folder_clicked)
        buttons.addWidget(self.download_btn)
        buttons.addWidget(self.open_btn)
        buttons.addStretch(1)
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
        self.facts.setText(view.facts_text)
        self.facts.setVisible(bool(view.facts_text))
        self.coverage.setText(view.coverage_note)
        self.coverage.setVisible(bool(view.coverage_note))
        self.states.setText(view.states_text)
        self.states.setVisible(bool(view.states_text))
        ready = view.local_ready
        if ready:
            tip = "Show the downloaded file in its folder"
        elif not view.remote_paths:
            tip = "No files were received."
        elif self._downloading:
            tip = "Available when the download has finished."
        else:
            tip = "Available when a verified local copy exists."
        self.open_btn.setEnabled(ready)
        self.open_btn.setToolTip(tip)
        can_download = self._downloadable and bool(view.remote_paths) and not ready
        self.download_btn.setVisible(can_download)
        self.download_btn.setEnabled(can_download and not self._downloading)
        self.setVisible(True)


__all__ = ["ResultCard"]
