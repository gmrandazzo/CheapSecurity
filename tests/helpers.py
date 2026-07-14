"""Test helpers."""

import cv2
import numpy as np


class FakeCapture:
    """Fake cv2.VideoCapture for unit tests."""

    def __init__(self, width=640, height=480, fps=15):
        self._width = width
        self._height = height
        self._fps = fps
        self._props = {}
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)

    def isOpened(self):  # noqa: N802
        return True

    def read(self):
        return True, self._frame.copy()

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self._width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self._height
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        return self._props.get(prop, -1.0)

    def set(self, prop, value):
        self._props[prop] = value
        return True

    def release(self):
        pass
