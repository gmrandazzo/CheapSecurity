#!/usr/bin/env python3
"""RTSP output publisher using MediaMTX + FFmpeg.

Reads the existing HTTP MJPEG stream and republishes it as an RTSP stream
that clients such as VLC or IP-camera viewers can consume.
"""

import logging
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cctv")


class RTSPPublisher:
    """Manage an external MediaMTX server and an FFmpeg republisher process."""

    def __init__(
        self,
        cfg: dict[str, Any],
        web_host: str,
        web_port: int,
    ) -> None:
        self.enabled = cfg.get("enabled", False)
        self.rtsp_port = int(cfg.get("port", 8554))
        self.rtsp_path = cfg.get("path", "live")
        self.width = int(cfg.get("width", 1280))
        self.height = int(cfg.get("height", 720))
        self.fps = int(cfg.get("fps", 15))
        self.mediamtx_binary = cfg.get("mediamtx_binary", "mediamtx")
        self.mediamtx_config = cfg.get("mediamtx_config", "rtsp/mediamtx.yml")

        self.web_host = web_host
        self.web_port = web_port

        self._mediamtx_proc: subprocess.Popen | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the RTSP publisher in a background thread."""
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown and terminate subprocesses."""
        self._stop_event.set()
        self._terminate(self._ffmpeg_proc)
        self._terminate(self._mediamtx_proc)
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            if not self._start_mediamtx():
                return
            self._start_ffmpeg()
            # Keep thread alive until stop() is called; restart ffmpeg if it dies.
            while not self._stop_event.wait(2.0):
                if self._ffmpeg_proc and self._ffmpeg_proc.poll() is not None:
                    logger.warning("FFmpeg RTSP publisher exited; restarting...")
                    self._start_ffmpeg()
        except Exception as e:
            logger.error(f"RTSP publisher error: {e}")

    def _start_mediamtx(self) -> bool:
        if not shutil.which(self.mediamtx_binary):
            logger.error(
                f"MediaMTX binary not found: {self.mediamtx_binary}. " "RTSP output disabled."
            )
            return False

        config_path = Path(self.mediamtx_config)
        if not config_path.is_file():
            logger.error(f"MediaMTX config not found: {config_path}. " "RTSP output disabled.")
            return False

        cmd = [
            self.mediamtx_binary,
            str(config_path),
        ]
        logger.info(f"Starting MediaMTX: {' '.join(cmd)}")
        self._mediamtx_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for the RTSP port to become reachable.
        if not self._wait_for_port("127.0.0.1", self.rtsp_port, timeout=10.0):
            logger.error("MediaMTX did not start in time; RTSP output disabled.")
            self._terminate(self._mediamtx_proc)
            return False

        logger.info(f"MediaMTX ready on rtsp://127.0.0.1:{self.rtsp_port}")
        return True

    def _start_ffmpeg(self) -> None:
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found; RTSP output disabled.")
            return

        input_url = f"http://127.0.0.1:{self.web_port}/video_feed"
        output_url = f"rtsp://127.0.0.1:{self.rtsp_port}/{self.rtsp_path}"

        cmd = [
            "ffmpeg",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-i",
            input_url,
            "-f",
            "mjpeg",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "2000k",
            "-maxrate",
            "2500k",
            "-bufsize",
            "500k",
            "-g",
            str(self.fps * 2),
            "-r",
            str(self.fps),
            "-s",
            f"{self.width}x{self.height}",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            output_url,
        ]
        logger.info(f"Starting FFmpeg RTSP publisher: {' '.join(cmd)}")
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            logger.warning("Killing RTSP subprocess after timeout.")
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception as e:
            logger.error(f"Error terminating RTSP subprocess: {e}")

    @staticmethod
    def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.2)
        return False
