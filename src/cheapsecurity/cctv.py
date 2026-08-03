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
import os
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
from typing import Any, Optional

import cv2
import numpy as np
import pyzipper
import requests

from cheapsecurity.rtsp import RTSPPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cctv")

# Night-mode enhancement profiles. Lower clip limits and larger tiles reduce
# noise amplification; lower gamma values lift shadows more aggressively.
_NIGHT_MODE_PROFILES: dict[str, dict[str, float | int]] = {
    "low": {"gamma": 0.65, "clip_limit": 1.0, "tile_grid": 16},
    "normal": {"gamma": 0.50, "clip_limit": 2.0, "tile_grid": 12},
    "aggressive": {"gamma": 0.35, "clip_limit": 3.0, "tile_grid": 8},
}


class CCTVSystem:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        with open(config_path) as f:
            self.cfg = json.load(f)

        cam = self.cfg["camera"]
        self.device = cam["device"]
        self.width = self._parse_dim(cam.get("width", 2560))
        self.height = self._parse_dim(cam.get("height", 1440))
        self.fps = cam["fps"]
        self.actual_fps = self.fps
        self.night_mode = cam.get("night_mode", False)
        self.night_mode_fps = cam.get("night_mode_fps", 5)
        self.night_mode_gain = cam.get("night_mode_gain", 255)
        self.night_mode_brightness = cam.get("night_mode_brightness", 200)
        self.night_mode_contrast = cam.get("night_mode_contrast", 200)
        strength = cam.get("night_mode_strength", "normal")
        self.night_mode_strength: str = (
            strength.lower().strip() if isinstance(strength, str) else "normal"
        )
        if self.night_mode_strength not in _NIGHT_MODE_PROFILES:
            self.night_mode_strength = "normal"

        # Optional second camera used when night mode is enabled.
        self.night_device: int | str | None = cam.get("night_device")
        self.night_device_width: int = self._parse_dim(cam.get("night_device_width", self.width))
        self.night_device_height: int = self._parse_dim(cam.get("night_device_height", self.height))
        self.night_device_fps: int = cam.get("night_device_fps", self.fps)
        self.night_software_enhance: bool = cam.get("night_software_enhance", True)
        self._active_device: int | str = self.device

        self._normal_brightness: float | None = None
        self._normal_contrast: float | None = None

        mot = self.cfg["motion"]
        self.threshold = mot["threshold"]
        self.min_area = mot["min_area"]
        self.blur_size = max(1, mot["blur_size"] // 2 * 2 + 1)  # must be odd
        self.cooldown_seconds = mot["cooldown_seconds"]
        self.motion_scale = max(0.05, min(1.0, mot.get("scale", 1.0)))

        web = self.cfg["web"]
        self.stream_scale = max(0.05, min(1.0, web.get("stream_scale", 1.0)))

        rtsp = self.cfg.get("rtsp", {})
        self._rtsp_publisher = RTSPPublisher(
            rtsp,
            web.get("host", "127.0.0.1"),
            web.get("port", 5000),
        )

        rec = self.cfg["recording"]
        self.record_dir = Path(rec["dir"]).resolve()
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration = rec["max_duration_seconds"]
        self.pre_buffer_seconds = rec["pre_buffer_seconds"]
        self.codec_fourcc = rec["codec"]
        self.video_ext = rec["extension"]
        self._recording_extensions = {self.video_ext, ".avi", ".mp4"}

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

        # Cloud Storage (Google Drive & OneDrive)
        cloud_cfg = self.cfg.get("cloud", {})
        gdrive_cfg = cloud_cfg.get("google_drive", {})
        self.gdrive_enabled = gdrive_cfg.get("enabled", False)
        self.gdrive_client_id = gdrive_cfg.get("client_id", "")
        self.gdrive_client_secret = gdrive_cfg.get("client_secret", "")
        self.gdrive_refresh_token = gdrive_cfg.get("refresh_token", "")
        self.gdrive_folder_id = gdrive_cfg.get("folder_id", "")

        onedrive_cfg = cloud_cfg.get("onedrive", {})
        self.onedrive_enabled = onedrive_cfg.get("enabled", False)
        self.onedrive_client_id = onedrive_cfg.get("client_id", "")
        self.onedrive_client_secret = onedrive_cfg.get("client_secret", "")
        self.onedrive_refresh_token = onedrive_cfg.get("refresh_token", "")
        self.onedrive_folder_path = onedrive_cfg.get("folder_path", "CheapSecurity")

        # Encryption (AES-256 ZIP)
        enc_cfg = self.cfg.get("encryption", {})
        self.encryption_passphrase: str = enc_cfg.get("passphrase", "")
        self.encrypt_telegram: bool = enc_cfg.get("telegram", False)
        self.encrypt_gdrive: bool = enc_cfg.get("google_drive", False)
        self.encrypt_onedrive: bool = enc_cfg.get("onedrive", False)

        self._load_env_secrets()

        self.cap: cv2.VideoCapture | None = None
        self.writer: cv2.VideoWriter | None = None
        self.recording_path: Path | None = None
        self._writer_fps: float = 0.0
        self._frames_written: int = 0
        self._consecutive_frame_failures: int = 0
        self._reconnect_backoff: float = 1.0
        self.is_recording = False
        self.last_motion_time: float = 0.0
        self.recording_started: float = 0.0
        self.motion_active = False
        self.running = False
        self.thread: threading.Thread | None = None

        self._manual_record_until: float = 0.0
        self._manual_record_chat_id: str | None = None
        self._manual_recording_active: bool = False
        self._manual_finalize_done = threading.Event()

        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cap_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._telegram_store_lock = threading.Lock()
        self._telegram_store_path: Path = self.record_dir / ".telegram_messages.json"
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

    def _load_env_secrets(self) -> None:
        """Override sensitive config values with environment variables.

        Environment-supplied secrets take precedence over config.json and are
        never written back to disk. Leave the config file entries empty or set
        them to dummy values when using this feature.
        """
        self.telegram_token = os.environ.get(
            "CHEAPSECURITY_TELEGRAM_BOT_TOKEN", self.telegram_token
        )
        self.telegram_chat_id = os.environ.get(
            "CHEAPSECURITY_TELEGRAM_CHAT_ID", self.telegram_chat_id
        )

        smtp_password = os.environ.get("CHEAPSECURITY_SMTP_PASSWORD")
        if smtp_password:
            self.smtp_cfg = dict(self.smtp_cfg)
            self.smtp_cfg["password"] = smtp_password

        self.gdrive_client_id = os.environ.get(
            "CHEAPSECURITY_GDRIVE_CLIENT_ID", self.gdrive_client_id
        )
        self.gdrive_client_secret = os.environ.get(
            "CHEAPSECURITY_GDRIVE_CLIENT_SECRET", self.gdrive_client_secret
        )
        self.gdrive_refresh_token = os.environ.get(
            "CHEAPSECURITY_GDRIVE_REFRESH_TOKEN", self.gdrive_refresh_token
        )

        self.onedrive_client_id = os.environ.get(
            "CHEAPSECURITY_ONEDRIVE_CLIENT_ID", self.onedrive_client_id
        )
        self.onedrive_client_secret = os.environ.get(
            "CHEAPSECURITY_ONEDRIVE_CLIENT_SECRET", self.onedrive_client_secret
        )
        self.onedrive_refresh_token = os.environ.get(
            "CHEAPSECURITY_ONEDRIVE_REFRESH_TOKEN", self.onedrive_refresh_token
        )

        self.encryption_passphrase = os.environ.get(
            "CHEAPSECURITY_ENCRYPTION_PASSPHRASE", self.encryption_passphrase
        )

        web_auth_password = os.environ.get("CHEAPSECURITY_WEB_AUTH_PASSWORD")
        if web_auth_password:
            self.cfg.setdefault("web", {}).setdefault("auth", {})[
                "password"
            ] = web_auth_password

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        logger.info("Starting CCTV engine...")
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._rtsp_publisher.start()
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
        self._rtsp_publisher.stop()
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
        if self.night_device is not None:
            self._switch_camera()
        else:
            self._apply_camera_night_mode()

    def set_night_mode_strength(self, strength: str) -> None:
        """Set the night-mode enhancement strength (low, normal, aggressive)."""
        strength = (strength or "normal").lower().strip()
        if strength not in _NIGHT_MODE_PROFILES:
            strength = "normal"
        self.night_mode_strength = strength
        self.cfg.setdefault("camera", {})["night_mode_strength"] = strength
        self._save_config()
        logger.info(f"Night mode strength set to {strength}")

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

    def set_gdrive_enabled(self, enabled: bool) -> None:
        self.gdrive_enabled = enabled
        self.cfg.setdefault("cloud", {}).setdefault("google_drive", {})["enabled"] = enabled
        self._save_config()
        logger.info(f"Google Drive uploads {'enabled' if enabled else 'disabled'}")

    def set_onedrive_enabled(self, enabled: bool) -> None:
        self.onedrive_enabled = enabled
        self.cfg.setdefault("cloud", {}).setdefault("onedrive", {})["enabled"] = enabled
        self._save_config()
        logger.info(f"OneDrive uploads {'enabled' if enabled else 'disabled'}")

    def set_encryption_passphrase(self, passphrase: str) -> None:
        self.encryption_passphrase = passphrase
        self.cfg.setdefault("encryption", {})["passphrase"] = passphrase
        self._save_config()
        logger.info("Encryption passphrase updated.")

    def set_encrypt_telegram(self, enabled: bool) -> None:
        self.encrypt_telegram = enabled
        self.cfg.setdefault("encryption", {})["telegram"] = enabled
        self._save_config()
        logger.info(f"Telegram encryption {'enabled' if enabled else 'disabled'}")

    def set_encrypt_gdrive(self, enabled: bool) -> None:
        self.encrypt_gdrive = enabled
        self.cfg.setdefault("encryption", {})["google_drive"] = enabled
        self._save_config()
        logger.info(f"Google Drive encryption {'enabled' if enabled else 'disabled'}")

    def set_encrypt_onedrive(self, enabled: bool) -> None:
        self.encrypt_onedrive = enabled
        self.cfg.setdefault("encryption", {})["onedrive"] = enabled
        self._save_config()
        logger.info(f"OneDrive encryption {'enabled' if enabled else 'disabled'}")

    @property
    def night_device_active(self) -> bool:
        """True when the optional IR/night camera is currently in use."""
        return self.night_device is not None and self._active_device == self.night_device

    def _save_config(self) -> None:
        with self._config_lock:
            temp_path = Path(self.config_path).with_suffix(".tmp")
            try:
                with open(temp_path, "w") as f:
                    json.dump(self.cfg, f, indent=2)
                temp_path.replace(self.config_path)
                Path(self.config_path).chmod(0o600)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")
                if temp_path.exists():
                    temp_path.unlink()

    def _recording_files(self) -> list:
        """Return existing recording files, excluding temp/fix files."""
        files = []
        for path in self.record_dir.iterdir():
            try:
                if (
                    path.is_file()
                    and path.suffix in self._recording_extensions
                    and not path.name.endswith(".fixed" + path.suffix)
                ):
                    files.append(path)
            except OSError:
                continue
        return files

    def list_recordings(self) -> list:
        """Return metadata for all recorded videos, newest first."""
        videos = []
        for path in sorted(self._recording_files(), reverse=True):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
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

        target_frame_interval = 1.0 / self.fps if self.fps > 0 else 1.0 / 15.0
        while self.running:
            loop_start = time.time()

            # Reconnect if the camera was released (e.g. by too many frame failures)
            if self.cap is None:
                # Give the kernel time to re-enumerate the USB camera before retrying.
                time.sleep(self._reconnect_backoff)
                with self._cap_lock:
                    if self.cap is not None:
                        continue
                if self._open_capture():
                    logger.info("Camera reconnected.")
                    self._reconnect_backoff = 1.0
                else:
                    logger.warning(
                        f"Camera reconnect failed; retrying in {self._reconnect_backoff:.1f}s..."
                    )
                    self._reconnect_backoff = min(self._reconnect_backoff * 2, 30.0)
                continue

            with self._cap_lock:
                if self.cap is None:
                    continue
                ok, frame = self.cap.read()
            if not ok or frame is None:
                self._consecutive_frame_failures += 1
                logger.warning(
                    f"Frame capture failed ({self._consecutive_frame_failures} consecutive), retrying..."
                )
                if self._consecutive_frame_failures >= 10:
                    logger.error("Too many frame failures; releasing camera for reconnect.")
                    self._release_capture()
                    self._consecutive_frame_failures = 0
                time.sleep(0.1)
                continue
            self._consecutive_frame_failures = 0

            # Some IR cameras return a grayscale frame; normalize to BGR so
            # the rest of the pipeline (motion detection, recording, stream)
            # always works with 3 channels.
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

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
    @staticmethod
    def _parse_dim(val: int | str | None) -> int:
        """Parse dimension settings (width/height). Return 0 for auto/max."""
        if val is None or val == 0:
            return 0
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("auto", "max", "0", ""):
                return 0
            with contextlib.suppress(ValueError):
                return int(s)
        if isinstance(val, int):
            return max(0, val)
        return 0

    def _open_device(
        self,
        device: int | str,
        width: int,
        height: int,
        fps: int,
        *,
        save_defaults: bool = False,
    ) -> cv2.VideoCapture | None:
        """Open a single V4L2 device with the requested resolution and FPS.

        Returns the capture object on success, or None if the device could not
        be opened. The caller is responsible for assigning the result to
        ``self.cap`` under ``self._cap_lock``.
        """
        logger.info(f"Opening camera /dev/video{device}")
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            # Fallback to default backend
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            logger.error(f"Failed to open camera device {device}")
            return None

        # If width or height is <= 0 (auto/max), request oversized dimensions so
        # V4L2 automatically clamps to the maximum supported hardware resolution.
        req_w = 10000 if width <= 0 else width
        req_h = 10000 if height <= 0 else height

        # Request MJPG pixel format so high resolutions (e.g. 2K/4K) are available
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
        cap.set(cv2.CAP_PROP_FPS, fps)

        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        if actual_fps > 0:
            self.actual_fps = actual_fps
        else:
            self.actual_fps = fps
        if width <= 0 or height <= 0:
            logger.info(
                f"Auto-detected max camera resolution: {actual_width}x{actual_height} @ {self.actual_fps:.1f} fps"
            )
        else:
            logger.info(
                f"Camera resolution: {actual_width}x{actual_height} @ {self.actual_fps:.1f} fps"
            )

        if save_defaults:
            # Capture current camera defaults before any night-mode changes
            self._normal_brightness = cap.get(cv2.CAP_PROP_BRIGHTNESS)
            self._normal_contrast = cap.get(cv2.CAP_PROP_CONTRAST)
            logger.info(
                f"Camera defaults — Brightness: {self._normal_brightness}, "
                f"Contrast: {self._normal_contrast}"
            )

        return cap

    def _open_capture(self) -> bool:
        """Open the camera matching current mode, falling back to alternate if needed."""
        if self.night_mode and self.night_device is not None:
            primary_device = self.night_device
            primary_w, primary_h, primary_fps = (
                self.night_device_width,
                self.night_device_height,
                self.night_device_fps,
            )
            primary_defaults = False
            fallback_device: int | str | None = self.device
            fallback_w, fallback_h, fallback_fps = self.width, self.height, self.fps
            fallback_defaults = True
        else:
            primary_device = self.device
            primary_w, primary_h, primary_fps = self.width, self.height, self.fps
            primary_defaults = True
            fallback_device = self.night_device
            fallback_w, fallback_h, fallback_fps = (
                self.night_device_width,
                self.night_device_height,
                self.night_device_fps,
            )
            fallback_defaults = False

        cap = self._open_device(
            primary_device, primary_w, primary_h, primary_fps, save_defaults=primary_defaults
        )
        active_dev = primary_device

        if cap is None and fallback_device is not None:
            logger.warning(
                f"Camera device {primary_device} failed to open; trying fallback device {fallback_device}."
            )
            cap = self._open_device(
                fallback_device,
                fallback_w,
                fallback_h,
                fallback_fps,
                save_defaults=fallback_defaults,
            )
            active_dev = fallback_device

        if cap is None:
            return False

        with self._cap_lock:
            self.cap = cap
            self._active_device = active_dev
        self._apply_camera_night_mode()
        return True

    def _switch_camera(self) -> bool:
        """Release the current camera and open the one for the current mode.

        If the requested camera fails to open, fall back to the alternate
        camera so the system keeps running.
        """
        if self.is_recording:
            logger.info("Stopping recording before camera switch.")
            self._stop_recording()

        if self.night_mode and self.night_device is not None:
            target_device = self.night_device
            width = self.night_device_width
            height = self.night_device_height
            fps = self.night_device_fps
            save_defaults = False
            label = "night"
            fallback_device: int | str | None = self.device
            fallback_w, fallback_h, fallback_fps = self.width, self.height, self.fps
            fallback_defaults = True
            fallback_label = "day"
        else:
            target_device = self.device
            width = self.width
            height = self.height
            fps = self.fps
            save_defaults = True
            label = "day"
            fallback_device = self.night_device
            fallback_w, fallback_h, fallback_fps = (
                self.night_device_width,
                self.night_device_height,
                self.night_device_fps,
            )
            fallback_defaults = False
            fallback_label = "night"

        self._release_capture()
        cap = self._open_device(target_device, width, height, fps, save_defaults=save_defaults)

        if cap is None and fallback_device is not None:
            logger.warning(
                f"{label.capitalize()} camera failed to open; falling back to {fallback_label} camera."
            )
            target_device = fallback_device
            width = fallback_w
            height = fallback_h
            fps = fallback_fps
            save_defaults = fallback_defaults
            label = fallback_label
            cap = self._open_device(target_device, width, height, fps, save_defaults=save_defaults)

        if cap is None:
            logger.error("Camera switch failed; waiting for reconnect loop.")
            return False

        with self._cap_lock:
            self.cap = cap
            self._active_device = target_device
        logger.info(f"Switched to {label} camera (/dev/video{target_device}).")
        self._apply_camera_night_mode()
        return True

    def _release_capture(self) -> None:
        with self._cap_lock:
            if self.cap:
                self.cap.release()
                self.cap = None
            self._prev_gray = None

    def _apply_camera_night_mode(self) -> None:
        """Try to tune V4L2 camera properties for low light.

        Skip the gain/brightness tweaks when the active device is the optional
        IR/night camera, because those controls are usually not meaningful on
        IR modules.
        """
        with self._cap_lock:
            if not self.cap or not self.cap.isOpened():
                return

            if self._active_device == self.night_device:
                logger.info("Night camera active; skipping V4L2 gain/brightness tuning.")
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

        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return False

        diff = cv2.absdiff(self._prev_gray, gray)
        _, thresh = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)  # type: ignore[call-overload]

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
        with self._state_lock:
            manual_chat_id = self._manual_record_chat_id
        if not manual_chat_id:
            for encoded in self._pre_buffer:
                decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
                if decoded is not None:
                    self._write_frame(decoded)
        self._pre_buffer.clear()

    def _create_writer(self, path: str, width: int, height: int) -> Optional["cv2.VideoWriter"]:
        # Use measured loop FPS so playback duration matches wall-clock time.
        writer_fps = max(1.0, min(60.0, self.measured_fps))
        self._writer_fps = writer_fps
        fourcc = cv2.VideoWriter.fourcc(*self.codec_fourcc)
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
            fourcc = cv2.VideoWriter.fourcc(*codec)
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

            if manual_chat_id:
                # Finalize manual recordings in the background so the capture
                # loop is not blocked by ffmpeg + Telegram upload.
                frames_written = self._frames_written
                writer_fps = self._writer_fps
                path = self.recording_path
                self._manual_finalize_done.clear()
                threading.Thread(
                    target=self._finalize_manual_recording,
                    args=(path, manual_chat_id, actual_duration, frames_written, writer_fps),
                    daemon=True,
                ).start()
            else:
                size = self.recording_path.stat().st_size
                logger.info(
                    f"Recording saved: {self.recording_path.name} ({self._human_size(size)})"
                )
                self._maybe_send_telegram(self.recording_path)
                self._maybe_upload_cloud(self.recording_path)

            self.recording_path = None
        self.is_recording = False

    def _fix_video_duration(
        self,
        path: Path,
        actual_duration: float,
        frames_written: int | None = None,
        writer_fps: float | None = None,
    ) -> None:
        """Adjust container frame rate so playback length matches wall-clock time.

        OpenCV's VideoWriter uses the loop's estimated FPS when the file is
        created. If the capture rate drops during recording (e.g., slow disk
        I/O), the saved file can play back too fast. Rewriting the container
        header with the actual FPS (frame_count / actual_duration) fixes this.
        """
        frames_written = frames_written if frames_written is not None else self._frames_written
        writer_fps = writer_fps if writer_fps is not None else self._writer_fps
        if actual_duration <= 0 or frames_written <= 0 or writer_fps <= 0:
            return

        playback_duration = frames_written / writer_fps
        drift = abs(playback_duration - actual_duration)
        # Only fix if the drift is meaningful (more than half a second or 10%)
        if drift < 0.5 and drift / max(actual_duration, 1.0) < 0.1:
            return

        correct_fps = frames_written / actual_duration
        correct_fps = max(1.0, min(60.0, correct_fps))

        if not shutil.which("ffmpeg"):
            logger.warning(
                f"Video duration drift ({drift:.2f}s) but ffmpeg not available; "
                f"playback may be too fast or slow."
            )
            return

        fixed_path = path.with_suffix(".fixed" + path.suffix)
        try:
            # Re-encode with a filter that forces evenly spaced timestamps at the
            # calculated frame rate. A plain ``-r`` with ``-c:v copy`` can drop
            # or duplicate frames depending on the input/output rate ratio, so
            # we rewrite timestamps while keeping the same MJPEG codec.
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-vf",
                    f"setpts=N/{correct_fps}/TB",
                    "-r",
                    str(correct_fps),
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "3",
                    str(fixed_path),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            fixed_path.replace(path)
            logger.info(
                f"Fixed video FPS from {writer_fps:.2f} to {correct_fps:.2f} "
                f"({frames_written} frames / {actual_duration:.2f}s)"
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to fix video duration: {e.stderr.decode(errors='ignore')}")
            with contextlib.suppress(Exception):
                fixed_path.unlink()
        except Exception as e:
            logger.error(f"Failed to fix video duration: {e}")
            with contextlib.suppress(Exception):
                fixed_path.unlink()

    def _finalize_manual_recording(
        self,
        path: Path,
        chat_id: str,
        actual_duration: float,
        frames_written: int,
        writer_fps: float,
    ) -> None:
        """Fix duration and upload a manual recording without blocking the main loop."""
        try:
            self._fix_video_duration(path, actual_duration, frames_written, writer_fps)
            size = path.stat().st_size
            logger.info(f"Recording saved: {path.name} ({self._human_size(size)})")
            try:
                self._send_telegram_video(path, chat_id=chat_id)
                self._maybe_upload_cloud(path)
            except Exception as e:
                logger.error(f"Failed to send manual Telegram video: {self._redact_token(str(e))}")
        finally:
            self._manual_finalize_done.set()

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def _apply_night_mode(self, frame: np.ndarray) -> np.ndarray:
        """Enhance low-light visibility using gamma correction + CLAHE.

        The strength is selected from ``_NIGHT_MODE_PROFILES`` so users can
        choose between a gentle lift (low), balanced enhancement (normal),
        or the previous aggressive CLAHE behavior.

        When a dedicated IR/night camera is active and software enhancement is
        disabled, the frame is returned unchanged.
        """
        if not self.night_mode:
            return frame
        if self._active_device == self.night_device and not self.night_software_enhance:
            return frame

        profile = _NIGHT_MODE_PROFILES.get(self.night_mode_strength, _NIGHT_MODE_PROFILES["normal"])
        gamma = float(profile["gamma"])

        lab: np.ndarray = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        lightness, a, b = cv2.split(lab)

        if gamma != 1.0 and gamma > 0.0:
            inv_gamma = 1.0 / gamma
            table = ((np.arange(256, dtype=np.float32) / 255.0) ** inv_gamma) * 255.0
            lightness = cv2.LUT(lightness, table.astype(np.uint8))

        clahe = cv2.createCLAHE(
            clipLimit=float(profile["clip_limit"]),
            tileGridSize=(int(profile["tile_grid"]), int(profile["tile_grid"])),
        )
        lightness = clahe.apply(lightness)
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

    def _redact_token(self, text: str) -> str:
        if not self.telegram_token:
            return text
        return text.replace(self.telegram_token, "<TOKEN>")

    def _create_aes_zip(self, source_path: Path, archive_name: str | None = None) -> Path:
        """Compress source_path into an AES-256 encrypted .zip file.

        Returns the path to the newly created .zip archive.
        """
        zip_name = (archive_name or source_path.stem) + ".zip"
        zip_path = source_path.parent / zip_name
        passphrase = (self.encryption_passphrase or "").encode("utf-8")

        with pyzipper.AESZipFile(
            zip_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(passphrase)
            zf.write(source_path, arcname=source_path.name)

        return zip_path

    def _send_telegram_document(self, doc_path: Path, chat_id: str, caption: str = "") -> None:
        """Send a document (.zip file) to a Telegram chat."""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendDocument"
        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                with open(doc_path, "rb") as f:
                    files: dict[str, Any] = {"document": (doc_path.name, f, "application/zip")}
                    data = {"chat_id": chat_id, "caption": caption}
                    response = requests.post(url, data=data, files=files, timeout=120)

                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram API {response.status_code} sending document (attempt {attempt}/{max_retries})"
                    )
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                    continue

                if response.status_code != 200:
                    raise RuntimeError(
                        f"Telegram API error {response.status_code}: {response.text}"
                    )
                result = response.json().get("result", {})
                raw_message_id = result.get("message_id")
                message_id: int | None = raw_message_id if isinstance(raw_message_id, int) else None
                if message_id is not None:
                    self._store_telegram_message(
                        message_id=message_id,
                        chat_id=chat_id,
                        msg_type="document",
                        caption=doc_path.name,
                    )
                logger.info(f"Telegram document sent: {doc_path.name}")
                return
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(
                    f"Telegram document send failed (attempt {attempt}/{max_retries}): {self._redact_token(str(e))}"
                )
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
            except Exception as e:
                logger.error(f"Failed to send Telegram document: {self._redact_token(str(e))}")
                return
        logger.error(f"Failed to send Telegram document after {max_retries} attempts")

    def _send_telegram_video(self, video_path: Path, chat_id: str | None = None) -> None:
        target_chat = chat_id or self.telegram_chat_id
        if not target_chat:
            return

        if self.encrypt_telegram and self.encryption_passphrase:
            zip_path: Path | None = None
            try:
                zip_path = self._create_aes_zip(video_path)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                caption = f"🔒 Encrypted Motion Video ({timestamp})\nFile: {zip_path.name}"
                self._send_telegram_document(zip_path, target_chat, caption=caption)
            finally:
                if zip_path and zip_path.exists():
                    zip_path.unlink(missing_ok=True)
            return

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendVideo"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = f"🎥 Motion detected at {timestamp}\nFile: {video_path.name}"

        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                with open(video_path, "rb") as f:
                    files = {"video": (video_path.name, f, "video/avi")}
                    data = {"chat_id": target_chat, "caption": caption}
                    response = requests.post(url, data=data, files=files, timeout=120)

                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram API {response.status_code} sending video (attempt {attempt}/{max_retries})"
                    )
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                    continue

                if response.status_code != 200:
                    raise RuntimeError(
                        f"Telegram API error {response.status_code}: {response.text}"
                    )
                result = response.json().get("result", {})
                raw_message_id = result.get("message_id")
                message_id: int | None = raw_message_id if isinstance(raw_message_id, int) else None
                if message_id is not None:
                    self._store_telegram_message(
                        message_id=message_id,
                        chat_id=target_chat,
                        msg_type="video",
                        caption=video_path.name,
                    )
                logger.info(f"Telegram video sent: {video_path.name}")
                return
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(
                    f"Telegram video send failed (attempt {attempt}/{max_retries}): {self._redact_token(str(e))}"
                )
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
            except Exception as e:
                logger.error(f"Failed to send Telegram video: {self._redact_token(str(e))}")
                return
        logger.error(f"Failed to send Telegram video after {max_retries} attempts")

    def _send_telegram_photo(self, image_bytes: bytes, chat_id: str, caption: str = "") -> None:
        if self.encrypt_telegram and self.encryption_passphrase:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_jpg = self.record_dir / f"snapshot_{timestamp}.jpg"
            zip_path: Path | None = None
            try:
                temp_jpg.write_bytes(image_bytes)
                zip_path = self._create_aes_zip(temp_jpg, archive_name=f"snapshot_{timestamp}")
                doc_caption = f"🔒 Encrypted Snapshot {caption}".strip()
                self._send_telegram_document(zip_path, chat_id, caption=doc_caption)
            finally:
                temp_jpg.unlink(missing_ok=True)
                if zip_path and zip_path.exists():
                    zip_path.unlink(missing_ok=True)
            return

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
        files = {"photo": ("snapshot.jpg", image_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption}

        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, data=data, files=files, timeout=60)
                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram API {response.status_code} sending photo (attempt {attempt}/{max_retries})"
                    )
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                    continue
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Telegram API error {response.status_code}: {response.text}"
                    )
                result = response.json().get("result", {})
                raw_message_id = result.get("message_id")
                message_id: int | None = raw_message_id if isinstance(raw_message_id, int) else None
                if message_id is not None:
                    self._store_telegram_message(
                        message_id=message_id,
                        chat_id=chat_id,
                        msg_type="photo",
                        caption=caption[:200],
                    )
                logger.info(f"Telegram snapshot sent to {chat_id}")
                return
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(
                    f"Telegram photo send failed (attempt {attempt}/{max_retries}): {self._redact_token(str(e))}"
                )
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
            except Exception as e:
                logger.error(f"Failed to send Telegram snapshot: {self._redact_token(str(e))}")
                return
        logger.error(f"Failed to send Telegram snapshot after {max_retries} attempts")

    def _send_telegram_message(self, text: str, chat_id: str) -> int | None:
        """Send a text message. Return the message_id on success, None otherwise."""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        data = {"chat_id": chat_id, "text": text}

        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, data=data, timeout=30)
                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram API {response.status_code} sending message (attempt {attempt}/{max_retries})"
                    )
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                    continue
                if response.status_code != 200:
                    logger.error(f"Telegram message error {response.status_code}: {response.text}")
                    return None
                result = response.json().get("result", {})
                raw_message_id = result.get("message_id")
                message_id: int | None = raw_message_id if isinstance(raw_message_id, int) else None
                if message_id is not None:
                    self._store_telegram_message(
                        message_id=message_id,
                        chat_id=chat_id,
                        msg_type="text",
                        caption=text[:200],
                    )
                return message_id
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(
                    f"Telegram message send failed (attempt {attempt}/{max_retries}): {self._redact_token(str(e))}"
                )
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {self._redact_token(str(e))}")
                return None
        logger.error(f"Failed to send Telegram message after {max_retries} attempts")
        return None

    def _store_telegram_message(
        self,
        message_id: int,
        chat_id: str,
        msg_type: str,
        caption: str = "",
        max_entries: int = 100,
    ) -> None:
        """Persist a sent Telegram message ID so it can be deleted later."""
        try:
            entry = {
                "message_id": message_id,
                "chat_id": chat_id,
                "type": msg_type,
                "caption": caption,
                "timestamp": datetime.now().isoformat(),
            }
            with self._telegram_store_lock:
                messages = self._load_telegram_messages()
                messages.append(entry)
                # Keep only the most recent entries to avoid unbounded growth.
                if len(messages) > max_entries:
                    messages = messages[-max_entries:]
                temp_path = self._telegram_store_path.with_suffix(".tmp")
                with open(temp_path, "w") as f:
                    json.dump(messages, f, indent=2)
                temp_path.replace(self._telegram_store_path)
        except Exception as e:
            logger.error(f"Failed to store Telegram message ID: {self._redact_token(str(e))}")

    def _load_telegram_messages(self) -> list[dict]:
        """Load persisted Telegram message IDs."""
        if not self._telegram_store_path.exists():
            return []
        try:
            with open(self._telegram_store_path) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.error(f"Failed to load Telegram message store: {self._redact_token(str(e))}")
        return []

    def _delete_telegram_message(self, message_id: int, chat_id: str) -> bool:
        """Delete a message from Telegram and the local store. Return True on success."""
        url = f"https://api.telegram.org/bot{self.telegram_token}/deleteMessage"
        data = {"chat_id": chat_id, "message_id": message_id}

        max_retries = 3
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, data=data, timeout=30)
                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram API {response.status_code} deleting message (attempt {attempt}/{max_retries})"
                    )
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2
                    continue
                if response.status_code != 200:
                    logger.error(
                        f"Telegram deleteMessage error {response.status_code}: {response.text}"
                    )
                    return False
                with self._telegram_store_lock:
                    messages = self._load_telegram_messages()
                    messages = [m for m in messages if m.get("message_id") != message_id]
                    temp_path = self._telegram_store_path.with_suffix(".tmp")
                    with open(temp_path, "w") as f:
                        json.dump(messages, f, indent=2)
                    temp_path.replace(self._telegram_store_path)
                logger.info(f"Telegram message {message_id} deleted")
                return True
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(
                    f"Telegram delete failed (attempt {attempt}/{max_retries}): {self._redact_token(str(e))}"
                )
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
            except Exception as e:
                logger.error(f"Failed to delete Telegram message: {self._redact_token(str(e))}")
                return False
        logger.error(f"Failed to delete Telegram message after {max_retries} attempts")
        return False

    def _telegram_poll_loop(self) -> None:
        logger.info("Starting Telegram command polling...")
        while self.running and self.telegram_poll_commands:
            # Command polling works even when automatic Telegram uploads are disabled,
            # so the user can still use /snapshot, /video, and toggle settings.
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/getUpdates"
                params = {"offset": self._telegram_offset + 1, "limit": 10}
                response = requests.get(url, params=params, timeout=30)
                if response.status_code >= 500:
                    logger.warning(
                        f"Telegram getUpdates returned {response.status_code}, retrying..."
                    )
                    time.sleep(5)
                    continue
                if response.status_code != 200:
                    logger.error(
                        f"Telegram getUpdates error {response.status_code}: {response.text}"
                    )
                    time.sleep(5)
                    continue

                data = response.json()
                if not data.get("ok"):
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    self._telegram_offset = max(self._telegram_offset, update["update_id"])
                    self._handle_telegram_update(update)
            except requests.exceptions.Timeout:
                # Long-polling timeouts are normal on a quiet chat.
                logger.debug("Telegram getUpdates timed out, retrying...")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Telegram poll connection error: {self._redact_token(str(e))}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Telegram poll error: {self._redact_token(str(e))}")
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
        elif cmd[0] == "/sent":
            self._handle_telegram_sent(chat_id)
        elif cmd[0] == "/delete":
            self._handle_telegram_delete(cmd, chat_id)
        elif cmd[0] == "/delete_range":
            self._handle_telegram_delete_range(cmd, chat_id)
        elif cmd[0] == "/id":
            reply = message.get("reply_to_message")
            if reply:
                self._send_telegram_message(f"Reply message ID: {reply['message_id']}", chat_id)
            else:
                self._send_telegram_message(
                    "Reply to a message with /id to get its Telegram message ID.", chat_id
                )
        elif cmd[0] == "/telegram_on":
            self.set_telegram_enabled(True)
            self._send_telegram_message("Telegram auto-uploads enabled.", chat_id)
        elif cmd[0] == "/telegram_off":
            self.set_telegram_enabled(False)
            self._send_telegram_message("Telegram auto-uploads disabled.", chat_id)
        elif cmd[0] == "/email_on":
            self.set_notifications_enabled(True)
            self._send_telegram_message("Email notifications enabled.", chat_id)
        elif cmd[0] == "/email_off":
            self.set_notifications_enabled(False)
            self._send_telegram_message("Email notifications disabled.", chat_id)
        elif cmd[0] == "/night_mode_on":
            self.set_night_mode(True)
            self._send_telegram_message(
                f"Night mode enabled (strength: {self.night_mode_strength}).", chat_id
            )
        elif cmd[0] == "/night_mode_off":
            self.set_night_mode(False)
            self._send_telegram_message("Night mode disabled.", chat_id)
        elif cmd[0] == "/night_mode":
            if len(cmd) > 1 and cmd[1] in _NIGHT_MODE_PROFILES:
                self.set_night_mode_strength(cmd[1])
                if not self.night_mode:
                    self.set_night_mode(True)
                self._send_telegram_message(
                    f"Night mode enabled with {self.night_mode_strength} strength.", chat_id
                )
            else:
                self._send_telegram_message(
                    "Usage: /night_mode low | normal | aggressive\n"
                    "Use /night_mode_off to disable.",
                    chat_id,
                )
        elif cmd[0] == "/encrypt_telegram_on":
            self.set_encrypt_telegram(True)
            self._send_telegram_message("Telegram upload encryption enabled (AES-256 ZIP).", chat_id)
        elif cmd[0] == "/encrypt_telegram_off":
            self.set_encrypt_telegram(False)
            self._send_telegram_message("Telegram upload encryption disabled.", chat_id)
        elif cmd[0] == "/encrypt_gdrive_on":
            self.set_encrypt_gdrive(True)
            self._send_telegram_message("Google Drive upload encryption enabled (AES-256 ZIP).", chat_id)
        elif cmd[0] == "/encrypt_gdrive_off":
            self.set_encrypt_gdrive(False)
            self._send_telegram_message("Google Drive upload encryption disabled.", chat_id)
        elif cmd[0] == "/encrypt_onedrive_on":
            self.set_encrypt_onedrive(True)
            self._send_telegram_message("OneDrive upload encryption enabled (AES-256 ZIP).", chat_id)
        elif cmd[0] == "/encrypt_onedrive_off":
            self.set_encrypt_onedrive(False)
            self._send_telegram_message("OneDrive upload encryption disabled.", chat_id)
        elif cmd[0] == "/encryption":
            pass_set = "Set" if bool(self.encryption_passphrase) else "Not set!"
            status_msg = (
                f"🔒 Encryption Settings (AES-256 ZIP):\n"
                f"- Passphrase: {pass_set}\n"
                f"- Telegram: {'Enabled' if self.encrypt_telegram else 'Disabled'}\n"
                f"- Google Drive: {'Enabled' if self.encrypt_gdrive else 'Disabled'}\n"
                f"- OneDrive: {'Enabled' if self.encrypt_onedrive else 'Disabled'}"
            )
            self._send_telegram_message(status_msg, chat_id)
        elif cmd[0] == "/help":
            self._send_telegram_message(
                "Available commands:\n"
                "/snapshot - get current picture\n"
                "/video <seconds> - record and send a video (1-60s, default 10)\n"
                "/sent - list recent bot messages that can be deleted\n"
                "/delete <message_id> - delete a bot message by ID\n"
                "/delete last - delete the most recent bot message\n"
                "/delete_range <min_id> <max_id> - delete all bot messages with IDs in the range\n"
                "/id - reply to any bot message with this to see its message ID\n"
                "/telegram_on /telegram_off - enable or disable auto Telegram uploads\n"
                "/email_on /email_off - enable or disable email notifications\n"
                "/night_mode_on /night_mode_off - enable or disable night mode\n"
                "  (switches to the IR camera if one is configured)\n"
                "/night_mode low|normal|aggressive - set night-mode enhancement strength\n"
                "/encryption - show cloud & Telegram encryption status\n"
                "/encrypt_telegram_on /off - toggle Telegram AES-256 encryption\n"
                "/encrypt_gdrive_on /off - toggle Google Drive AES-256 encryption\n"
                "/encrypt_onedrive_on /off - toggle OneDrive AES-256 encryption\n"
                "/help - show this help",
                chat_id,
            )
        else:
            self._send_telegram_message("Unknown command. Use /help.", chat_id)

    def _handle_telegram_sent(self, chat_id: str) -> None:
        try:
            messages = self._load_telegram_messages()
            if not messages:
                self._send_telegram_message("No tracked bot messages.", chat_id)
                return
            lines = ["Recent bot messages (newest last):"]
            for entry in messages[-20:]:
                ts = entry.get("timestamp", "unknown")[:19].replace("T", " ")
                lines.append(
                    f"{entry['message_id']} - {entry['type']} - {ts}\n  {entry.get('caption', '')}"
                )
            self._send_telegram_message("\n".join(lines), chat_id)
        except Exception as e:
            logger.error(f"Failed to list Telegram messages: {self._redact_token(str(e))}")
            self._send_telegram_message("Failed to list messages.", chat_id)

    def _handle_telegram_delete(self, cmd: list[str], chat_id: str) -> None:
        try:
            if len(cmd) < 2:
                self._send_telegram_message("Usage: /delete <message_id> or /delete last", chat_id)
                return

            if cmd[1] == "last":
                messages = self._load_telegram_messages()
                if not messages:
                    self._send_telegram_message("No tracked messages to delete.", chat_id)
                    return
                message_id = messages[-1]["message_id"]
            else:
                try:
                    message_id = int(cmd[1])
                except ValueError:
                    self._send_telegram_message("Invalid message ID.", chat_id)
                    return

            if self._delete_telegram_message(message_id, chat_id):
                self._send_telegram_message(f"Message {message_id} deleted.", chat_id)
            else:
                self._send_telegram_message(
                    f"Could not delete message {message_id}. It may be too old or already removed.",
                    chat_id,
                )
        except Exception as e:
            logger.error(f"Failed to handle delete command: {self._redact_token(str(e))}")
            self._send_telegram_message("Failed to delete message.", chat_id)

    def _handle_telegram_delete_range(self, cmd: list[str], chat_id: str) -> None:
        try:
            if len(cmd) != 3:
                self._send_telegram_message("Usage: /delete_range <min_id> <max_id>", chat_id)
                return

            try:
                min_id = int(cmd[1])
                max_id = int(cmd[2])
            except ValueError:
                self._send_telegram_message("Both IDs must be integers.", chat_id)
                return

            if min_id > max_id:
                self._send_telegram_message("min_id must be <= max_id.", chat_id)
                return

            # Attempt to delete every message ID in the requested range, not
            # only the IDs stored locally. Telegram will return an error for
            # IDs that are too old, already deleted, or not sent by this bot.
            ids_to_delete = list(range(min_id, max_id + 1))

            deleted = 0
            failed = 0
            for message_id in ids_to_delete:
                if self._delete_telegram_message(message_id, chat_id):
                    deleted += 1
                else:
                    failed += 1

            if deleted == 0:
                self._send_telegram_message(
                    f"Could not delete any messages in range {min_id}-{max_id} "
                    "(they may be too old, already removed, or not sent by this bot).",
                    chat_id,
                )
            else:
                self._send_telegram_message(
                    f"Deleted {deleted} messages in range {min_id}-{max_id} ({failed} failed).",
                    chat_id,
                )
        except Exception as e:
            logger.error(f"Failed to handle delete_range command: {self._redact_token(str(e))}")
            self._send_telegram_message("Failed to delete message range.", chat_id)

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
            logger.error(f"Failed to handle snapshot command: {self._redact_token(str(e))}")
            self._send_telegram_message("Failed to take snapshot.", chat_id)

    def trigger_manual_recording(self, seconds: int, chat_id: str | None = None) -> None:
        """Start or extend a manual recording for the given number of seconds."""
        seconds = max(1, min(60, seconds))
        with self._state_lock:
            self._manual_record_until = time.time() + seconds
            self._manual_record_chat_id = chat_id
            self._manual_recording_active = True

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

            self.trigger_manual_recording(seconds, chat_id)
        except Exception as e:
            logger.error(f"Failed to handle video command: {self._redact_token(str(e))}")
            self._send_telegram_message("Failed to start recording.", chat_id)

    # ------------------------------------------------------------------
    # Cloud Storage Uploads (Google Drive & OneDrive)
    # ------------------------------------------------------------------
    def _upload_to_gdrive(self, video_path: Path) -> bool:
        """Upload a video recording to Google Drive via Google Drive REST API v3."""
        if not self.gdrive_refresh_token or not self.gdrive_client_id:
            logger.warning("Google Drive credentials not configured; skipping upload.")
            return False

        target_file = video_path
        temp_zip: Path | None = None
        if self.encrypt_gdrive and self.encryption_passphrase:
            temp_zip = self._create_aes_zip(video_path)
            target_file = temp_zip

        try:
            token_url = "https://oauth2.googleapis.com/token"
            token_data = {
                "client_id": self.gdrive_client_id,
                "client_secret": self.gdrive_client_secret,
                "refresh_token": self.gdrive_refresh_token,
                "grant_type": "refresh_token",
            }
            token_resp = requests.post(token_url, data=token_data, timeout=30)
            if token_resp.status_code != 200:
                logger.error(
                    f"Google Drive token refresh failed: {token_resp.status_code} {token_resp.text}"
                )
                return False
            access_token = token_resp.json().get("access_token")
            if not access_token:
                logger.error("No access_token returned from Google Drive OAuth.")
                return False

            # Use a resumable upload so the file can be streamed instead of
            # loading the whole clip into memory on small boards.
            upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
            metadata: dict[str, Any] = {"name": target_file.name}
            if self.gdrive_folder_id:
                metadata["parents"] = [self.gdrive_folder_id]

            init_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            }
            init_resp = requests.post(
                upload_url, headers=init_headers, data=json.dumps(metadata), timeout=30
            )
            if init_resp.status_code != 200:
                logger.error(
                    f"Google Drive resumable session failed: {init_resp.status_code} {init_resp.text}"
                )
                return False
            location = init_resp.headers.get("Location")
            if not location:
                logger.error("Google Drive resumable session returned no Location header.")
                return False

            mime_type = "application/zip" if target_file.suffix.lower() == ".zip" else "video/avi"
            file_size = target_file.stat().st_size
            upload_headers = {
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
            }
            with open(target_file, "rb") as f:
                resp = requests.put(location, headers=upload_headers, data=f, timeout=300)

            if resp.status_code in (200, 201):
                logger.info(f"Uploaded {target_file.name} to Google Drive.")
                return True
            else:
                logger.error(f"Google Drive upload failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Google Drive upload error: {e}")
            return False
        finally:
            if temp_zip and temp_zip.exists():
                temp_zip.unlink(missing_ok=True)

    def _upload_to_onedrive(self, video_path: Path) -> bool:
        """Upload a video recording to OneDrive via Microsoft Graph REST API."""
        if not self.onedrive_refresh_token or not self.onedrive_client_id:
            logger.warning("OneDrive credentials not configured; skipping upload.")
            return False

        target_file = video_path
        temp_zip: Path | None = None
        if self.encrypt_onedrive and self.encryption_passphrase:
            temp_zip = self._create_aes_zip(video_path)
            target_file = temp_zip

        try:
            token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
            token_data = {
                "client_id": self.onedrive_client_id,
                "client_secret": self.onedrive_client_secret,
                "refresh_token": self.onedrive_refresh_token,
                "grant_type": "refresh_token",
                "scope": "Files.ReadWrite.All offline_access",
            }
            token_resp = requests.post(token_url, data=token_data, timeout=30)
            if token_resp.status_code != 200:
                logger.error(
                    f"OneDrive token refresh failed: {token_resp.status_code} {token_resp.text}"
                )
                return False
            access_token = token_resp.json().get("access_token")
            if not access_token:
                logger.error("No access_token returned from OneDrive OAuth.")
                return False

            folder = self.onedrive_folder_path.strip("/")
            filename = target_file.name
            path_url = f"{folder}/{filename}" if folder else filename
            upload_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{path_url}:/content"

            mime_type = "application/zip" if target_file.suffix.lower() == ".zip" else "video/avi"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": mime_type,
            }
            with open(target_file, "rb") as f:
                resp = requests.put(upload_url, headers=headers, data=f, timeout=300)

            if resp.status_code in (200, 201):
                logger.info(f"Uploaded {target_file.name} to OneDrive.")
                return True
            else:
                logger.error(f"OneDrive upload failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"OneDrive upload error: {e}")
            return False
        finally:
            if temp_zip and temp_zip.exists():
                temp_zip.unlink(missing_ok=True)

    def _maybe_upload_cloud(self, video_path: Path) -> None:
        """Trigger background cloud uploads if enabled."""
        if not video_path or not video_path.is_file():
            return
        if self.gdrive_enabled:
            threading.Thread(
                target=self._upload_to_gdrive,
                args=(video_path,),
                daemon=True,
            ).start()
        if self.onedrive_enabled:
            threading.Thread(
                target=self._upload_to_onedrive,
                args=(video_path,),
                daemon=True,
            ).start()

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

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except FileNotFoundError:
                return float("inf")

        files = sorted(self._recording_files(), key=_mtime)
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
            self._recording_files(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        total_size = 0
        for path in files:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
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
                if not path.exists():
                    continue
                try:
                    total_size -= path.stat().st_size
                except FileNotFoundError:
                    continue
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
