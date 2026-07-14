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
CheapSecurity launcher.

Starts the CCTV engine and the web dashboard.
"""

import os
import signal
from types import FrameType

from cheapsecurity.web import app, init_cctv


def main() -> None:
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


if __name__ == "__main__":
    main()
