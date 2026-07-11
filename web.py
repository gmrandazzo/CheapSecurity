#!/usr/bin/env python3
"""
CheapSecurity web interface.

Serves a small dashboard with a live MJPEG stream, a list of recordings,
and direct playback/download links.
"""

import base64
import os
import threading
import time
from pathlib import Path

import cv2

from flask import Flask, Response, jsonify, make_response, render_template, request, send_from_directory

from cctv import CCTVSystem

app = Flask(__name__)
cctv: CCTVSystem | None = None


def init_cctv(config_path: str = "config.json") -> CCTVSystem:
    global cctv
    if cctv is None:
        cctv = CCTVSystem(config_path)
        cctv.start()
    return cctv


def _check_auth():
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
def require_auth():
    return _check_auth()


@app.route("/")
def index():
    cfg = cctv.cfg if cctv else {}
    return render_template("index.html", cfg=cfg)


@app.route("/api/status")
def api_status():
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify(
        {
            "running": cctv.running,
            "is_recording": cctv.is_recording,
            "motion_active": cctv.motion_active,
            "recording_file": cctv.recording_path.name if cctv.recording_path else None,
            "resolution": f"{cctv.cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cctv.cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}" if cctv.cap else f"{cctv.width}x{cctv.height}",
            "fps": cctv.actual_fps,
            "notifications_enabled": cctv.notifications_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/recordings")
def api_recordings():
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify({"recordings": cctv.list_recordings()})


@app.route("/api/settings")
def api_settings():
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    return jsonify(
        {
            "notifications_enabled": cctv.notifications_enabled,
            "auth_enabled": cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False),
        }
    )


@app.route("/api/settings/notifications", methods=["POST"])
def api_set_notifications():
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.notifications_enabled))
    cctv.set_notifications_enabled(enabled)
    return jsonify({"notifications_enabled": enabled})


@app.route("/api/settings/auth", methods=["POST"])
def api_set_auth():
    if cctv is None:
        return jsonify({"error": "CCTV not initialized"}), 503
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", cctv.cfg.get("web", {}).get("auth", {}).get("enabled", False)))
    cctv.set_auth_enabled(enabled)
    return jsonify({"auth_enabled": enabled})


@app.route("/recordings/<path:filename>")
def serve_recording(filename):
    if cctv is None:
        return "CCTV not initialized", 503
    record_dir = str(cctv.record_dir)
    return send_from_directory(record_dir, filename)


@app.route("/video_feed")
def video_feed():
    if cctv is None:
        return "CCTV not initialized", 503

    def generate():
        while True:
            frame = cctv.get_frame()
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
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

    def _shutdown(signum, frame):
        system.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = system.cfg["web"]["host"]
    port = system.cfg["web"]["port"]
    app.run(host=host, port=port, threaded=True, debug=False)
