"""Deep unit tests to reach high test coverage for web.py."""

from unittest.mock import MagicMock

import cheapsecurity.web as web_module
import pytest


@pytest.fixture
def mock_cctv(tmp_path):
    record_dir = tmp_path / "recordings"
    record_dir.mkdir()
    (record_dir / "clip1.avi").write_bytes(b"DATA1")
    (record_dir / "clip2.avi").write_bytes(b"DATA2")

    sys = MagicMock()
    sys.record_dir = record_dir
    sys.night_mode = False
    sys.night_mode_strength = "normal"
    sys.night_device_active = False
    sys.night_device = None
    sys.notifications_enabled = True
    sys.telegram_enabled = True
    sys.gdrive_enabled = True
    sys.onedrive_enabled = True
    sys.encryption_passphrase = "secret"
    sys.encrypt_telegram = True
    sys.encrypt_gdrive = True
    sys.encrypt_onedrive = True
    sys.cfg = {
        "telegram": {"chat_id": "12345"},
        "web": {"auth": {"enabled": True, "username": "admin", "password": "pass"}},
    }
    sys._recording_files.return_value = [record_dir / "clip1.avi", record_dir / "clip2.avi"]
    sys.get_frame.return_value = b"JPEG_HEADER"

    web_module.cctv = sys
    yield sys
    web_module.cctv = None


@pytest.fixture
def client(mock_cctv):
    web_module.app.config["TESTING"] = True
    with web_module.app.test_client() as client:
        yield client


def test_basic_auth_required_when_enabled(client, mock_cctv):
    # Missing auth header -> 401
    res = client.get("/api/settings")
    assert res.status_code == 401

    # Wrong credentials -> 401
    auth_header = {"Authorization": "Basic " + b"wrong:credentials".hex()}
    res = client.get("/api/settings", headers=auth_header)
    assert res.status_code == 401


def test_recordings_bulk_actions(client, mock_cctv):
    import base64
    valid_auth = {
        "Authorization": "Basic " + base64.b64encode(b"admin:pass").decode("utf-8"),
        "X-Requested-With": "XMLHttpRequest",
    }

    # Bulk ZIP download
    res = client.post(
        "/api/recordings/download",
        json={"filenames": ["clip1.avi", "clip2.avi"]},
        headers=valid_auth,
    )
    assert res.status_code == 200
    assert res.content_type == "application/zip"

    # Bulk Delete
    res_del = client.post(
        "/api/recordings/delete",
        json={"filenames": ["clip1.avi"]},
        headers=valid_auth,
    )
    assert res_del.status_code == 200
    assert len(res_del.json["results"]) == 1

    # Bulk Telegram Send
    res_tel = client.post(
        "/api/recordings/telegram",
        json={"filenames": ["clip2.avi"]},
        headers=valid_auth,
    )
    assert res_tel.status_code == 200
    assert len(res_tel.json["results"]) == 1


def test_settings_endpoints_post(client, mock_cctv):
    import base64
    auth = {
        "Authorization": "Basic " + base64.b64encode(b"admin:pass").decode("utf-8"),
        "X-Requested-With": "XMLHttpRequest",
    }

    client.post("/api/settings/notifications", json={"enabled": False}, headers=auth)
    assert mock_cctv.set_notifications_enabled.called

    client.post("/api/settings/telegram", json={"enabled": True}, headers=auth)
    assert mock_cctv.set_telegram_enabled.called

    client.post("/api/settings/gdrive", json={"enabled": True}, headers=auth)
    assert mock_cctv.set_gdrive_enabled.called

    client.post("/api/settings/onedrive", json={"enabled": True}, headers=auth)
    assert mock_cctv.set_onedrive_enabled.called

    client.post("/api/settings/auth", json={"enabled": False}, headers=auth)
    assert mock_cctv.set_auth_enabled.called

    client.post(
        "/api/settings/encryption",
        json={
            "passphrase": "new_pass",
            "telegram": True,
            "google_drive": False,
            "onedrive": True,
        },
        headers=auth,
    )
    assert mock_cctv.set_encryption_passphrase.called
    assert mock_cctv.set_encrypt_telegram.called


def test_uninitialized_cctv_503(client):
    web_module.cctv = None
    res = client.get("/api/settings")
    assert res.status_code == 503


def test_telegram_delete_endpoints(client, mock_cctv):
    import base64
    auth = {
        "Authorization": "Basic " + base64.b64encode(b"admin:pass").decode("utf-8"),
        "X-Requested-With": "XMLHttpRequest",
    }
    mock_cctv._delete_telegram_message.return_value = True

    res = client.post("/api/telegram/delete", json={"message_id": 123}, headers=auth)
    assert res.status_code == 200
    assert res.json["deleted"] is True

    res_range = client.post("/api/telegram/delete_range", json={"min_id": 100, "max_id": 105}, headers=auth)
    assert res_range.status_code == 200
    assert res_range.json["deleted"] == 6


def test_video_download_and_mjpeg_stream(client, mock_cctv):
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pass").decode("utf-8")}

    # Get single recording file
    res = client.get("/recordings/clip1.avi", headers=auth)
    assert res.status_code in (200, 206)

    # Get non-existent recording file -> 404
    res_404 = client.get("/recordings/nonexistent.avi", headers=auth)
    assert res_404.status_code == 404

    # MJPEG stream endpoint /video_feed
    res_stream = client.get("/video_feed", headers=auth)
    assert res_stream.status_code == 200
    assert "multipart/x-mixed-replace" in res_stream.content_type
