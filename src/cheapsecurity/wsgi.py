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
Production WSGI entry point for CheapSecurity.

Starts the CCTV engine once and exposes the Flask application to Gunicorn.
Use only one Gunicorn worker so the camera is opened by a single process.
"""

import os

from cheapsecurity.web import app, init_cctv

config_path = os.environ.get("CHEAPSECURITY_CONFIG", "config.json")
init_cctv(config_path)

application = app
