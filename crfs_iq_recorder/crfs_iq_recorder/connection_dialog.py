"""Small dialog for HTTP and SFTP sensor settings."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from .connection_state import ConnectionState
from .constants import DEFAULT_SFTP_PORT, DEFAULT_SFTP_USERNAME


class ConnectionDialog(QDialog):
    test_requested = Signal(object)

    def __init__(self, state: ConnectionState, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(460)
        self._state = state

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        http = QFormLayout()
        http.setHorizontalSpacing(12)
        http.setVerticalSpacing(8)
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
        pass_row.addWidget(self.pass_edit, 1)
        pass_row.addWidget(self.show_http)
        self.timeout_edit = QLineEdit(state.timeout or "30")
        self.demo_check = QCheckBox("Demo mode (no real sensor)")
        self.demo_check.setChecked(state.demo)
        http.addRow("Sensor IP / hostname", self.host_edit)
        http.addRow("HTTP port", self.port_edit)
        http.addRow("", self.https_check)
        http.addRow("HTTP username", self.user_edit)
        http.addRow("HTTP password", pass_row)
        http.addRow("Timeout (s)", self.timeout_edit)
        http.addRow("", self.demo_check)
        layout.addLayout(http)

        note = QLabel("SFTP is used only to browse and download files already on the sensor.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        sftp = QFormLayout()
        sftp.setHorizontalSpacing(12)
        sftp.setVerticalSpacing(8)
        self.sftp_same = QCheckBox("Use HTTP username and password for SFTP")
        self.sftp_same.setChecked(state.sftp_same_as_http)
        self.sftp_port = QLineEdit(state.sftp_port or str(DEFAULT_SFTP_PORT))
        self.sftp_port.setPlaceholderText("22")
        self.sftp_user = QLineEdit(state.sftp_username or DEFAULT_SFTP_USERNAME)
        self.sftp_pass = QLineEdit(state.sftp_password)
        self.sftp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        sftp.addRow(self.sftp_same)
        sftp.addRow("SFTP port", self.sftp_port)
        sftp.addRow("SFTP username", self.sftp_user)
        sftp.addRow("SFTP password", self.sftp_pass)
        layout.addLayout(sftp)
        self.sftp_same.toggled.connect(self._sync_sftp_enabled)
        self._sync_sftp_enabled()

        hint = QLabel(
            "CRFS SSH is a different login from HTTP. Passwords stay in this session only "
            "and are not saved to the settings file."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        test_btn = buttons.addButton("Test connection", QDialogButtonBox.ButtonRole.ActionRole)
        test_btn.clicked.connect(lambda: self.test_requested.emit(self.result_state()))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_sftp_enabled(self) -> None:
        separate = not self.sftp_same.isChecked()
        if separate and not self.sftp_user.text().strip():
            self.sftp_user.setText(DEFAULT_SFTP_USERNAME)
        self.sftp_user.setEnabled(separate)
        self.sftp_pass.setEnabled(separate)

    def result_state(self) -> ConnectionState:
        return ConnectionState(
            host=self.host_edit.text().strip(),
            http_port=self.port_edit.text().strip(),
            use_https=self.https_check.isChecked(),
            username=self.user_edit.text().strip(),
            http_password=self.pass_edit.text(),
            timeout=self.timeout_edit.text().strip() or "30",
            sftp_port=self.sftp_port.text().strip() or str(DEFAULT_SFTP_PORT),
            sftp_username=self.sftp_user.text().strip(),
            sftp_password=self.sftp_pass.text(),
            sftp_same_as_http=self.sftp_same.isChecked(),
            demo=self.demo_check.isChecked(),
        )
