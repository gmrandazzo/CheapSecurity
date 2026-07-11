#!/usr/bin/env python3
"""
Production WSGI entry point for CheapSecurity.

Starts the CCTV engine once and exposes the Flask application to Gunicorn.
Use only one Gunicorn worker so the camera is opened by a single process.
"""

import os

from web import app, init_cctv

config_path = os.environ.get("CHEAPSECURITY_CONFIG", "config.json")
init_cctv(config_path)

application = app
