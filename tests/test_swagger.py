"""Tests for the Swagger/OpenAPI docs endpoint."""

import json

import pytest

from cheapsecurity.web import app


@pytest.fixture
def swagger_client(monkeypatch):
    """Provide a Flask test client with a mocked CCTV system."""
    from unittest.mock import MagicMock

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
    fake_cctv.night_mode_strength = "normal"
    fake_cctv.night_device_active = False
    fake_cctv.night_device_configured = False
    fake_cctv.notifications_enabled = False
    fake_cctv.telegram_enabled = False
    fake_cctv.list_recordings.return_value = []
    fake_cctv.record_dir = MagicMock()
    fake_cctv.record_dir.glob.return_value = []

    monkeypatch.setattr("cheapsecurity.web.cctv", fake_cctv)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestSwagger:
    def test_swagger_ui_page_reachable(self, swagger_client):
        response = swagger_client.get("/api/")
        assert response.status_code == 200

    def test_swagger_spec_reachable(self, swagger_client):
        response = swagger_client.get("/apispec_1.json")
        assert response.status_code == 200
        spec = json.loads(response.data)
        assert "paths" in spec
        assert "/api/status" in spec["paths"]
        assert "/api/recordings" in spec["paths"]

    def test_swagger_spec_contains_post_endpoints(self, swagger_client):
        response = swagger_client.get("/apispec_1.json")
        spec = json.loads(response.data)
        paths = spec["paths"]
        assert "post" in paths.get("/api/recordings/delete", {})
        assert "post" in paths.get("/api/recordings/download", {})
        assert "post" in paths.get("/api/settings/night_mode", {})
        assert "post" in paths.get("/api/snapshot", {})
        assert "post" in paths.get("/api/video", {})
        assert "post" in paths.get("/api/telegram/delete", {})
        assert "post" in paths.get("/api/telegram/delete_range", {})
