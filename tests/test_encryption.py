"""Unit tests for AES-256 ZIP encryption features."""

import json
from unittest.mock import MagicMock, patch

import pytest
import pyzipper

from cheapsecurity.cctv import CCTVSystem


@pytest.fixture
def system(tmp_path):
    record_dir = tmp_path / "recordings"
    record_dir.mkdir()
    config = {
        "camera": {"device": 0, "width": 640, "height": 480, "fps": 15},
        "motion": {"threshold": 25, "min_area": 500, "blur_size": 21, "cooldown_seconds": 5, "scale": 0.5},
        "recording": {"dir": str(record_dir), "max_duration_seconds": 10, "pre_buffer_seconds": 2, "codec": "MJPG", "extension": ".avi"},
        "notifications": {"enabled": False},
        "storage": {"max_age_days": 3, "max_size_gb": 10, "cleanup_interval_minutes": 60, "delete_old_on_startup": False},
        "web": {"host": "127.0.0.1", "port": 5000},
        "telegram": {
            "enabled": True,
            "bot_token": "TEST_BOT_TOKEN",
            "chat_id": "12345",
            "poll_commands": True,
        },
        "cloud": {
            "google_drive": {
                "enabled": True,
                "client_id": "gclient",
                "client_secret": "gsecret",
                "refresh_token": "gtoken",
            },
            "onedrive": {
                "enabled": True,
                "client_id": "mclient",
                "client_secret": "msecret",
                "refresh_token": "mtoken",
            },
        },
        "encryption": {
            "passphrase": "SecretPassphrase123!",
            "telegram": True,
            "google_drive": True,
            "onedrive": True,
        },
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    sys = CCTVSystem(config_path=config_file)
    sys.record_dir = record_dir
    yield sys
    sys.stop()


def test_create_aes_zip_and_extract(system, tmp_path):
    dummy_file = tmp_path / "test_video.avi"
    dummy_content = b"VIDEO_DUMMY_DATA_123456789"
    dummy_file.write_bytes(dummy_content)

    system.set_encryption_passphrase("MySuperSecret123")
    zip_path = system._create_aes_zip(dummy_file)

    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    # Extract with correct password
    with pyzipper.AESZipFile(zip_path, "r") as zf:
        zf.setpassword(b"MySuperSecret123")
        extracted_data = zf.read(dummy_file.name)
        assert extracted_data == dummy_content

    # Extraction fails with wrong password
    with pyzipper.AESZipFile(zip_path, "r") as zf:
        zf.setpassword(b"WrongPassword")
        with pytest.raises((RuntimeError, pyzipper.BadZipFile)):
            zf.read(dummy_file.name)

    zip_path.unlink()


def test_encryption_mutators(system):
    system.set_encryption_passphrase("NewPass123")
    assert system.encryption_passphrase == "NewPass123"

    system.set_encrypt_telegram(False)
    assert system.encrypt_telegram is False

    system.set_encrypt_gdrive(True)
    assert system.encrypt_gdrive is True

    system.set_encrypt_onedrive(False)
    assert system.encrypt_onedrive is False


@patch("requests.post")
def test_send_telegram_video_encrypted(mock_post, system, tmp_path):
    dummy_video = tmp_path / "motion_test.avi"
    dummy_video.write_bytes(b"DATA")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 999}}
    mock_post.return_value = mock_resp

    system.encrypt_telegram = True
    system.encryption_passphrase = "SecretPass"

    system._send_telegram_video(dummy_video, chat_id="12345")

    assert mock_post.called
    args, kwargs = mock_post.call_args
    assert "sendDocument" in args[0]
    assert "document" in kwargs.get("files", {})
    doc_name = kwargs["files"]["document"][0]
    assert doc_name.endswith(".zip")


@patch("requests.post")
def test_telegram_encryption_bot_commands(mock_post, system):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}
    mock_post.return_value = mock_resp

    # Toggle telegram encryption off via command
    update_off = {
        "update_id": 1,
        "message": {"text": "/encrypt_telegram_off", "chat": {"id": 12345}},
    }
    system._handle_telegram_update(update_off)
    assert system.encrypt_telegram is False

    # Toggle telegram encryption on via command
    update_on = {
        "update_id": 2,
        "message": {"text": "/encrypt_telegram_on", "chat": {"id": 12345}},
    }
    system._handle_telegram_update(update_on)
    assert system.encrypt_telegram is True

    # Check status command
    update_status = {
        "update_id": 3,
        "message": {"text": "/encryption", "chat": {"id": 12345}},
    }
    system._handle_telegram_update(update_status)
    assert mock_post.called
