"""Unit tests for cheapsecurity.cctv."""

import json
import time
from pathlib import Path

import cv2
import numpy as np
from helpers import FakeCapture

from cheapsecurity.cctv import CCTVSystem


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


class TestNightCameraSwitching:
    def test_single_camera_mode_keeps_day_device(self, system):
        system.night_device = None
        system._open_capture()
        system.set_night_mode(True)
        assert system._active_device == system.device
        assert system.cap is not None
        assert system.cap.get(cv2.CAP_PROP_GAIN) == 255

    def test_dual_camera_switch_opens_night_device(self, system, monkeypatch):
        opened = []

        def factory(device, *args, **kwargs):
            opened.append(device)
            if device == 0:
                return FakeCapture(640, 480, 15)
            return FakeCapture(320, 240, 10)

        monkeypatch.setattr("cv2.VideoCapture", factory)
        system.night_device = 1
        system.night_device_width = 320
        system.night_device_height = 240
        system.night_device_fps = 10
        system.set_night_mode(True)
        assert system._active_device == 1
        assert 1 in opened

    def test_night_camera_failure_falls_back_to_day(self, system, monkeypatch):
        opened = []

        class FailCapture:
            def isOpened(self):  # noqa: N802
                return False

            def release(self):
                pass

        def factory(device, *args, **kwargs):
            opened.append(device)
            if device == 0:
                return FakeCapture(640, 480, 15)
            return FailCapture()

        monkeypatch.setattr("cv2.VideoCapture", factory)
        system.night_device = 1
        system.set_night_mode(True)
        assert system._active_device == 0
        assert opened.count(0) >= 1

    def test_night_device_active_property(self, system, monkeypatch):
        monkeypatch.setattr("cv2.VideoCapture", lambda *a, **k: FakeCapture(640, 480, 15))
        system.night_device = 1
        system.set_night_mode(True)
        assert system.night_device_active is True
        system.set_night_mode(False)
        assert system.night_device_active is False

    def test_software_enhance_disabled_for_ir_camera(self, system):
        system.night_device = 1
        system.night_software_enhance = False
        system.night_mode = True
        system._active_device = 1
        frame = np.ones((10, 10, 3), dtype=np.uint8) * 128
        result = system._apply_night_mode(frame)
        np.testing.assert_array_equal(result, frame)


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

    def test_config_saved_with_restrictive_permissions(self, system):
        system._save_config()
        mode = oct(Path(system.config_path).stat().st_mode)[-3:]
        assert mode == "600"


class TestRecordingFiles:
    def test_recording_files_includes_fallback_extension(self, system, temp_recordings):
        (temp_recordings / "motion_1.mp4").write_bytes(b"1")
        (temp_recordings / "motion_2.avi").write_bytes(b"2")
        files = {p.name for p in system._recording_files()}
        assert "motion_1.mp4" in files
        assert "motion_2.avi" in files

    def test_recording_files_excludes_fixed_temp(self, system, temp_recordings):
        (temp_recordings / "motion_1.fixed.avi").write_bytes(b"temp")
        assert system._recording_files() == []


class TestTokenRedaction:
    def test_redact_token_masks_token(self, system):
        system.telegram_token = "secret123"
        assert system._redact_token("error secret123") == "error <TOKEN>"
        assert system._redact_token("no token") == "no token"


class TestTelegramMessageStore:
    def test_store_telegram_message_persists_entry(self, system):
        system._store_telegram_message(
            message_id=12345,
            chat_id="42",
            msg_type="video",
            caption="test video",
        )
        messages = system._load_telegram_messages()
        assert len(messages) == 1
        assert messages[0]["message_id"] == 12345
        assert messages[0]["chat_id"] == "42"
        assert messages[0]["type"] == "video"
        assert messages[0]["caption"] == "test video"

    def test_store_limits_entries(self, system):
        for i in range(105):
            system._store_telegram_message(i, "42", "text", max_entries=100)
        messages = system._load_telegram_messages()
        assert len(messages) == 100
        assert messages[0]["message_id"] == 5
        assert messages[-1]["message_id"] == 104

    def test_delete_telegram_message_calls_api_and_removes_entry(self, system, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"ok": True, "result": True}

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        monkeypatch.setattr("requests.post", fake_post)
        system.telegram_token = "token"
        system.telegram_chat_id = "42"
        system._store_telegram_message(999, "42", "photo")

        assert system._delete_telegram_message(999, "42") is True
        assert system._load_telegram_messages() == []
        assert any("deleteMessage" in call[0] for call in calls)
        delete_call = [c for c in calls if "deleteMessage" in c[0]][0]
        assert delete_call[1]["data"] == {"chat_id": "42", "message_id": 999}

    def test_sent_command_lists_messages(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._store_telegram_message(100, "42", "video", "motion_1.avi")
        system._store_telegram_message(101, "42", "photo", "snapshot")
        system._handle_telegram_sent("42")
        assert len(sent) == 1
        assert "100" in sent[0]
        assert "101" in sent[0]
        assert "video" in sent[0]
        assert "photo" in sent[0]

    def test_delete_command_deletes_by_id(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        deleted = []
        monkeypatch.setattr(
            system, "_delete_telegram_message",
            lambda msg_id, chat_id: deleted.append((msg_id, chat_id)) or True,
        )
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/delete 12345", "chat": {"id": 42}}})
        assert deleted == [(12345, "42")]
        assert any("12345 deleted" in msg for msg in sent)

    def test_delete_last_command_deletes_most_recent(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        deleted = []
        monkeypatch.setattr(
            system, "_delete_telegram_message",
            lambda msg_id, chat_id: deleted.append((msg_id, chat_id)) or True,
        )
        system._store_telegram_message(100, "42", "video")
        system._store_telegram_message(101, "42", "photo")
        system._handle_telegram_update({"message": {"text": "/delete last", "chat": {"id": 42}}})
        assert deleted == [(101, "42")]

    def test_delete_command_rejects_invalid_id(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/delete abc", "chat": {"id": 42}}})
        assert any("Invalid message ID" in msg for msg in sent)

    def test_id_command_returns_reply_message_id(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update(
            {
                "message": {
                    "text": "/id",
                    "chat": {"id": 42},
                    "reply_to_message": {"message_id": 98765},
                }
            }
        )
        assert any("98765" in msg for msg in sent)

    def test_id_command_without_reply_shows_usage(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/id", "chat": {"id": 42}}})
        assert any("Reply to a message" in msg for msg in sent)

    def test_telegram_on_command_enables_uploads(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        system.telegram_enabled = False
        system.cfg["telegram"] = {"enabled": False}
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/telegram_on", "chat": {"id": 42}}})
        assert system.telegram_enabled is True
        assert system.cfg["telegram"]["enabled"] is True
        assert any("Telegram auto-uploads enabled" in msg for msg in sent)

    def test_telegram_off_command_disables_uploads(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        system.telegram_enabled = True
        system.cfg["telegram"] = {"enabled": True}
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/telegram_off", "chat": {"id": 42}}})
        assert system.telegram_enabled is False
        assert system.cfg["telegram"]["enabled"] is False
        assert any("Telegram auto-uploads disabled" in msg for msg in sent)

    def test_email_on_command_enables_notifications(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        system.notifications_enabled = False
        system.cfg["notifications"] = {"enabled": False}
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/email_on", "chat": {"id": 42}}})
        assert system.notifications_enabled is True
        assert system.cfg["notifications"]["enabled"] is True
        assert any("Email notifications enabled" in msg for msg in sent)

    def test_email_off_command_disables_notifications(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        system.notifications_enabled = True
        system.cfg["notifications"] = {"enabled": True}
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update({"message": {"text": "/email_off", "chat": {"id": 42}}})
        assert system.notifications_enabled is False
        assert system.cfg["notifications"]["enabled"] is False
        assert any("Email notifications disabled" in msg for msg in sent)

    def test_delete_range_command_deletes_all_ids_in_range(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        deleted = []
        monkeypatch.setattr(
            system, "_delete_telegram_message",
            lambda msg_id, chat_id: deleted.append(msg_id) or True,
        )
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._store_telegram_message(100, "42", "video")
        system._store_telegram_message(105, "42", "video")
        system._handle_telegram_update(
            {"message": {"text": "/delete_range 100 103", "chat": {"id": 42}}}
        )
        assert sorted(deleted) == [100, 101, 102, 103]
        assert any("Deleted 4 messages in range 100-103" in msg for msg in sent)

    def test_delete_range_command_rejects_invalid_range(self, system, monkeypatch):
        system.telegram_chat_id = "42"
        sent = []
        monkeypatch.setattr(
            system, "_send_telegram_message", lambda text, chat_id: sent.append(text)
        )
        system._handle_telegram_update(
            {"message": {"text": "/delete_range 200 100", "chat": {"id": 42}}}
        )
        assert any("min_id must be <= max_id" in msg for msg in sent)
