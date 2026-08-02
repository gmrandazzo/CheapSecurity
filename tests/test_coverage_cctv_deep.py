"""Deep unit tests to reach high test coverage for cctv.py."""

import json
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from cheapsecurity.cctv import CCTVSystem


@pytest.fixture
def cctv_sys(tmp_path):
    record_dir = tmp_path / "recordings"
    record_dir.mkdir()
    config = {
        "camera": {
            "device": 0,
            "width": 640,
            "height": 480,
            "fps": 15,
            "night_device": 1,
            "night_device_width": 640,
            "night_device_height": 480,
            "night_device_fps": 15,
            "night_software_enhance": True,
            "night_mode": False,
            "night_mode_strength": "aggressive",
        },
        "motion": {
            "threshold": 25,
            "min_area": 500,
            "blur_size": 21,
            "cooldown_seconds": 2,
            "scale": 0.5,
        },
        "recording": {
            "dir": str(record_dir),
            "max_duration_seconds": 5,
            "pre_buffer_seconds": 1,
            "codec": "MJPG",
            "extension": ".avi",
        },
        "notifications": {
            "enabled": True,
            "smtp": {
                "server": "smtp.example.com",
                "port": 465,
                "username": "user",
                "password": "pass",
                "use_tls": True,
            },
            "from": "from@example.com",
            "to": ["to@example.com"],
            "subject": "Alert",
            "min_interval_minutes": 1,
        },
        "telegram": {
            "enabled": True,
            "bot_token": "BOT_TOKEN_123",
            "chat_id": "12345",
            "send_video": True,
            "min_interval_minutes": 1,
            "poll_commands": True,
        },
        "cloud": {
            "google_drive": {
                "enabled": True,
                "client_id": "gclient",
                "client_secret": "gsecret",
                "refresh_token": "gtoken",
                "folder_id": "gfolder",
            },
            "onedrive": {
                "enabled": True,
                "client_id": "mclient",
                "client_secret": "msecret",
                "refresh_token": "mtoken",
                "folder_path": "CheapSecurity",
            },
        },
        "encryption": {
            "passphrase": "SecretPassphrase123!",
            "telegram": True,
            "google_drive": True,
            "onedrive": True,
        },
        "storage": {
            "max_age_days": 1,
            "max_size_gb": 0.0001,  # low threshold to test size cleanup
            "cleanup_interval_minutes": 1,
            "delete_old_on_startup": True,
            "emergency_free_space_gb": 1000.0,  # high to force emergency cleanup test
            "emergency_delete_count": 2,
        },
        "web": {"host": "127.0.0.1", "port": 5000, "stream_scale": 0.5},
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    sys = CCTVSystem(config_path=config_file)
    sys.record_dir = record_dir
    yield sys
    sys.stop()


def test_night_mode_enhancement_profiles(cctv_sys):
    cctv_sys.set_night_mode_strength("low")
    assert cctv_sys.night_mode_strength == "low"
    cctv_sys.set_night_mode_strength("aggressive")
    assert cctv_sys.night_mode_strength == "aggressive"
    cctv_sys.set_night_mode_strength("invalid_strength")
    assert cctv_sys.night_mode_strength == "normal"


def test_storage_cleanup_and_emergency_deletion(cctv_sys, tmp_path):
    record_dir = cctv_sys.record_dir
    f1 = record_dir / "old_recording1.avi"
    f2 = record_dir / "old_recording2.avi"
    f3 = record_dir / "recent_recording.avi"

    f1.write_bytes(b"X" * 1024 * 1024)
    f2.write_bytes(b"Y" * 1024 * 1024)
    f3.write_bytes(b"Z" * 1024 * 1024)

    # Artificially set old mtime on f1 & f2
    old_time = time.time() - 86400 * 5
    import os
    os.utime(f1, (old_time, old_time))
    os.utime(f2, (old_time + 10, old_time + 10))

    cctv_sys._ensure_disk_space()
    cctv_sys._cleanup_storage()

    # Old files should be deleted
    assert not f1.exists()


def test_telegram_store_and_delete_messages(cctv_sys, tmp_path):
    cctv_sys._store_telegram_message(101, "12345", "video", "test.avi")
    cctv_sys._store_telegram_message(102, "12345", "photo", "snapshot")

    messages = cctv_sys._load_telegram_messages()
    assert len(messages) >= 2

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        success = cctv_sys._delete_telegram_message(101, chat_id="12345")
        assert success is True


def test_telegram_all_commands_handling(cctv_sys):
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 555}}
        mock_post.return_value = mock_resp

        # Test authorization check
        unauth_update = {
            "update_id": 1,
            "message": {"text": "/snapshot", "chat": {"id": 99999}},
        }
        cctv_sys._handle_telegram_update(unauth_update)

        # Test command parsing
        commands = [
            "/sent",
            "/delete 101",
            "/delete last",
            "/delete_range 100 200",
            "/id",
            "/telegram_on",
            "/telegram_off",
            "/email_on",
            "/email_off",
            "/night_mode_on",
            "/night_mode_off",
            "/night_mode low",
            "/night_mode invalid",
            "/encrypt_telegram_on",
            "/encrypt_telegram_off",
            "/encrypt_gdrive_on",
            "/encrypt_gdrive_off",
            "/encrypt_onedrive_on",
            "/encrypt_onedrive_off",
            "/encryption",
            "/help",
        ]

        for cmd_str in commands:
            update = {
                "update_id": 2,
                "message": {"text": cmd_str, "chat": {"id": 12345}},
            }
            cctv_sys._handle_telegram_update(update)


@patch("smtplib.SMTP_SSL")
def test_send_alert_email(mock_smtp, cctv_sys):
    mock_instance = MagicMock()
    mock_smtp.return_value.__enter__.return_value = mock_instance

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cctv_sys._send_alert_email(frame)

    assert mock_instance.sendmail.called


@patch("requests.post")
def test_gdrive_upload_success_and_error_handling(mock_post, cctv_sys, tmp_path):
    dummy_video = tmp_path / "motion_test.avi"
    dummy_video.write_bytes(b"HEADER_AND_VIDEO_DATA")

    cctv_sys.gdrive_enabled = True
    cctv_sys.gdrive_client_id = "cid"
    cctv_sys.gdrive_refresh_token = "ref"
    cctv_sys.encrypt_gdrive = False

    # Token success, upload success
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "ACCESS_123"}

    upload_resp = MagicMock()
    upload_resp.status_code = 200

    mock_post.side_effect = [token_resp, upload_resp]

    res = cctv_sys._upload_to_gdrive(dummy_video)
    assert res is True

    # Token error response
    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.text = "invalid grant"
    mock_post.side_effect = [err_resp]

    res_err = cctv_sys._upload_to_gdrive(dummy_video)
    assert res_err is False


@patch("requests.post")
@patch("requests.put")
def test_onedrive_upload_success_and_error_handling(mock_put, mock_post, cctv_sys, tmp_path):
    dummy_video = tmp_path / "motion_test.avi"
    dummy_video.write_bytes(b"ONEDRIVE_DATA")

    cctv_sys.onedrive_enabled = True
    cctv_sys.onedrive_client_id = "cid"
    cctv_sys.onedrive_refresh_token = "ref"
    cctv_sys.encrypt_onedrive = False

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "ACCESS_MS"}
    mock_post.return_value = token_resp

    upload_resp = MagicMock()
    upload_resp.status_code = 201
    mock_put.return_value = upload_resp

    res = cctv_sys._upload_to_onedrive(dummy_video)
    assert res is True


def test_fix_video_duration_and_manual_recording(cctv_sys, tmp_path):
    video = tmp_path / "test_fix.avi"
    video.write_bytes(b"VIDEO_HEADER_DATA")

    cctv_sys._fix_video_duration(video, actual_duration=10.0, frames_written=150, writer_fps=15.0)
    assert video.exists()

    cctv_sys.trigger_manual_recording(5, chat_id="12345")
    assert cctv_sys._manual_recording_active is True

    with patch("cheapsecurity.cctv.CCTVSystem._send_telegram_video") as mock_send:
        cctv_sys._finalize_manual_recording(video, chat_id="12345", actual_duration=5.0, frames_written=75, writer_fps=15.0)
        assert mock_send.called


@patch("cv2.VideoCapture")
def test_camera_switching_and_release(mock_vcap, cctv_sys):
    mock_cap_instance = MagicMock()
    mock_cap_instance.isOpened.return_value = True
    mock_cap_instance.get.return_value = 15.0
    mock_vcap.return_value = mock_cap_instance

    opened = cctv_sys._open_capture()
    assert opened is True

    cctv_sys._release_capture()
    assert cctv_sys.cap is None

    switched = cctv_sys._switch_camera()
    assert isinstance(switched, bool)


def test_motion_detection_and_recording_triggers(cctv_sys):
    # Black frame
    f1 = np.zeros((480, 640, 3), dtype=np.uint8)
    assert cctv_sys._detect_motion(f1) is False

    # White frame (huge motion difference)
    f2 = np.full((480, 640, 3), 255, dtype=np.uint8)
    assert cctv_sys._detect_motion(f2) is True

    cctv_sys._start_recording(f2)
    assert cctv_sys.is_recording is True

    cctv_sys._write_frame(f2)
    assert cctv_sys._frames_written >= 1

    cctv_sys._stop_recording()
    assert cctv_sys.is_recording is False


def test_redact_token_and_save_config(cctv_sys):
    redacted = cctv_sys._redact_token("Error with token BOT_TOKEN_123 in URL")
    assert "BOT_TOKEN_123" not in redacted
    assert "<TOKEN>" in redacted

    cctv_sys.set_auth_enabled(True)
    assert cctv_sys.cfg["web"]["auth"]["enabled"] is True


@patch("requests.post")
def test_telegram_send_photo_and_video_retry_failures(mock_post, cctv_sys, tmp_path):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_post.return_value = mock_resp

    # Should retry 3 times and gracefully handle 500 server error
    cctv_sys.encrypt_telegram = False
    cctv_sys._send_telegram_photo(b"DUMMY_PHOTO", chat_id="12345", caption="Snap")
    assert mock_post.call_count >= 3

    mock_post.reset_mock()
    dummy_video = tmp_path / "vid.avi"
    dummy_video.write_bytes(b"VIDEO")
    cctv_sys._send_telegram_video(dummy_video, chat_id="12345")
    assert mock_post.call_count >= 3
