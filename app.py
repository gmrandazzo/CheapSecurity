#!/usr/bin/env python3
"""
CheapSecurity launcher.

Starts the CCTV engine and the web dashboard.
"""

import os
import signal

from cctv import CCTVSystem
from web import app, init_cctv


def main():
    config_path = os.environ.get("CHEAPSECURITY_CONFIG", "config.json")
    system = init_cctv(config_path)

    def _shutdown(signum, frame):
        system.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = system.cfg["web"]["host"]
    port = system.cfg["web"]["port"]
    print(f"CheapSecurity running at http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
