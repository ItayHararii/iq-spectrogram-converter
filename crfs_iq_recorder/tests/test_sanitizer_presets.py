import json

from crfs_iq_recorder.connection_state import ConnectionState
from crfs_iq_recorder.presets import load_preset, save_preset
from crfs_iq_recorder.sanitizer import redact_obj, redact_text, redact_url


def test_redact_url_userinfo():
    assert "secret" not in redact_url("http://admin:secret@192.0.2.10/emp/")


def test_sftp_password_is_redacted():
    data = redact_obj({"sftp_password": "secret", "host": "192.0.2.10"})
    assert data["sftp_password"] == "***"
    assert data["host"] == "192.0.2.10"


def test_redact_authorization_header_value():
    data = redact_obj({"Authorization": "Basic abcdef", "password": "x", "task_id": "iq-testing"})
    assert data["Authorization"] == "***"
    assert data["password"] == "***"
    assert data["task_id"] == "iq-testing"


def test_redact_basic_in_text():
    text = redact_text("Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==")
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in text
    assert "***" in text


def test_connection_state_persistable_omits_passwords():
    state = ConnectionState(http_password="secret-http", sftp_password="secret-sftp")
    data = state.persistable()
    blob = json.dumps(data)
    assert "password" not in data
    assert "http_password" not in data
    assert "sftp_password" not in data
    assert "secret-http" not in blob
    assert "secret-sftp" not in blob
    assert "demo" not in data
    assert data["download_dir"]


def test_saved_demo_flag_is_ignored_unless_constructor_requests_it():
    state = ConnectionState.from_settings({"host": "10.1.0.11", "demo": True})
    assert state.demo is False
    forced = ConnectionState.from_settings({"host": "10.1.0.11", "demo": True}, demo=True)
    assert forced.demo is True
    assert "demo" not in forced.persistable()


def test_legacy_http_sftp_settings_switch_to_crfs_ssh_account():
    state = ConnectionState.from_settings(
        {
            "host": "192.168.1.12",
            "username": "admin",
            "sftp_username": "admin",
            "sftp_same_as_http": True,
        }
    )
    from crfs_iq_recorder.constants import DEFAULT_PASSWORD, DEFAULT_SFTP_PASSWORD, DEFAULT_SFTP_USERNAME

    assert state.sftp_same_as_http is False
    assert state.sftp_user() == DEFAULT_SFTP_USERNAME
    assert state.sftp_pass() == DEFAULT_SFTP_PASSWORD
    assert state.http_password == DEFAULT_PASSWORD


def test_preset_strips_password(tmp_path):
    path = tmp_path / "preset.json"
    save_preset(path, {"host": "192.0.2.10", "password": "nope", "task_id": "iq-testing"})
    loaded = load_preset(path)
    assert "password" not in loaded
    assert loaded["task_id"] == "iq-testing"
    assert "nope" not in path.read_text(encoding="utf-8")
