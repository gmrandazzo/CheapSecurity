#!/usr/bin/env python3
# CheapSecurity - lightweight CCTV system for the Odroid XU4
# Copyright (C) 2026  Marco
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
CheapSecurity CCTV engine.

Captures video from a V4L2 webcam, detects motion by frame differencing,
records clips to disk with a pre-motion buffer, and exposes the live feed
for the web interface.
"""

import contextlib
import json
import logging
import shutil
import smtplib
import ssl
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from types import FrameType
from typing import Optional

import cv2
import numpy as np
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cctv")


class CCTVSystem:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        with open(config_path) as f:
            self.cfg = json.load(f)

        cam = self.cfg["camera"]
        self.device = cam["device"]
        self.width = cam["width"]
        self.height = cam["height"]
        self.fps = cam["fps"]
        self.actual_fps = self.fps
        self.night_mode = cam.get("night_mode", False)
        self.night_mode_fps = cam.get("night_mode_fps", 5)
        self.night_mode_gain = cam.get("night_mode_gain", 255)
        self.night_mode_brightness = cam.get("night_mode_brightness", 200)
        self.night_mode_contrast = cam.get("night_mode_contrast", 200)

        self._normal_brightness: float | None = None
        self._normal_contrast: float | None = None
        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        mot = self.cfg["motion"]
        self.threshold = mot["threshold"]
        self.min_area = mot["min_area"]
        self.blur_size = max(1, mot["blur_size"] // 2 * 2 + 1)  # must be odd
        self.cooldown_seconds = mot["cooldown_seconds"]
        self.motion_scale = max(0.05, min(1.0, mot.get("scale", 1.0)))

        web = self.cfg["web"]
        self.stream_scale = max(0.05, min(1.0, web.get("stream_scale", 1.0)))

        rec = self.cfg["recording"]
        self.record_dir = Path(rec["dir"]).resolve()
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration = rec["max_duration_seconds"]
        self.pre_buffer_seconds = rec["pre_buffer_seconds"]
        self.codec_fourcc = rec["codec"]
        self.video_ext = rec["extension"]

        sto = self.cfg["storage"]
        self.max_age_days = sto["max_age_days"]
        self.max_size_gb = sto["max_size_gb"]
        self.cleanup_interval = sto["cleanup_interval_minutes"]
        self.delete_old_on_startup = sto.get("delete_old_on_startup", False)
        self.emergency_free_space_gb = sto.get("emergency_free_space_gb", 1.0)
        self.emergency_delete_count = sto.get("emergency_delete_count", 4)

        # Notifications
        notif = self.cfg.get("notifications", {})
        self.notifications_enabled = notif.get("enabled", False)
        self.smtp_cfg = notif.get("smtp", {})
        self.mail_from = notif.get("from", self.smtp_cfg.get("username", ""))
        self.mail_to = notif.get("to", [])
        self.mail_subject = notif.get("subject", "CheapSecurity Motion Alert")
        self.min_alert_interval = notif.get("min_interval_minutes", 5) * 60
        self._last_alert_time: float = 0.0

        # Telegram
        tel = self.cfg.get("telegram", {})
        self.telegram_enabled = tel.get("enabled", False)
        self.telegram_token = tel.get("bot_token", "")
        self.telegram_chat_id = tel.get("chat_id", "")
        self.telegram_send_video = tel.get("send_video", True)
        self.telegram_poll_commands = tel.get("poll_commands", False)
        self.min_telegram_interval = tel.get("min_interval_minutes", 5) * 60
        self._last_telegram_time: float = 0.0
        self._telegram_offset: int = 0
        self._telegram_poll_thread: threading.Thread | None = None

        self.cap: cv2.VideoCapture | None = None
        self.writer: cv2.VideoWriter | None = None
        self.recording_path: Path | None = None
        self._writer_fps: float = 0.0
        self._frames_written: int = 0
        self.is_recording = False
        self.last_motion_time: float = 0.0
        self.recording_started: float = 0.0
        self.motion_active = False
        self.running = False
        self.thread: threading.Thread | None = None

        self._manual_record_until: float = 0.0
        self._manual_record_chat_id: str | None = None
        self._manual_recording_active: bool = False

        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cap_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._current_frame: bytes | None = None
        self._jpeg_quality = 75
        self._buffer_jpeg_quality = 85  # lower memory use for pre-motion buffer

        # Measured capture loop FPS (may be lower than camera-reported FPS
        # on slow hardware). Used for video writer so playback duration
        # matches wall-clock recording duration.
        self.measured_fps: float = float(self.fps) if self.fps > 0 else 15.0
        self._frame_times: deque = deque()

        pre_size = int(self.measured_fps * self.pre_buffer_seconds)
        self._pre_buffer: deque = deque(maxlen=max(pre_size, 1))
        self._prev_gray: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        logger.info("Starting CCTV engine...")
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if self.telegram_poll_commands and self.telegram_token and self.telegram_chat_id:
            self._telegram_poll_thread = threading.Thread(
                target=self._telegram_poll_loop, daemon=True
            )
            self._telegram_poll_thread.start()

    def stop(self) -> None:
        logger.info("Stopping CCTV engine...")
        self.running = False
        if self.thread:
            self.thread.join(timeout=5.0)
        if self._telegram_poll_thread:
            self._telegram_poll_thread.join(timeout=2.0)
        self._release_capture()
        self._stop_recording()
        logger.info("CCTV engine stopped.")

    def get_frame(self) -> bytes | None:
        with self._lock:
            return self._current_frame

    def set_night_mode(self, enabled: bool) -> None:
        self.night_mode = enabled
        self.cfg.setdefault("camera", {})["night_mode"] = enabled
        self._save_config()
        logger.info(f"Night mode {'enabled' if enabled else 'disabled'}")
        self._apply_camera_night_mode()

    def set_telegram_enabled(self, enabled: bool) -> None:
        self.telegram_enabled = enabled
        self.cfg.setdefault("telegram", {})["enabled"] = enabled
        self._save_config()
        logger.info(f"Telegram notifications {'enabled' if enabled else 'disabled'}")

    def set_notifications_enabled(self, enabled: bool) -> None:
        self.notifications_enabled = enabled
        self.cfg.setdefault("notifications", {})["enabled"] = enabled
        self._save_config()
        logger.info(f"Notifications {'enabled' if enabled else 'disabled'}")

    def set_auth_enabled(self, enabled: bool) -> None:
        self.cfg.setdefault("web", {}).setdefault("auth", {})["enabled"] = enabled
        self._save_config()
        logger.info(f"Web auth {'enabled' if enabled else 'disabled'}")

    def _save_config(self) -> None:
        with self._config_lock:
            temp_path = Path(self.config_path).with_suffix(".tmp")
            try:
                with open(temp_path, "w") as f:
                    json.dump(self.cfg, f, indent=2)
                temp_path.replace(self.config_path)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")
                if temp_path.exists():
                    temp_path.unlink()

    def list_recordings(self) -> list:
        """Return metadata for all recorded videos, newest first."""
        videos = []
        for path in sorted(self.record_dir.glob(f"*{self.video_ext}"), reverse=True):
            stat = path.stat()
            videos.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "size_human": self._human_size(stat.st_size),
                    "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
        return videos

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _run(self) -> None:
        if not self._open_capture():
            logger.error("Could not open camera. Engine halted.")
            return

        last_cleanup = time.time()
        if self.delete_old_on_startup:
            self._cleanup_storage()

        assert self.cap is not None, "Camera capture not initialized"
        target_frame_interval = 1.0 / self.fps if self.fps > 0 else 1.0 / 15.0
        while self.running:
            loop_start = time.time()

            with self._cap_lock:
                if self.cap is None:
                    break
                ok, frame = self.cap.read()
            if not ok or frame is None:
                logger.warning("Frame capture failed, retrying...")
                time.sleep(0.1)
                continue

            # Enhance low-light visibility when night mode is on
            frame = self._apply_night_mode(frame)

            # Update live JPEG frame for web stream
            self._update_live_frame(frame)

            # Motion detection
            motion = self._detect_motion(frame)
            now = time.time()

            # Update measured FPS from a short sliding window so the video
            # writer uses the real capture rate. This prevents clips from
            # playing back too fast when the loop runs slower than the
            # camera-reported FPS.
            self._frame_times.append(now)
            while self._frame_times and (now - self._frame_times[0]) > 2.0:
                self._frame_times.popleft()
            window = (now - self._frame_times[0]) if self._frame_times else 0.0
            if window > 0.3:
                self.measured_fps = len(self._frame_times) / window
            else:
                self.measured_fps = float(self.fps) if self.fps > 0 else 15.0

            # Manual recording request from Telegram
            with self._state_lock:
                manual_active = now < self._manual_record_until
                self._manual_recording_active = manual_active

            # Update motion state with cooldown
            if motion:
                self.last_motion_time = now
                self.motion_active = True
            elif self.motion_active and (now - self.last_motion_time) <= self.cooldown_seconds:
                # Still inside motion cooldown
                pass
            else:
                self.motion_active = False

            should_record = self.motion_active or manual_active

            if should_record and not self.is_recording:
                self._start_recording(frame)
                if self.motion_active:
                    self._maybe_send_alert(frame)
            elif not should_record and self.is_recording:
                self._stop_recording()

            # Enforce max clip duration
            if self.is_recording and (now - self.recording_started) >= self.max_duration:
                logger.info("Max clip duration reached, closing segment.")
                self._stop_recording()
                if self.motion_active or manual_active:
                    self._start_recording(frame)

            if self.is_recording:
                self._write_frame(frame)
            else:
                self._pre_buffer.append(self._encode_buffer_frame(frame))

            # Periodic storage cleanup
            if (now - last_cleanup) > (self.cleanup_interval * 60):
                self._cleanup_storage()
                last_cleanup = now

            # Throttle loop to configured FPS. Real V4L2 capture blocks until
            # a frame is ready, but this also limits CPU use when capture is
            # fast and keeps the frame rate stable for recordings.
            elapsed = time.time() - loop_start
            sleep_time = target_frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._stop_recording()
        self._release_capture()

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------
    def _open_capture(self) -> bool:
        with self._cap_lock:
            logger.info(f"Opening camera /dev/video{self.device}")
            self.cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                # Fallback to default backend
                self.cap = cv2.VideoCapture(self.device)
            if not self.cap.isOpened():
                logger.error(f"Failed to open camera device {self.device}")
                return False

            # Request MJPG pixel format so high resolutions (e.g. 2K) are available
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)

            actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
            if actual_fps > 0:
                self.actual_fps = actual_fps
            else:
                self.actual_fps = self.fps
            logger.info(
                f"Camera resolution: {actual_width}x{actual_height} @ {self.actual_fps:.1f} fps"
            )

            # Capture current camera defaults before any night-mode changes
            self._normal_brightness = self.cap.get(cv2.CAP_PROP_BRIGHTNESS)
            self._normal_contrast = self.cap.get(cv2.CAP_PROP_CONTRAST)
            logger.info(
                f"Camera defaults — Brightness: {self._normal_brightness}, Contrast: {self._normal_contrast}"
            )

        self._apply_camera_night_mode()
        return True

    def _release_capture(self) -> None:
        with self._cap_lock:
            if self.cap:
                self.cap.release()
                self.cap = None

    def _apply_camera_night_mode(self) -> None:
        """Try to tune V4L2 camera properties for low light."""
        with self._cap_lock:
            if not self.cap or not self.cap.isOpened():
                return

            if self.night_mode:
                logger.info("Applying night mode camera settings...")
                self.cap.set(cv2.CAP_PROP_FPS, self.night_mode_fps)
                self.cap.set(cv2.CAP_PROP_GAIN, self.night_mode_gain)
                self.cap.set(cv2.CAP_PROP_BRIGHTNESS, self.night_mode_brightness)
                self.cap.set(cv2.CAP_PROP_CONTRAST, self.night_mode_contrast)
            else:
                logger.info("Restoring normal camera settings...")
                self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                self.cap.set(cv2.CAP_PROP_GAIN, 0)
                if self._normal_brightness is not None:
                    self.cap.set(cv2.CAP_PROP_BRIGHTNESS, self._normal_brightness)
                if self._normal_contrast is not None:
                    self.cap.set(cv2.CAP_PROP_CONTRAST, self._normal_contrast)

            actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
            actual_gain = self.cap.get(cv2.CAP_PROP_GAIN)
            actual_brightness = self.cap.get(cv2.CAP_PROP_BRIGHTNESS)
            actual_contrast = self.cap.get(cv2.CAP_PROP_CONTRAST)
            if actual_fps > 0:
                self.actual_fps = actual_fps
            logger.info(
                f"Camera settings — FPS: {self.actual_fps:.1f}, "
                f"Gain: {actual_gain}, Brightness: {actual_brightness}, Contrast: {actual_contrast}"
            )

    # ------------------------------------------------------------------
    # Motion detection
    # ------------------------------------------------------------------
    def _detect_motion(self, frame: np.ndarray) -> bool:
        # Downscale for fast motion detection on high-res streams
        if self.motion_scale < 1.0:
            small = cv2.resize(frame, (0, 0), fx=self.motion_scale, fy=self.motion_scale)
        else:
            small = frame

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (self.blur_size, self.blur_size), 0)

        if self._prev_gray is None:
            self._prev_gray = gray
            return False

        diff = cv2.absdiff(self._prev_gray, gray)
        _, thresh = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        self._prev_gray = gray

        # Scale contour area back to full-resolution pixels
        area_factor = 1.0 / (self.motion_scale**2)
        return any(cv2.contourArea(cnt) * area_factor >= self.min_area for cnt in contours)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _start_recording(self, frame: np.ndarray) -> None:
        self._ensure_disk_space()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.recording_path = self.record_dir / f"motion_{timestamp}{self.video_ext}"
        h, w = frame.shape[:2]

        writer = self._create_writer(str(self.recording_path), w, h)
        if writer is None:
            logger.error("Could not create video writer; skipping recording.")
            self.recording_path = None
            return

        self.writer = writer
        self._frames_written = 0
        self.is_recording = True
        self.recording_started = time.time()
        logger.info(f"Recording started: {self.recording_path.name}")

        # Dump pre-buffer for motion-triggered recordings only
        if not self._manual_record_chat_id:
            for encoded in self._pre_buffer:
                decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
                if decoded is not None:
                    self._write_frame(decoded)
        self._pre_buffer.clear()

    def _create_writer(self, path: str, width: int, height: int) -> Optional["cv2.VideoWriter"]:
        # Use measured loop FPS so playback duration matches wall-clock time.
        writer_fps = max(1.0, min(60.0, self.measured_fps))
        self._writer_fps = writer_fps
        fourcc = cv2.VideoWriter_fourcc(*self.codec_fourcc)
        writer = cv2.VideoWriter(path, fourcc, writer_fps, (width, height))
        if writer.isOpened():
            logger.info(f"Video writer created at {writer_fps:.2f} fps")
            return writer

        # Fallbacks for embedded/ARM boards where codec support varies
        for codec, ext in [("MJPG", ".avi"), ("XVID", ".avi")]:
            logger.warning(f"Codec {self.codec_fourcc} failed, trying {codec}")
            fallback_path = path
            if ext != self.video_ext:
                fallback_path = str(Path(path).with_suffix(ext))
            fourcc = cv2.VideoWriter_fourcc(*codec)
            writer = cv2.VideoWriter(fallback_path, fourcc, writer_fps, (width, height))
            if writer.isOpened():
                self.recording_path = Path(fallback_path)
                logger.info(f"Video writer created at {writer_fps:.2f} fps ({codec})")
                return writer

        return None

    def _write_frame(self, frame: np.ndarray) -> None:
        if self.writer:
            self.writer.write(frame)
            self._frames_written += 1

    def _stop_recording(self) -> None:
        if self.writer:
            self.writer.release()
            self.writer = None
        if self.recording_path:
            actual_duration = time.time() - self.recording_started

            # If this was a manual Telegram recording, send it to the requester
            with self._state_lock:
                manual_chat_id = self._manual_record_chat_id
                self._manual_record_chat_id = None

            # Duration fix is only safe for manual recordings that have no
            # pre-buffer. Motion recordings include pre-buffer frames whose
            # original wall-clock time is not part of actual_duration.
            if manual_chat_id:
                self._fix_video_duration(self.recording_path, actual_duration)

            size = self.recording_path.stat().st_size
            logger.info(f"Recording saved: {self.recording_path.name} ({self._human_size(size)})")

            if manual_chat_id:
                try:
                    self._send_telegram_video(self.recording_path, chat_id=manual_chat_id)
                except Exception as e:
                    logger.error(f"Failed to send manual Telegram video: {e}")
            else:
                self._maybe_send_telegram(self.recording_path)

            self.recording_path = None
        self.is_recording = False
        self.motion_active = False

    def _fix_video_duration(self, path: Path, actual_duration: float) -> None:
        """Adjust container frame rate so playback length matches wall-clock time.

        OpenCV's VideoWriter uses the loop's estimated FPS when the file is
        created. If the capture rate drops during recording (e.g., slow disk
        I/O), the saved file can play back too fast. Rewriting the container
        header with the actual FPS (frame_count / actual_duration) fixes this.
        """
        if actual_duration <= 0 or self._frames_written <= 0 or self._writer_fps <= 0:
            return

        playback_duration = self._frames_written / self._writer_fps
        drift = abs(playback_duration - actual_duration)
        # Only fix if the drift is meaningful (more than half a second or 10%)
        if drift < 0.5 and drift / max(actual_duration, 1.0) < 0.1:
            return

        correct_fps = self._frames_written / actual_duration
        correct_fps = max(1.0, min(60.0, correct_fps))

        if not shutil.which("ffmpeg"):
            logger.warning(
                f"Video duration drift ({drift:.2f}s) but ffmpeg not available; "
                f"playback may be too fast or slow."
            )
            return

        fixed_path = path.with_suffix(".fixed" + path.suffix)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-r",
                    str(correct_fps),
                    "-c:v",
                    "copy",
                    str(fixed_path),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            fixed_path.replace(path)
            logger.info(
                f"Fixed video FPS from {self._writer_fps:.2f} to {correct_fps:.2f} "
                f"({self._frames_written} frames / {actual_duration:.2f}s)"
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to fix video duration: {e.stderr.decode(errors='ignore')}")
        except Exception as e:
            logger.error(f"Failed to fix video duration: {e}")

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def _apply_night_mode(self, frame: np.ndarray) -> np.ndarray:
        """Enhance low-light visibility using CLAHE on the L channel."""
        if not self.night_mode:
            return frame
        lab: np.ndarray = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        lightness, a, b = cv2.split(lab)
        lightness = self._clahe.apply(lightness)
        lab = cv2.merge([lightness, a, b])
        return np.asarray(cv2.cvtColor(lab, cv2.COLOR_LAB2BGR))

    def _encode_buffer_frame(self, frame: np.ndarray) -> bytes:
        _, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._buffer_jpeg_quality]
        )
        return bytes(buf.tobytes())

    def _update_live_frame(self, frame: np.ndarray) -> None:
        if self.stream_scale < 1.0:
            stream_frame = cv2.resize(frame, (0, 0), fx=self.stream_scale, fy=self.stream_scale)
        else:
            stream_frame = frame
        _, buf = cv2.imencode(
            ".jpg", stream_frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
        )
        with self._lock:
            self._current_frame = buf.tobytes()

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    def _maybe_send_alert(self, frame: np.ndarray) -> None:
        if not self.notifications_enabled:
            return
        if not self.mail_to or not self.smtp_cfg.get("server"):
            return

        now = time.time()
        if (now - self._last_alert_time) < self.min_alert_interval:
            return
        self._last_alert_time = now

        # Send in background so motion capture is not blocked
        threading.Thread(
            target=self._send_alert_email,
            args=(frame.copy(),),
            daemon=True,
        ).start()

    def _send_alert_email(self, frame: np.ndarray) -> None:
        try:
            _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            image_bytes = buf.tobytes()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            msg = MIMEMultipart()
            msg["From"] = self.mail_from
            msg["To"] = ", ".join(self.mail_to)
            msg["Subject"] = self.mail_subject

            body = f"Motion was detected by CheapSecurity at {timestamp}.\n\nA recording has been started."
            msg.attach(MIMEText(body, "plain"))

            part = MIMEBase("application", "octet-stream")
            part.set_payload(image_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename= motion_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg",
            )
            msg.attach(part)

            server = self.smtp_cfg.get("server")
            port = self.smtp_cfg.get("port", 465)
            username = self.smtp_cfg.get("username", "")
            password = self.smtp_cfg.get("password", "")
            use_tls = self.smtp_cfg.get("use_tls", True)

            if use_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(server, port, context=context, timeout=30) as smtp:
                    if username:
                        smtp.login(username, password)
                    smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())
            else:
                with smtplib.SMTP(server, port, timeout=30) as smtp:
                    if username:
                        smtp.login(username, password)
                    smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())

            logger.info(f"Alert email sent to {self.mail_to}")
        except Exception as e:
            logger.error(f"Failed to send alert email: {e}")

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    def _maybe_send_telegram(self, video_path: Path) -> None:
        if not self.telegram_enabled:
            return
        if not self.telegram_token or not self.telegram_chat_id:
            return
        if not self.telegram_send_video:
            return
        if not video_path or not video_path.is_file():
            return

        now = time.time()
        if (now - self._last_telegram_time) < self.min_telegram_interval:
            return
        self._last_telegram_time = now

        threading.Thread(
            target=self._send_telegram_video,
            args=(video_path,),
            daemon=True,
        ).start()

    def _send_telegram_video(self, video_path: Path, chat_id: str | None = None) -> None:
        try:
            target_chat = chat_id or self.telegram_chat_id
            if not target_chat:
                return

            url = f"https://api.telegram.org/bot{self.telegram_token}/sendVideo"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            caption = f"🎥 Motion detected at {timestamp}\nFile: {video_path.name}"

            with open(video_path, "rb") as f:
                files = {"video": (video_path.name, f, "video/avi")}
                data = {"chat_id": target_chat, "caption": caption}
                response = requests.post(url, data=data, files=files, timeout=120)

            if response.status_code != 200:
                raise RuntimeError(f"Telegram API error {response.status_code}: {response.text}")
            logger.info(f"Telegram video sent: {video_path.name}")
        except Exception as e:
            logger.error(f"Failed to send Telegram video: {e}")
            raise

    def _send_telegram_photo(self, image_bytes: bytes, chat_id: str, caption: str = "") -> None:
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
            files = {"photo": ("snapshot.jpg", image_bytes, "image/jpeg")}
            data = {"chat_id": chat_id, "caption": caption}
            response = requests.post(url, data=data, files=files, timeout=60)
            if response.status_code != 200:
                raise RuntimeError(f"Telegram API error {response.status_code}: {response.text}")
            logger.info(f"Telegram snapshot sent to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram snapshot: {e}")
            raise

    def _send_telegram_message(self, text: str, chat_id: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {"chat_id": chat_id, "text": text}
            response = requests.post(url, data=data, timeout=30)
            if response.status_code != 200:
                logger.error(f"Telegram message error {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def _telegram_poll_loop(self) -> None:
        logger.info("Starting Telegram command polling...")
        while self.running and self.telegram_poll_commands:
            if not self.telegram_enabled:
                time.sleep(5)
                continue
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/getUpdates"
                params = {"offset": self._telegram_offset + 1, "limit": 10}
                response = requests.get(url, params=params, timeout=30)
                if response.status_code != 200:
                    time.sleep(5)
                    continue

                data = response.json()
                if not data.get("ok"):
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    self._telegram_offset = max(self._telegram_offset, update["update_id"])
                    self._handle_telegram_update(update)
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
            time.sleep(2)
        logger.info("Telegram command polling stopped.")

    def _handle_telegram_update(self, update: dict) -> None:
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        if not text or not chat_id:
            return

        # Only respond to the configured chat
        if chat_id != str(self.telegram_chat_id):
            self._send_telegram_message("You are not authorized to use this bot.", chat_id)
            return

        cmd = text.lower().split()
        if not cmd:
            return

        if cmd[0] == "/snapshot":
            self._handle_telegram_snapshot(chat_id)
        elif cmd[0] == "/video":
            seconds = 10
            if len(cmd) > 1:
                with contextlib.suppress(ValueError):
                    seconds = int(cmd[1])
            self._handle_telegram_video(seconds, chat_id)
        elif cmd[0] == "/help":
            self._send_telegram_message(
                "Available commands:\n"
                "/snapshot - get current picture\n"
                "/video <seconds> - record and send a video (1-60s, default 10)\n"
                "/help - show this help",
                chat_id,
            )
        else:
            self._send_telegram_message("Unknown command. Use /help.", chat_id)

    def _handle_telegram_snapshot(self, chat_id: str) -> None:
        try:
            frame = self.get_frame()
            if frame is None:
                self._send_telegram_message("No camera frame available.", chat_id)
                return
            caption = f"📸 Snapshot at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            threading.Thread(
                target=self._send_telegram_photo,
                args=(frame, chat_id, caption),
                daemon=True,
            ).start()
        except Exception as e:
            logger.error(f"Failed to handle snapshot command: {e}")
            self._send_telegram_message("Failed to take snapshot.", chat_id)

    def _handle_telegram_video(self, seconds: int, chat_id: str) -> None:
        try:
            seconds = max(1, min(60, seconds))
            with self._state_lock:
                manual_active = time.time() < self._manual_record_until

                # Motion recording has priority: do not interrupt or redirect it
                if self.is_recording and not manual_active:
                    self._send_telegram_message(
                        "A video is already recording due to detected motion. "
                        "It will be uploaded automatically to Telegram when the motion stops.",
                        chat_id,
                    )
                    return

                if self.is_recording and manual_active:
                    self._send_telegram_message(
                        f"Already recording on your request. Extending by {seconds} seconds.",
                        chat_id,
                    )
                else:
                    self._send_telegram_message(f"Recording {seconds} seconds video...", chat_id)

                self._manual_record_until = time.time() + seconds
                self._manual_record_chat_id = chat_id
                self._manual_recording_active = True
        except Exception as e:
            logger.error(f"Failed to handle video command: {e}")
            self._send_telegram_message("Failed to start recording.", chat_id)

    # ------------------------------------------------------------------
    # Storage cleanup
    # ------------------------------------------------------------------
    def _ensure_disk_space(self) -> None:
        """Delete the oldest N recordings if free disk space is low."""
        free_bytes = shutil.disk_usage(self.record_dir).free
        free_gb = free_bytes / (1024**3)
        if free_gb >= self.emergency_free_space_gb:
            return

        logger.warning(
            f"Low disk space: {free_gb:.2f} GB free. "
            f"Deleting up to {self.emergency_delete_count} oldest recordings."
        )
        files = sorted(
            (p for p in self.record_dir.glob(f"*{self.video_ext}") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        deleted = 0
        for path in files[: self.emergency_delete_count]:
            if path.exists():
                logger.info(f"Emergency deletion: {path.name}")
                path.unlink(missing_ok=True)
                deleted += 1

        free_bytes_after = shutil.disk_usage(self.record_dir).free
        logger.info(
            f"Deleted {deleted} recordings. " f"Free space: {free_bytes_after / (1024 ** 3):.2f} GB"
        )

    def _cleanup_storage(self) -> None:
        logger.info("Running storage cleanup...")
        now = datetime.now()
        files = sorted(
            (p for p in self.record_dir.glob(f"*{self.video_ext}") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        total_size = 0
        for path in files:
            stat = path.stat()
            age_days = (now - datetime.fromtimestamp(stat.st_mtime)).total_seconds() / 86400
            if age_days > self.max_age_days:
                logger.info(f"Deleting old recording: {path.name}")
                path.unlink(missing_ok=True)
                continue
            total_size += stat.st_size

        max_bytes = self.max_size_gb * (1024**3)
        if total_size > max_bytes:
            # Delete oldest until under limit
            for path in reversed(files):
                if path.exists():
                    total_size -= path.stat().st_size
                    logger.info(f"Deleting recording to free space: {path.name}")
                    path.unlink(missing_ok=True)
                    if total_size <= max_bytes:
                        break

        logger.info(f"Storage usage: {self._human_size(total_size)}")

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        value = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024.0:
                return f"{value:.1f} {unit}"
            value /= 1024.0
        return f"{value:.1f} PB"


if __name__ == "__main__":
    import signal

    system = CCTVSystem()

    def _shutdown(signum: int, frame: FrameType | None) -> None:
        system.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    system.start()
    try:
        while system.running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        system.stop()
