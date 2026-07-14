"""Shared fixtures for CheapSecurity tests."""

import json

import pytest
from cheapsecurity.cctv import CCTVSystem
from helpers import FakeCapture


@pytest.fixture
def config_dict():
    """Return a minimal valid configuration dictionary."""
    return {
        "camera": {
            "device": 0,
            "width": 640,
            "height": 480,
            "fps": 15,
            "night_mode": False,
            "night_mode_fps": 5,
            "night_mode_gain": 255,
            "night_mode_brightness": 200,
            "night_mode_contrast": 200,
        },
        "motion": {
            "threshold": 25,
            "min_area": 500,
            "blur_size": 21,
            "cooldown_seconds": 2,
            "scale": 0.5,
        },
        "recording": {
            "dir": "./recordings",
            "max_duration_seconds": 10,
            "pre_buffer_seconds": 1,
            "codec": "MJPG",
            "extension": ".avi",
        },
        "notifications": {
            "enabled": False,
            "smtp": {
                "server": "smtp.example.com",
                "port": 465,
                "username": "user",
                "password": "pass",
                "use_tls": True,
            },
            "from": "from@example.com",
            "to": ["to@example.com"],
            "subject": "Test",
            "min_interval_minutes": 5,
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "send_video": False,
            "min_interval_minutes": 5,
            "poll_commands": False,
        },
        "web": {
            "host": "127.0.0.1",
            "port": 5000,
            "stream_scale": 1.0,
            "auth": {
                "enabled": False,
                "username": "admin",
                "password": "changeme",
            },
        },
        "storage": {
            "max_age_days": 3,
            "max_size_gb": 10,
            "cleanup_interval_minutes": 60,
            "delete_old_on_startup": False,
            "emergency_free_space_gb": 1.0,
            "emergency_delete_count": 4,
        },
    }


@pytest.fixture
def temp_config(config_dict, tmp_path):
    """Write a config file to a temp directory and return its path."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_dict))
    return str(config_path)


@pytest.fixture
def temp_recordings(tmp_path):
    """Create an isolated recordings directory."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    return recordings


@pytest.fixture
def patched_config(config_dict, temp_recordings, tmp_path):
    """Return a config dict using temp recordings dir, already written to disk."""
    config_dict["recording"]["dir"] = str(temp_recordings)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_dict))
    return str(config_path)


@pytest.fixture
def fake_capture():
    return FakeCapture(640, 480, 15)


@pytest.fixture
def system(patched_config, monkeypatch):
    """Build a CCTVSystem with a fake camera but do not start the main loop."""
    monkeypatch.setattr("cv2.VideoCapture", lambda *args, **kwargs: FakeCapture(640, 480, 15))
    system = CCTVSystem(patched_config)
    yield system
    system.stop()
