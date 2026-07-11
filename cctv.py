#!/usr/bin/env python3
"""
CheapSecurity CCTV engine.

Captures video from a V4L2 webcam, detects motion by frame differencing,
records clips to disk with a pre-motion buffer, and exposes the live feed
for the web interface.
"""

import json
import logging
import os
import shutil
import smtplib
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cctv")


class CCTVSystem:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        with open(config_path, "r") as f:
            self.cfg = json.load(f)

        cam = self.cfg["camera"]
        self.device = cam["device"]
        self.width = cam["width"]
        self.height = cam["height"]
        self.fps = cam["fps"]
        self.actual_fps = self.fps

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

        self.cap: Optional[cv2.VideoCapture] = None
        self.writer: Optional[cv2.VideoWriter] = None
        self.recording_path: Optional[Path] = None
        self.is_recording = False
        self.last_motion_time: float = 0.0
        self.recording_started: float = 0.0
        self.motion_active = False
        self.running = False
        self.thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._current_frame: Optional[bytes] = None
        self._jpeg_quality = 75
        self._buffer_jpeg_quality = 85  # lower memory use for pre-motion buffer

        pre_size = int(self.actual_fps * self.pre_buffer_seconds)
        self._pre_buffer: deque = deque(maxlen=max(pre_size, 1))
        self._prev_gray: Optional[np.ndarray] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        logger.info("Starting CCTV engine...")
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        logger.info("Stopping CCTV engine...")
        self.running = False
        if self.thread:
            self.thread.join(timeout=5.0)
        self._release_capture()
        self._stop_recording()
        logger.info("CCTV engine stopped.")

    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._current_frame

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
        with open(self.config_path, "w") as f:
            json.dump(self.cfg, f, indent=2)

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

        while self.running:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                logger.warning("Frame capture failed, retrying...")
                time.sleep(0.1)
                continue

            # Update live JPEG frame for web stream
            self._update_live_frame(frame)

            # Motion detection
            motion = self._detect_motion(frame)
            now = time.time()

            if motion:
                self.last_motion_time = now
                self.motion_active = True
                if not self.is_recording:
                    self._start_recording(frame)
                    self._maybe_send_alert(frame)
            else:
                # Keep recording for cooldown period after motion stops
                if self.is_recording and (now - self.last_motion_time) > self.cooldown_seconds:
                    self.motion_active = False
                    self._stop_recording()

            # Enforce max clip duration
            if self.is_recording and (now - self.recording_started) >= self.max_duration:
                logger.info("Max clip duration reached, closing segment.")
                self._stop_recording()
                # Restart immediately if still in motion
                if motion:
                    self._start_recording(frame)

            if self.is_recording:
                self._write_frame(frame)
            else:
                self._pre_buffer.append(self._encode_buffer_frame(frame))

            # Periodic storage cleanup
            if (now - last_cleanup) > (self.cleanup_interval * 60):
                self._cleanup_storage()
                last_cleanup = now

        self._stop_recording()
        self._release_capture()

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------
    def _open_capture(self) -> bool:
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
        logger.info(f"Camera resolution: {actual_width}x{actual_height} @ {self.actual_fps:.1f} fps")
        return True

    def _release_capture(self) -> None:
        if self.cap:
            self.cap.release()
            self.cap = None

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
        area_factor = 1.0 / (self.motion_scale ** 2)
        for cnt in contours:
            if cv2.contourArea(cnt) * area_factor >= self.min_area:
                return True
        return False

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
        self.is_recording = True
        self.recording_started = time.time()
        logger.info(f"Recording started: {self.recording_path.name}")

        # Dump pre-buffer so the clip includes seconds before motion
        for encoded in self._pre_buffer:
            decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                self._write_frame(decoded)
        self._pre_buffer.clear()

    def _create_writer(self, path: str, width: int, height: int) -> Optional["cv2.VideoWriter"]:
        fourcc = cv2.VideoWriter_fourcc(*self.codec_fourcc)
        writer = cv2.VideoWriter(path, fourcc, self.actual_fps, (width, height))
        if writer.isOpened():
            return writer

        # Fallbacks for embedded/ARM boards where codec support varies
        for codec, ext in [("MJPG", ".avi"), ("XVID", ".avi")]:
            logger.warning(f"Codec {self.codec_fourcc} failed, trying {codec}")
            fallback_path = path
            if ext != self.video_ext:
                fallback_path = str(Path(path).with_suffix(ext))
            fourcc = cv2.VideoWriter_fourcc(*codec)
            writer = cv2.VideoWriter(fallback_path, fourcc, self.actual_fps, (width, height))
            if writer.isOpened():
                self.recording_path = Path(fallback_path)
                return writer

        return None

    def _write_frame(self, frame: np.ndarray) -> None:
        if self.writer:
            self.writer.write(frame)

    def _stop_recording(self) -> None:
        if self.writer:
            self.writer.release()
            self.writer = None
        if self.recording_path:
            size = self.recording_path.stat().st_size
            logger.info(f"Recording saved: {self.recording_path.name} ({self._human_size(size)})")
            self.recording_path = None
        self.is_recording = False
        self.motion_active = False

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def _encode_buffer_frame(self, frame: np.ndarray) -> bytes:
        _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._buffer_jpeg_quality])
        return buf.tobytes()

    def _update_live_frame(self, frame: np.ndarray) -> None:
        if self.stream_scale < 1.0:
            stream_frame = cv2.resize(frame, (0, 0), fx=self.stream_scale, fy=self.stream_scale)
        else:
            stream_frame = frame
        _, buf = cv2.imencode(".jpg", stream_frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])
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
    # Storage cleanup
    # ------------------------------------------------------------------
    def _ensure_disk_space(self) -> None:
        """Delete the oldest N recordings if free disk space is low."""
        free_bytes = shutil.disk_usage(self.record_dir).free
        free_gb = free_bytes / (1024 ** 3)
        if free_gb >= self.emergency_free_space_gb:
            return

        logger.warning(
            f"Low disk space: {free_gb:.2f} GB free. "
            f"Deleting up to {self.emergency_delete_count} oldest recordings."
        )
        files = sorted(
            (p for p in self.record_dir.iterdir() if p.is_file()),
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
            f"Deleted {deleted} recordings. "
            f"Free space: {free_bytes_after / (1024 ** 3):.2f} GB"
        )

    def _cleanup_storage(self) -> None:
        logger.info("Running storage cleanup...")
        now = datetime.now()
        files = sorted(
            (p for p in self.record_dir.iterdir() if p.is_file()),
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

        max_bytes = self.max_size_gb * (1024 ** 3)
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
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"


if __name__ == "__main__":
    import signal

    system = CCTVSystem()

    def _shutdown(signum, frame):
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
