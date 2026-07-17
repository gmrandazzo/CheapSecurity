"""Unit tests for cheapsecurity.cctv."""

import json
import time
from pathlib import Path

import numpy as np
from cheapsecurity.cctv import CCTVSystem
from helpers import FakeCapture


class TestCCTVSystemInit:
    def test_loads_config(self, system, patched_config):
        with open(patched_config) as f:
            cfg = json.load(f)
        assert system.cfg == cfg
        assert system.width == cfg["camera"]["width"]
        assert system.height == cfg["camera"]["height"]
        assert system.fps == cfg["camera"]["fps"]

    def test_blur_size_is_odd(self, config_dict, tmp_path, monkeypatch):
        config_dict["motion"]["blur_size"] = 20
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_dict))
        monkeypatch.setattr("cv2.VideoCapture", lambda *args, **kwargs: FakeCapture())
        system = CCTVSystem(str(config_path))
        assert system.blur_size == 21
        system.stop()

    def test_record_dir_created(self, system, patched_config):
        cfg = json.loads(Path(patched_config).read_text())
        assert Path(cfg["recording"]["dir"]).exists()


class TestNightMode:
    def test_no_op_when_disabled(self, system):
        frame = np.ones((10, 10, 3), dtype=np.uint8) * 128
        result = system._apply_night_mode(frame)
        np.testing.assert_array_equal(result, frame)

    def test_clahe_applied_when_enabled(self, system):
        system.night_mode = True
        frame = np.ones((10, 10, 3), dtype=np.uint8) * 128
        result = system._apply_night_mode(frame)
        assert result.shape == frame.shape
        assert result.dtype == frame.dtype


class TestMotionDetection:
    def test_no_motion_on_identical_frames(self, system):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        system._prev_gray = None
        assert system._detect_motion(frame) is False
        assert system._detect_motion(frame) is False

    def test_motion_detected_on_changed_frames(self, system):
        system._prev_gray = None
        frame1 = np.zeros((100, 100, 3), dtype=np.uint8)
        frame2 = np.ones((100, 100, 3), dtype=np.uint8) * 255
        system._detect_motion(frame1)
        assert system._detect_motion(frame2) is True

    def test_respects_min_area(self, system):
        system._prev_gray = None
        system.min_area = 10000
        frame1 = np.zeros((100, 100, 3), dtype=np.uint8)
        frame2 = np.ones((100, 100, 3), dtype=np.uint8) * 255
        system._detect_motion(frame1)
        # The whole frame changed but min_area is larger than the frame area
        assert system._detect_motion(frame2) is False


class TestStorageCleanup:
    def test_list_recordings(self, system, temp_recordings):
        (temp_recordings / "motion_20260101_120000.avi").write_bytes(b"1234")
        (temp_recordings / "motion_20260101_120001.avi").write_bytes(b"5678")
        recordings = system.list_recordings()
        assert len(recordings) == 2
        assert recordings[0]["filename"] == "motion_20260101_120001.avi"

    def test_cleanup_deletes_old_files(self, system, temp_recordings):
        old_file = temp_recordings / "motion_20200101_120000.avi"
        old_file.write_bytes(b"old")
        # Set mtime far in the past
        old_time = time.time() - 86400 * 365
        old_file.touch()
        import os

        os.utime(old_file, (old_time, old_time))
        system._cleanup_storage()
        assert not old_file.exists()

    def test_cleanup_ignores_other_file_types(self, system, temp_recordings):
        other_file = temp_recordings / "important_document.txt"
        other_file.write_bytes(b"do not delete")
        old_time = time.time() - 86400 * 365
        other_file.touch()
        import os

        os.utime(other_file, (old_time, old_time))
        system._cleanup_storage()
        assert other_file.exists()

    def test_human_size(self, system):
        assert system._human_size(0) == "0.0 B"
        assert system._human_size(1024) == "1.0 KB"
        assert system._human_size(1024 * 1024) == "1.0 MB"


class TestTelegramCommands:
    def test_snapshot_no_frame(self, system, monkeypatch):
        system.telegram_token = "token"
        system.telegram_chat_id = "123"
        monkeypatch.setattr(system, "get_frame", lambda: None)
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_snapshot("123")
        assert any("No camera frame" in msg for msg in sent)

    def test_video_command_clamps_seconds(self, system, monkeypatch):
        system.telegram_token = "token"
        system.telegram_chat_id = "123"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_video(100, "123")
        assert system._manual_record_until > time.time() + 55
        assert system._manual_record_until <= time.time() + 60

    def test_video_command_minimum_one_second(self, system, monkeypatch):
        system.telegram_token = "token"
        system.telegram_chat_id = "123"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_video(0, "123")
        assert system._manual_record_until > time.time()
        assert system._manual_record_until <= time.time() + 2


class TestConfigMutators:
    def test_set_night_mode_updates_config(self, system, tmp_path, monkeypatch):
        monkeypatch.setattr(system, "_apply_camera_night_mode", lambda: None)
        system.set_night_mode(True)
        assert system.cfg["camera"]["night_mode"] is True
        cfg = json.loads(Path(system.config_path).read_text())
        assert cfg["camera"]["night_mode"] is True

    def test_set_telegram_enabled_updates_config(self, system):
        system.set_telegram_enabled(True)
        assert system.cfg["telegram"]["enabled"] is True

    def test_set_auth_enabled_updates_config(self, system):
        system.set_auth_enabled(True)
        assert system.cfg["web"]["auth"]["enabled"] is True
