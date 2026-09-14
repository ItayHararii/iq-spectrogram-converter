"""Small dialog for HTTP, SFTP, and download-folder settings."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .connection_state import ConnectionState
from .constants import DEFAULT_SFTP_PORT, DEFAULT_SFTP_USERNAME
from .paths import default_download_dir
from .theme import apply_combo_popup_palette, fit_window_to_screen

__all__ = ["ConnectionDialog", "apply_combo_popup_palette"]


class ConnectionDialog(QDialog):
    test_requested = Signal(object)

    def __init__(self, state: ConnectionState, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setSizeGripEnabled(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._state = state

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        inner = QWidget()
        form_l = QVBoxLayout(inner)
        form_l.setContentsMargins(4, 4, 12, 8)
        form_l.setSpacing(14)

        form_l.addWidget(self._section("Sensor"))
        http = QFormLayout()
        http.setHorizontalSpacing(14)
        http.setVerticalSpacing(8)
        http.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.host_edit = QLineEdit(state.host)
        self.host_edit.setPlaceholderText("Sensor IP or hostname")
        self.port_edit = QLineEdit(state.http_port)
        self.port_edit.setPlaceholderText("80")
        self.https_check = QCheckBox("Use HTTPS")
        self.https_check.setChecked(state.use_https)
        self.user_edit = QLineEdit(state.username)
        self.pass_edit = QLineEdit(state.http_password)
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_http = QCheckBox("Show")
        self.show_http.toggled.connect(
            lambda on: self.pass_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        pass_row = QHBoxLayout()
        pass_row.setContentsMargins(0, 0, 0, 0)
        pass_row.addWidget(self.pass_edit, 1)
        pass_row.addWidget(self.show_http)
        self.timeout_edit = QLineEdit(state.timeout or "30")
        http.addRow("IP / hostname", self.host_edit)
        http.addRow("HTTP port", self.port_edit)
        http.addRow("HTTPS", self.https_check)
        http.addRow("HTTP username", self.user_edit)
        http.addRow("HTTP password", pass_row)
        http.addRow("Timeout (s)", self.timeout_edit)
        form_l.addLayout(http)

        form_l.addWidget(self._section("Files on the sensor"))
        note = QLabel("SFTP browses recordings on the sensor. SSH login differs from HTTP.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        form_l.addWidget(note)
        sftp = QFormLayout()
        sftp.setHorizontalSpacing(14)
        sftp.setVerticalSpacing(8)
        self.sftp_port = QLineEdit(state.sftp_port or str(DEFAULT_SFTP_PORT))
        self.sftp_port.setPlaceholderText("22")
        self.sftp_user = QLineEdit(state.sftp_username or DEFAULT_SFTP_USERNAME)
        self.sftp_pass = QLineEdit(state.sftp_password)
        self.sftp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        sftp.addRow("SFTP port", self.sftp_port)
        sftp.addRow("SFTP username", self.sftp_user)
        sftp.addRow("SFTP password", self.sftp_pass)
        form_l.addLayout(sftp)

        form_l.addWidget(self._section("This computer"))
        local = QFormLayout()
        local.setHorizontalSpacing(14)
        local.setVerticalSpacing(8)
        self.download_edit = QLineEdit(state.download_dir.strip() or str(default_download_dir()))
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse_download)
        dl_row = QHBoxLayout()
        dl_row.setContentsMargins(0, 0, 0, 0)
        dl_row.addWidget(self.download_edit, 1)
        dl_row.addWidget(browse)
        local.addRow("Download folder", dl_row)
        form_l.addLayout(local)

        hint = QLabel("Passwords are not saved.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form_l.addWidget(hint)
        form_l.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setWidget(inner)
        layout.addWidget(self.scroll, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        test_btn = self.buttons.addButton("Test HTTP connection", QDialogButtonBox.ButtonRole.ActionRole)
        test_btn.clicked.connect(lambda: self.test_requested.emit(self.result_state()))
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setObjectName("accent")
            ok.setDefault(True)
            ok.setText("Save")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        fit_window_to_screen(self, 560, 620, min_width=420, min_height=320)

    def _section(self, title: str) -> QLabel:
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        return label

    def _browse_download(self) -> None:
        start = self.download_edit.text().strip() or str(default_download_dir())
        chosen = QFileDialog.getExistingDirectory(self, "Download folder", start)
        if chosen:
            self.download_edit.setText(chosen)

    def result_state(self) -> ConnectionState:
        return replace(
            self._state,
            host=self.host_edit.text().strip(),
            http_port=self.port_edit.text().strip(),
            use_https=self.https_check.isChecked(),
            username=self.user_edit.text().strip(),
            http_password=self.pass_edit.text(),
            timeout=self.timeout_edit.text().strip() or "30",
            sftp_port=self.sftp_port.text().strip() or str(DEFAULT_SFTP_PORT),
            sftp_username=self.sftp_user.text().strip() or DEFAULT_SFTP_USERNAME,
            sftp_password=self.sftp_pass.text(),
            sftp_same_as_http=False,
            download_dir=self.download_edit.text().strip() or str(default_download_dir()),
        )
