"""Unit tests for diagnose.py."""

import json
from unittest.mock import MagicMock, patch

import numpy as np

from cheapsecurity.diagnose import main


def test_diagnose_main_missing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No config.json exists -> prints error and exits gracefully
    main()


def test_diagnose_main_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record_dir = tmp_path / "recordings"
    record_dir.mkdir()
    (record_dir / "rec1.avi").write_bytes(b"DATA")

    config = {
        "camera": {"device": 0, "width": 640, "height": 480, "fps": 15},
        "motion": {"threshold": 25, "min_area": 500, "blur_size": 21, "scale": 0.5},
        "recording": {"dir": str(record_dir)},
        "notifications": {"enabled": True},
    }
    (tmp_path / "config.json").write_text(json.dumps(config))

    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    mock_cap.get.return_value = 15.0
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    mock_cap.read.return_value = (True, frame)

    with patch("cv2.VideoCapture", return_value=mock_cap):
        main()
