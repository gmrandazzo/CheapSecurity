#!/usr/bin/env python3
"""
Test manual Telegram video recording duration.
Uses a fake camera and an isolated temp config so the real service is untouched.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

import cv2
import pytest
from cheapsecurity.cctv import CCTVSystem
from helpers import FakeCapture


def _build_config(record_dir: Path) -> dict:
    return {
        "camera": {
            "device": 0,
            "width": 640,
            "height": 480,
            "fps": 30,
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
            "dir": str(record_dir),
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
            "bot_token": "test-token",
            "chat_id": "12345",
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


def _get_video_duration(path: Path) -> float:
    """Return video duration in seconds using ffprobe or OpenCV."""
    import shutil
    import subprocess

    if shutil.which("ffprobe"):
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            duration = float(result.stdout.strip())
            if duration > 0:
                return duration
        except Exception:
            pass

    cap = cv2.VideoCapture(str(path))
    try:
        if cap.isOpened():
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps > 0 and frames > 0:
                return frames / fps
    finally:
        cap.release()
    return 0.0


class TestManualVideo:
    def test_manual_video_duration(self, tmp_path):
        record_dir = tmp_path / "recordings"
        record_dir.mkdir()
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(_build_config(record_dir)))

        with patch("cv2.VideoCapture", return_value=FakeCapture(640, 480, 30)):
            system = CCTVSystem(str(config_path))
            system.start()
            time.sleep(0.5)

            requested_seconds = 3
            system._handle_telegram_video(requested_seconds, "12345")
            start = time.time()

            while not system.is_recording and (time.time() - start) < 5:
                time.sleep(0.05)
            assert system.is_recording, "Recording did not start"

            recording_started_at = time.time()
            while system.is_recording and (time.time() - recording_started_at) < 10:
                time.sleep(0.05)

            elapsed = time.time() - recording_started_at
            system.stop()

            recordings = list(record_dir.glob("*.avi")) + list(record_dir.glob("*.mp4"))
            assert recordings, "No recording created"

            assert requested_seconds - 0.5 <= elapsed <= requested_seconds + 3.0

            video_duration = _get_video_duration(recordings[-1])
            assert requested_seconds - 0.5 <= video_duration <= requested_seconds + 1.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
