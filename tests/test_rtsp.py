"""Unit tests for the RTSP publisher."""

from unittest.mock import MagicMock, patch

import pytest
from cheapsecurity.rtsp import RTSPPublisher


@pytest.fixture
def rtsp_cfg():
    return {
        "enabled": True,
        "port": 8554,
        "path": "live",
        "width": 640,
        "height": 480,
        "fps": 15,
        "mediamtx_binary": "/usr/local/bin/mediamtx",
        "mediamtx_config": "rtsp/mediamtx.yml",
    }


class TestRTSPPublisher:
    def test_disabled_does_not_start_anything(self, rtsp_cfg):
        rtsp_cfg["enabled"] = False
        publisher = RTSPPublisher(rtsp_cfg, "0.0.0.0", 5000)
        with patch("cheapsecurity.rtsp.subprocess.Popen") as mock_popen:
            publisher.start()
            # Give the thread a moment if it were to run
            publisher.stop()
            mock_popen.assert_not_called()

    def test_missing_mediamtx_logs_error(self, rtsp_cfg):
        publisher = RTSPPublisher(rtsp_cfg, "0.0.0.0", 5000)
        with patch("cheapsecurity.rtsp.shutil.which", return_value=None), patch(
            "cheapsecurity.rtsp.subprocess.Popen"
        ) as mock_popen:
            publisher.start()
            publisher.stop()
            mock_popen.assert_not_called()

    def test_starts_mediamtx_and_ffmpeg(self, rtsp_cfg):
        publisher = RTSPPublisher(rtsp_cfg, "0.0.0.0", 5000)
        commands = []

        def popen_side_effect(cmd, **kwargs):
            commands.append(cmd)
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None
            return fake_proc

        with patch("cheapsecurity.rtsp.shutil.which", return_value="/bin/binary"), patch(
            "cheapsecurity.rtsp.subprocess.Popen", side_effect=popen_side_effect
        ), patch("cheapsecurity.rtsp.RTSPPublisher._wait_for_port", return_value=True):
            publisher.start()
            # Allow background thread to start processes
            import time

            time.sleep(0.2)
            publisher.stop()

        # subprocess.Popen called twice: mediamtx + ffmpeg
        assert len(commands) == 2
        assert commands[0][0] == "/usr/local/bin/mediamtx"
        assert "ffmpeg" in commands[1]
        assert "rtsp://127.0.0.1:8554/live" in commands[1]

    def test_stop_terminates_subprocesses(self, rtsp_cfg):
        publisher = RTSPPublisher(rtsp_cfg, "0.0.0.0", 5000)
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        publisher._ffmpeg_proc = fake_proc
        publisher._mediamtx_proc = fake_proc

        publisher.stop()

        fake_proc.terminate.assert_called()
        fake_proc.wait.assert_called()
