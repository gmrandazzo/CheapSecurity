"""Unit tests for cheapsecurity.web."""

import base64
import json
from unittest.mock import MagicMock

import pytest
from cheapsecurity.web import app


@pytest.fixture
def client(monkeypatch):
    """Provide a Flask test client with a mocked CCTV system."""
    fake_cctv = MagicMock()
    fake_cctv.cfg = {
        "camera": {"width": 640, "height": 480, "fps": 15},
        "web": {"auth": {"enabled": False, "username": "admin", "password": "changeme"}},
    }
    fake_cctv.running = True
    fake_cctv.is_recording = False
    fake_cctv.motion_active = False
    fake_cctv.recording_path = None
    fake_cctv.cap = None
    fake_cctv.width = 640
    fake_cctv.height = 480
    fake_cctv.actual_fps = 15.0
    fake_cctv.night_mode = False
    fake_cctv.notifications_enabled = False
    fake_cctv.telegram_enabled = False
    fake_cctv.list_recordings.return_value = []
    fake_cctv.record_dir = MagicMock()
    fake_cctv.record_dir.glob.return_value = []

    monkeypatch.setattr("cheapsecurity.web.cctv", fake_cctv)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, fake_cctv


class TestIndex:
    def test_index_renders(self, client):
        test_client, _ = client
        response = test_client.get("/")
        assert response.status_code == 200


class TestStatus:
    def test_status_returns_state(self, client):
        test_client, fake_cctv = client
        response = test_client.get("/api/status")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["running"] is True
        assert data["resolution"] == "640x480"


class TestRecordings:
    def test_empty_recordings_list(self, client):
        test_client, _ = client
        response = test_client.get("/api/recordings")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["recordings"] == []


class TestAuth:
    def test_auth_disabled_allows_request(self, client):
        test_client, fake_cctv = client
        fake_cctv.cfg["web"]["auth"]["enabled"] = False
        response = test_client.get("/api/status")
        assert response.status_code == 200

    def test_auth_enabled_rejects_no_header(self, client):
        test_client, fake_cctv = client
        fake_cctv.cfg["web"]["auth"]["enabled"] = True
        response = test_client.get("/api/status")
        assert response.status_code == 401

    def test_auth_enabled_accepts_valid_header(self, client):
        test_client, fake_cctv = client
        fake_cctv.cfg["web"]["auth"]["enabled"] = True
        credentials = base64.b64encode(b"admin:changeme").decode("ascii")
        response = test_client.get("/api/status", headers={"Authorization": f"Basic {credentials}"})
        assert response.status_code == 200

    def test_auth_enabled_rejects_invalid_credentials(self, client):
        test_client, fake_cctv = client
        fake_cctv.cfg["web"]["auth"]["enabled"] = True
        credentials = base64.b64encode(b"admin:wrong").decode("ascii")
        response = test_client.get("/api/status", headers={"Authorization": f"Basic {credentials}"})
        assert response.status_code == 401


class TestDownload:
    def test_download_recordings_missing_filenames(self, client):
        test_client, _ = client
        response = test_client.post(
            "/api/recordings/download",
            json={},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 400

    def test_download_recordings_success(self, client, tmp_path):
        test_client, fake_cctv = client
        rec_dir = tmp_path / "recordings"
        rec_dir.mkdir()
        fake_cctv.record_dir = rec_dir

        file1 = rec_dir / "motion_1.avi"
        file1.write_bytes(b"dummy video data")

        response = test_client.post(
            "/api/recordings/download",
            json={"filenames": ["motion_1.avi"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/zip"


class TestTelegramSend:
    def test_telegram_send_missing_filenames(self, client):
        test_client, _ = client
        response = test_client.post(
            "/api/recordings/telegram",
            json={},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 400

    def test_telegram_send_success(self, client, tmp_path):
        test_client, fake_cctv = client
        rec_dir = tmp_path / "recordings"
        rec_dir.mkdir()
        fake_cctv.record_dir = rec_dir

        file1 = rec_dir / "motion_1.avi"
        file1.write_bytes(b"dummy video data")

        response = test_client.post(
            "/api/recordings/telegram",
            json={"filenames": ["motion_1.avi"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["results"][0]["sent"] is True


class TestCSRF:
    def test_post_rejects_missing_csrf_header(self, client):
        test_client, _ = client
        response = test_client.post("/api/recordings/delete", json={"filenames": ["x.avi"]})
        assert response.status_code == 403

    def test_post_accepts_csrf_header(self, client, tmp_path):
        test_client, fake_cctv = client
        rec_dir = tmp_path / "recordings"
        rec_dir.mkdir()
        fake_cctv.record_dir = rec_dir

        file1 = rec_dir / "motion_1.avi"
        file1.write_bytes(b"dummy video data")

        response = test_client.post(
            "/api/recordings/delete",
            json={"filenames": ["motion_1.avi"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 200
