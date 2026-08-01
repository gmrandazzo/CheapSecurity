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
CheapSecurity web interface.

Serves a small dashboard with a live MJPEG stream, a list of recordings,
and direct playback/download links.
"""

import base64
import contextlib
import os
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from types import FrameType
from typing import TypeAlias

import cv2
from flasgger import Swagger
from flask import (
    Flask,
    Response,
    after_this_request,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from cheapsecurity.cctv import CCTVSystem

app = Flask(__name__)
Swagger(
    app,
    template={
        "swagger": "2.0",
        "info": {
            "title": "CheapSecurity API",
            "description": "REST API and MJPEG stream for the CheapSecurity CCTV system.",
            "version": "0.1.0",
        },
        "securityDefinitions": {
            "basicAuth": {
                "type": "basic",
                "description": "HTTP Basic Auth configured in config.json web.auth",
            }
        },
    },
    config={
        "specs": [
            {
                "endpoint": "apispec_1",
                "route": "/apispec_1.json",
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "specs_route": "/api/",
        "headers": [],
    },
    merge=True,
)
cctv: CCTVSystem | None = None

RouteReturn: TypeAlias = Response | tuple[Response | str, int] | str


def init_cctv(config_path: str = "config.json") -> CCTVSystem:
    global cctv
    if cctv is None:
        cctv = CCTVSystem(config_path)
        cctv.start()
    assert cctv is not None
    return cctv


def _check_auth() -> Response | None:
    auth_cfg = (cctv.cfg.get("web") or {}).get("auth") if cctv else None
    if not auth_cfg or not auth_cfg.get("enabled"):
        return None
    # Allow the local RTSP publisher (FFmpeg) to read the MJPEG stream
    # without credentials when auth is enabled.
    if request.path == "/video_feed" and request.remote_addr in ("127.0.0.1", "::1"):
        return None
    # Swagger UI and its static assets / OpenAPI spec must be reachable.
    if (
        request.path == "/api/"
        or request.path.startswith("/flasgger_static")
        or request.path.startswith("/apispec_")
    ):
        return None
    expected_user = auth_cfg.get("username", "admin")
    expected_pass = auth_cfg.get("password", "changeme")

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == expected_user and password == expected_pass:
                return None
        except Exception:
            pass

    resp = make_response("Unauthorized", 401)
    resp.headers["WWW-Authenticate"] = 'Basic realm="CheapSecurity"'
    return resp


@app.before_request
def require_auth() -> Response | None:
    return _check_auth()


_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@app.before_request
def require_csrf() -> Response | None:
    if request.method in _CSRF_SAFE_METHODS:
        return None
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return None
    # Allow requests originating from the Swagger UI page.
    referrer = request.referrer or ""
    if "/api/" in referrer:
        return None
    resp = make_response(jsonify({"error": "CSRF protection: missing X-Requested-With header"}))
    resp.status_code = 403
    return resp


@app.route("/")
def index() -> str:
    cfg = cctv.cfg if cctv else {}
    return str(render_template("index.html", cfg=cfg))


@app.route("/api/status")
def api_status() -> RouteReturn:
    """Get the current CCTV engine status.
    ---
    tags:
      - status
    security:
      - basicAuth: []
    responses:
      200:
        description: Current engine status
        schema:
          type: object
          properties:
            running: {type: boolean}
            is_recording: {type: boolean}
            motion_active: {type: boolean}
            recording_file: {type: string, nullable: true}
            resolution: {type: string}
            fps: {type: number}
            night_mode: {type: boolean}
            night_mode_strength: {type: string}
            night_device_active: {type: boolean}
            night_device_configured: {type: boolean}
            notifications_enabled: {type: boolean}
            telegram_enabled: {type: boolean}
            auth_enabled: {type: boolean}
        examples:
          application/json:
            running: true
            is_recording: false
            motion_active: false
            recording_file: null
            resolution: "2560x1440"
            fps: 30.0
            night_mode: false
            night_mode_strength: normal
            night_device_active: false
            night_device_configured: false
            notifications_enabled: false
            telegram_enabled: true
            auth_enabled: true
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify(
        {
            "running": cctv.running,
            "is_recording": cctv.is_recording,
            "motion_active": cctv.motion_active,
            "recording_file": cctv.recording_path.name if cctv.recording_path else None,
            "resolution": f"{cctv.cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cctv.cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}"
            if cctv.cap
            else f"{cctv.width}x{cctv.height}",
            "fps": cctv.actual_fps,
            "night_mode": cctv.night_mode,
            "night_mode_strength": cctv.night_mode_strength,
            "night_device_active": cctv.night_device_active,
            "night_device_configured": cctv.night_device is not None,
            "notifications_enabled": cctv.notifications_enabled,
            "telegram_enabled": cctv.telegram_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/recordings")
def api_recordings() -> RouteReturn:
    """List all recorded videos, newest first.
    ---
    tags:
      - recordings
    security:
      - basicAuth: []
    responses:
      200:
        description: List of recordings
        schema:
          type: object
          properties:
            recordings:
              type: array
              items:
                type: object
                properties:
                  filename: {type: string}
                  size_bytes: {type: integer}
                  size_human: {type: string}
                  created: {type: string, format: date-time}
        examples:
          application/json:
            recordings:
              - filename: motion_20260710_232000.avi
                size_bytes: 12582912
                size_human: "12.0 MB"
                created: "2026-07-10T23:20:00"
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify({"recordings": cctv.list_recordings()})


@app.route("/api/recordings/delete", methods=["POST"])
def api_delete_recordings() -> RouteReturn:
    """Delete one or more recordings.
    ---
    tags:
      - recordings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            filenames:
              type: array
              items: {type: string}
              example: ["motion_20260101_120000.avi"]
    responses:
      200:
        description: Per-file deletion results
        examples:
          application/json:
            results:
              - filename: motion_20260710_232000.avi
                deleted: true
              - filename: notfound.avi
                deleted: false
                error: "Invalid file"
      400:
        description: No filenames provided
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])
    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    results = []
    for name in filenames:
        path = cctv.record_dir / name
        try:
            # Security: ensure the resolved path is still inside the recordings directory
            if path.resolve().parent != cctv.record_dir.resolve() or not path.is_file():
                results.append({"filename": name, "deleted": False, "error": "Invalid file"})
                continue
            path.unlink()
            results.append({"filename": name, "deleted": True})
        except Exception as e:
            results.append({"filename": name, "deleted": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/recordings/download", methods=["POST"])
def api_download_recordings() -> RouteReturn:
    """Download selected recordings as a ZIP file.
    ---
    tags:
      - recordings
    security:
      - basicAuth: []
    produces:
      - application/zip
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            filenames:
              type: array
              items: {type: string}
              example: ["motion_20260101_120000.avi"]
    responses:
      200:
        description: ZIP file download
      400:
        description: No filenames provided
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])
    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    with tempfile.NamedTemporaryFile(
        suffix=".zip", delete=False, dir=str(cctv.record_dir)
    ) as temp_zip:
        temp_zip_path = temp_zip.name

    @after_this_request
    def remove_file(response: Response) -> Response:
        with contextlib.suppress(Exception):
            os.unlink(temp_zip_path)
        return response

    try:
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in filenames:
                path = cctv.record_dir / name
                try:
                    if path.resolve().parent != cctv.record_dir.resolve() or not path.is_file():
                        continue
                    zf.write(path, arcname=path.name)
                except Exception:
                    continue
    except Exception as e:
        with contextlib.suppress(Exception):
            os.unlink(temp_zip_path)
        return jsonify({"error": f"Failed to generate zip: {e}"}), 500

    return send_file(
        temp_zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="cheapsecurity_recordings.zip",
    )


@app.route("/api/recordings/telegram", methods=["POST"])
def api_send_telegram_recordings() -> RouteReturn:
    """Send selected recordings to Telegram.
    ---
    tags:
      - recordings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            filenames:
              type: array
              items: {type: string}
              example: ["motion_20260101_120000.avi"]
    responses:
      200:
        description: Per-file send results
        examples:
          application/json:
            results:
              - filename: motion_20260710_232000.avi
                sent: true
      400:
        description: No filenames provided
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])
    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    results = []
    for name in filenames:
        path = cctv.record_dir / name
        try:
            if path.resolve().parent != cctv.record_dir.resolve() or not path.is_file():
                results.append({"filename": name, "sent": False, "error": "Invalid file"})
                continue
            # Async upload in background thread to avoid blocking the single-worker Gunicorn server
            threading.Thread(
                target=cctv._send_telegram_video,
                args=(path,),
                daemon=True,
            ).start()
            results.append({"filename": name, "sent": True})
        except Exception as e:
            results.append({"filename": name, "sent": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/telegram/delete", methods=["POST"])
def api_delete_telegram_message() -> RouteReturn:
    """Delete a previously sent Telegram message by message_id.
    ---
    tags:
      - telegram
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            message_id:
              type: integer
              example: 12345
    responses:
      200:
        description: Deletion result
        examples:
          application/json:
            deleted: true
            message_id: 12345
      400:
        description: Missing or invalid message_id
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    message_id = data.get("message_id")
    if message_id is None:
        return jsonify({"error": "message_id is required"}), 400
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return jsonify({"error": "message_id must be an integer"}), 400

    chat_id = cctv.cfg.get("telegram", {}).get("chat_id") if cctv.telegram_enabled else None
    if not chat_id:
        return jsonify({"error": "Telegram not configured"}), 400

    deleted = cctv._delete_telegram_message(message_id, chat_id)
    return jsonify({"deleted": deleted, "message_id": message_id})


@app.route("/api/telegram/delete_range", methods=["POST"])
def api_delete_telegram_range() -> RouteReturn:
    """Delete Telegram messages whose IDs fall in a range.

    Every ID in [min_id, max_id] is sent to Telegram's deleteMessage API,
    regardless of whether it is stored in the local tracking file.
    ---
    tags:
      - telegram
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            min_id:
              type: integer
              example: 12340
            max_id:
              type: integer
              example: 12350
    responses:
      200:
        description: Range deletion result
        examples:
          application/json:
            deleted: 5
            failed: 6
            range: [12340, 12350]
      400:
        description: Missing/invalid range, min_id > max_id, or Telegram not configured
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    min_id = data.get("min_id")
    max_id = data.get("max_id")
    if min_id is None or max_id is None:
        return jsonify({"error": "min_id and max_id are required"}), 400
    try:
        min_id = int(min_id)
        max_id = int(max_id)
    except (TypeError, ValueError):
        return jsonify({"error": "min_id and max_id must be integers"}), 400

    if min_id > max_id:
        return jsonify({"error": "min_id must be <= max_id"}), 400

    chat_id = cctv.cfg.get("telegram", {}).get("chat_id") if cctv.telegram_enabled else None
    if not chat_id:
        return jsonify({"error": "Telegram not configured"}), 400

    ids_to_delete = list(range(min_id, max_id + 1))
    deleted = 0
    failed = 0
    for message_id in ids_to_delete:
        if cctv._delete_telegram_message(message_id, chat_id):
            deleted += 1
        else:
            failed += 1
    return jsonify({"deleted": deleted, "failed": failed, "range": [min_id, max_id]})


@app.route("/api/settings")
def api_settings() -> RouteReturn:
    """Get current toggleable settings.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    responses:
      200:
        description: Current settings
        schema:
          type: object
          properties:
            night_mode: {type: boolean}
            night_mode_strength: {type: string}
            night_device_active: {type: boolean}
            night_device_configured: {type: boolean}
            notifications_enabled: {type: boolean}
            telegram_enabled: {type: boolean}
            auth_enabled: {type: boolean}
        examples:
          application/json:
            night_mode: false
            night_mode_strength: normal
            night_device_active: false
            night_device_configured: false
            notifications_enabled: false
            telegram_enabled: true
            auth_enabled: true
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify(
        {
            "night_mode": cctv.night_mode,
            "night_mode_strength": cctv.night_mode_strength,
            "night_device_active": cctv.night_device_active,
            "night_device_configured": cctv.night_device is not None,
            "notifications_enabled": cctv.notifications_enabled,
            "telegram_enabled": cctv.telegram_enabled,
            "gdrive_enabled": cctv.gdrive_enabled,
            "onedrive_enabled": cctv.onedrive_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/settings/telegram", methods=["POST"])
def api_set_telegram() -> RouteReturn:
    """Enable or disable Telegram uploads.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
    responses:
      200:
        description: New Telegram setting
        examples:
          application/json:
            telegram_enabled: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.telegram_enabled))
    cctv.set_telegram_enabled(enabled)
    return jsonify({"telegram_enabled": enabled})


@app.route("/api/settings/gdrive", methods=["POST"])
def api_set_gdrive() -> RouteReturn:
    """Enable or disable Google Drive uploads.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
    responses:
      200:
        description: New Google Drive setting
        examples:
          application/json:
            gdrive_enabled: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.gdrive_enabled))
    cctv.set_gdrive_enabled(enabled)
    return jsonify({"gdrive_enabled": enabled})


@app.route("/api/settings/onedrive", methods=["POST"])
def api_set_onedrive() -> RouteReturn:
    """Enable or disable OneDrive uploads.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
    responses:
      200:
        description: New OneDrive setting
        examples:
          application/json:
            onedrive_enabled: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.onedrive_enabled))
    cctv.set_onedrive_enabled(enabled)
    return jsonify({"onedrive_enabled": enabled})


@app.route("/api/settings/night_mode", methods=["POST"])
def api_set_night_mode() -> RouteReturn:
    """Enable or disable night mode and optionally set its strength.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
            strength: {type: string, example: low}
    responses:
      200:
        description: New night mode setting
        examples:
          application/json:
            night_mode: true
            night_mode_strength: low
            night_device_active: true
            night_device_configured: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.night_mode))
    strength = data.get("strength", cctv.night_mode_strength)
    cctv.set_night_mode(enabled)
    if isinstance(strength, str):
        cctv.set_night_mode_strength(strength)
    return jsonify(
        {
            "night_mode": cctv.night_mode,
            "night_mode_strength": cctv.night_mode_strength,
            "night_device_active": cctv.night_device_active,
            "night_device_configured": cctv.night_device is not None,
        }
    )


@app.route("/api/settings/notifications", methods=["POST"])
def api_set_notifications() -> RouteReturn:
    """Enable or disable email notifications.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
    responses:
      200:
        description: New notifications setting
        examples:
          application/json:
            notifications_enabled: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.notifications_enabled))
    cctv.set_notifications_enabled(enabled)
    return jsonify({"notifications_enabled": enabled})


@app.route("/api/settings/auth", methods=["POST"])
def api_set_auth() -> RouteReturn:
    """Enable or disable built-in HTTP Basic Auth.
    ---
    tags:
      - settings
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            enabled: {type: boolean, example: true}
    responses:
      200:
        description: New auth setting
        examples:
          application/json:
            auth_enabled: true
      403:
        description: CSRF protection triggered
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(
        data.get("enabled", cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False))
    )
    cctv.set_auth_enabled(enabled)
    return jsonify({"auth_enabled": enabled})


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot() -> RouteReturn:
    """Capture and return a single JPEG snapshot from the live camera feed.
    ---
    tags:
      - camera
    security:
      - basicAuth: []
    produces:
      - image/jpeg
    responses:
      200:
        description: JPEG snapshot image
        schema:
          type: string
          format: binary
      503:
        description: CCTV not initialized or no frame available
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    frame = cctv.get_frame()
    if frame is None:
        return jsonify({"error": "No camera frame available"}), 503
    return Response(frame, mimetype="image/jpeg")


@app.route("/api/video", methods=["POST"])
def api_video() -> RouteReturn:
    """Trigger a manual video recording for a configurable number of seconds.
    ---
    tags:
      - camera
    security:
      - basicAuth: []
    parameters:
      - name: body
        in: body
        required: false
        schema:
          type: object
          properties:
            seconds:
              type: integer
              minimum: 1
              maximum: 60
              default: 10
              example: 10
    responses:
      200:
        description: Recording started
        schema:
          type: object
          properties:
            recording_until: {type: number}
            chat_id: {type: string, nullable: true}
        examples:
          application/json:
            recording_until: 1752193290.5
            chat_id: "123456789"
      400:
        description: Invalid seconds value
      503:
        description: CCTV engine not initialized
    """
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    try:
        seconds = int(data.get("seconds", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "seconds must be an integer"}), 400
    seconds = max(1, min(60, seconds))
    chat_id = cctv.cfg.get("telegram", {}).get("chat_id") if cctv.telegram_enabled else None
    cctv.trigger_manual_recording(seconds, chat_id)
    return jsonify({"recording_until": time.time() + seconds, "chat_id": chat_id})


@app.route("/recordings/<path:filename>")
def serve_recording(filename: str) -> RouteReturn:
    if cctv is None:
        return "CCTV not initialized", 503
    record_dir = str(cctv.record_dir)
    return send_from_directory(record_dir, filename)


@app.route("/video_feed")
def video_feed() -> RouteReturn:
    if cctv is None:
        return "CCTV not initialized", 503

    def generate() -> Iterator[bytes]:
        while True:
            frame = cctv.get_frame()
            if frame:
                yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                # Pace the MJPEG stream to the camera's actual rate instead of
                # spinning as fast as the network allows on the same frame.
                fps = max(1.0, cctv.actual_fps) if cctv else 1.0
                time.sleep(1.0 / fps)
            else:
                time.sleep(0.05)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import signal

    config_path = os.environ.get("CHEAPSECURITY_CONFIG", "config.json")
    system = init_cctv(config_path)

    def _shutdown(signum: int, frame: FrameType | None) -> None:
        system.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = system.cfg["web"]["host"]
    port = system.cfg["web"]["port"]
    print(f"CheapSecurity running at http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, debug=False)
