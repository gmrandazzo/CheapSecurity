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
import io
import os
import time
import zipfile
from collections.abc import Iterator
from types import FrameType
from typing import TypeAlias

import cv2
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from cheapsecurity.cctv import CCTVSystem

app = Flask(__name__)
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


@app.route("/")
def index() -> str:
    cfg = cctv.cfg if cctv else {}
    return str(render_template("index.html", cfg=cfg))


@app.route("/api/status")
def api_status() -> RouteReturn:
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
            "notifications_enabled": cctv.notifications_enabled,
            "telegram_enabled": cctv.telegram_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/recordings")
def api_recordings() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify({"recordings": cctv.list_recordings()})


@app.route("/api/recordings/delete", methods=["POST"])
def api_delete_recordings() -> RouteReturn:
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
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])
    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in filenames:
            path = cctv.record_dir / name
            try:
                if path.resolve().parent != cctv.record_dir.resolve() or not path.is_file():
                    continue
                zf.write(path, arcname=path.name)
            except Exception:
                continue

    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name="cheapsecurity_recordings.zip",
    )


@app.route("/api/recordings/telegram", methods=["POST"])
def api_send_telegram_recordings() -> RouteReturn:
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
            cctv._send_telegram_video(path)
            results.append({"filename": name, "sent": True})
        except Exception as e:
            results.append({"filename": name, "sent": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/settings")
def api_settings() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify(
        {
            "night_mode": cctv.night_mode,
            "notifications_enabled": cctv.notifications_enabled,
            "telegram_enabled": cctv.telegram_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/settings/telegram", methods=["POST"])
def api_set_telegram() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.telegram_enabled))
    cctv.set_telegram_enabled(enabled)
    return jsonify({"telegram_enabled": enabled})


@app.route("/api/settings/night_mode", methods=["POST"])
def api_set_night_mode() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.night_mode))
    cctv.set_night_mode(enabled)
    return jsonify({"night_mode": enabled})


@app.route("/api/settings/notifications", methods=["POST"])
def api_set_notifications() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.notifications_enabled))
    cctv.set_notifications_enabled(enabled)
    return jsonify({"notifications_enabled": enabled})


@app.route("/api/settings/auth", methods=["POST"])
def api_set_auth() -> RouteReturn:
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(
        data.get("enabled", cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False))
    )
    cctv.set_auth_enabled(enabled)
    return jsonify({"auth_enabled": enabled})


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
