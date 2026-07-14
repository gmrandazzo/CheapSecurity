#!/usr/bin/env python3
"""
Test manual Telegram video recording duration.
Uses a fake camera so the real service can keep running.
"""

import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from cheapsecurity.cctv import CCTVSystem


class FakeCapture:
    def __init__(self, width=640, height=480, fps=30):
        self._width = width
        self._height = height
        self._fps = fps
        self._props = {}
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)

    def isOpened(self):  # noqa: N802
        return True

    def read(self):
        return True, self._frame.copy()

    def get(self, prop):
        if prop == 3:  # CAP_PROP_FRAME_WIDTH
            return self._width
        if prop == 4:  # CAP_PROP_FRAME_HEIGHT
            return self._height
        if prop == 5:  # CAP_PROP_FPS
            return self._fps
        return self._props.get(prop, -1.0)

    def set(self, prop, value):
        self._props[prop] = value
        return True

    def release(self):
        pass


class TestManualVideo(unittest.TestCase):
    def test_manual_video_duration(self):
        with patch("cv2.VideoCapture", return_value=FakeCapture(640, 480, 30)):
            system = CCTVSystem("config.json")
            # Speed up the test: use low resolution and short duration
            system.width = 640
            system.height = 480
            system.actual_fps = 30
            system.telegram_token = "test"
            system.telegram_chat_id = "12345"
            system.telegram_send_video = False  # avoid real API calls in test

            # Clean recordings
            for p in system.record_dir.glob("*"):
                p.unlink()

            system.start()
            time.sleep(0.5)  # let it start

            # Trigger manual recording for 3 seconds
            requested_seconds = 3
            system._handle_telegram_video(requested_seconds, "12345")
            start = time.time()

            # Wait for recording to start
            while not system.is_recording and (time.time() - start) < 5:
                time.sleep(0.05)
            self.assertTrue(system.is_recording, "Recording did not start")

            recording_started_at = time.time()

            # Wait for recording to finish
            while system.is_recording and (time.time() - recording_started_at) < 10:
                time.sleep(0.05)

            elapsed = time.time() - recording_started_at
            print(f"Requested {requested_seconds}s, recorded for {elapsed:.2f}s")

            system.stop()

            # Find the recording
            recordings = list(system.record_dir.glob("*.avi")) + list(
                system.record_dir.glob("*.mp4")
            )
            self.assertTrue(len(recordings) > 0, "No recording created")
            print(f"Created {recordings[-1].name}")

            # Wall-clock recording time includes startup overhead, so allow
            # a wider tolerance. The important check is the video file below.
            self.assertGreaterEqual(elapsed, requested_seconds - 0.5)
            self.assertLessEqual(elapsed, requested_seconds + 3.0)

            # Verify the saved file's playback duration matches the request.
            video_duration = self._get_video_duration(recordings[-1])
            print(f"Video file playback duration: {video_duration:.2f}s")
            self.assertGreaterEqual(
                video_duration,
                requested_seconds - 0.5,
                f"Video playback duration ({video_duration:.2f}s) much shorter than requested",
            )
            self.assertLessEqual(video_duration, requested_seconds + 1.5)

    def _get_video_duration(self, path: Path) -> float:
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


if __name__ == "__main__":
    unittest.main()
