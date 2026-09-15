#!/usr/bin/env python3
"""
Tests for AUTO camera detection (camera.device / camera.night_device = "auto").
"""

import json

import numpy as np
import pytest
from helpers import FakeCapture

from cheapsecurity.cctv import CCTVSystem


@pytest.fixture
def auto_config_path(config_dict, tmp_path):
    """A config file with AUTO device selection enabled."""
    config_dict["camera"]["device"] = "auto"
    config_dict["camera"]["night_device"] = "auto"
    config_dict["recording"]["dir"] = str(tmp_path / "recordings")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_dict))
    return str(config_path)


@pytest.fixture
def auto_system(auto_config_path):
    return CCTVSystem(auto_config_path)


def _patch_probing(monkeypatch, system, devices, probes, caps=None):
    """Stub out hardware access for _resolve_auto_devices.

    devices: indices returned by enumeration
    probes:  {index: (score, kind) or None} from probing
    caps:    {index: (name, is_capture) or None} from VIDIOC_QUERYCAP
    """
    monkeypatch.setattr(CCTVSystem, "_enumerate_video_devices", staticmethod(lambda: devices))
    monkeypatch.setattr(
        CCTVSystem,
        "_v4l2_device_caps",
        staticmethod(lambda i: (caps or {}).get(i, (f"Cam{i}", True))),
    )
    monkeypatch.setattr(system, "_probe_device", lambda dev: probes.get(dev))


class TestAutoFlags:
    def test_auto_config_sets_flags(self, auto_system):
        assert auto_system._auto_device is True
        assert auto_system._auto_night_device is True
        assert auto_system._auto_resolved is False
        assert auto_system.night_device is None

    def test_explicit_config_disables_auto(self, patched_config):
        system = CCTVSystem(patched_config)
        assert system._auto_device is False
        assert system._auto_night_device is False
        assert system._auto_resolved is True
        assert system.device == 0
        assert system.night_device is None


class TestAnalyzeFrame:
    def test_grayscale_frame_is_mono(self):
        gray = np.zeros((60, 80), dtype=np.uint8)
        assert CCTVSystem._analyze_frame(gray) == (0.0, "mono")

    def test_neutral_chroma_frame_is_mono(self):
        # YUYV from a monochrome sensor decodes to equal BGR channels.
        bgr_gray = np.full((60, 80, 3), 128, dtype=np.uint8)
        score, kind = CCTVSystem._analyze_frame(bgr_gray)
        assert kind == "mono"
        assert score == 0.0

    def test_multihue_frame_is_color(self):
        frame = np.zeros((60, 80, 3), dtype=np.uint8)
        frame[:, :27] = (0, 0, 255)  # red
        frame[:, 27:54] = (0, 255, 0)  # green
        frame[:, 54:] = (255, 0, 0)  # blue
        score, kind = CCTVSystem._analyze_frame(frame)
        assert kind == "color"
        assert score > 100.0

    def test_single_hue_vivid_frame_is_tinted(self):
        # IR-cut-removed color sensors show a uniform tint (e.g. all red).
        red = np.zeros((60, 80, 3), dtype=np.uint8)
        red[..., 2] = 255
        score, kind = CCTVSystem._analyze_frame(red)
        assert kind == "tinted"
        assert score > 100.0


class TestResolveAutoDevices:
    def test_picks_color_day_and_mono_night(self, auto_system, monkeypatch):
        _patch_probing(
            monkeypatch, auto_system, [0, 1, 2, 3], {0: (55.0, "color"), 2: (0.0, "mono")}
        )
        assert auto_system._resolve_auto_devices() is True
        assert auto_system.device == 0
        assert auto_system.night_device == 2

    def test_tinted_camera_becomes_night(self, auto_system, monkeypatch):
        # IR-tinted sensor out-scores the plain color one but must not be day.
        _patch_probing(
            monkeypatch, auto_system, [0, 2], {0: (50.0, "color"), 2: (90.0, "tinted")}
        )
        assert auto_system._resolve_auto_devices() is True
        assert auto_system.device == 0
        assert auto_system.night_device == 2

    def test_color_camera_preferred_for_day_even_with_lower_score(
        self, auto_system, monkeypatch
    ):
        # A tinted IR sensor may out-score the plain color camera, but the
        # color camera must still win the day slot.
        _patch_probing(
            monkeypatch, auto_system, [0, 2], {0: (10.0, "color"), 2: (90.0, "tinted")}
        )
        assert auto_system._resolve_auto_devices() is True
        assert auto_system.device == 0
        assert auto_system.night_device == 2

    def test_index_shuffle_still_resolves(self, auto_system, monkeypatch):
        # USB enumeration order changed: color camera is now video4.
        _patch_probing(
            monkeypatch, auto_system, [2, 3, 4, 5], {4: (60.0, "color"), 2: (0.0, "mono")}
        )
        assert auto_system._resolve_auto_devices() is True
        assert auto_system.device == 4
        assert auto_system.night_device == 2

    def test_no_night_camera_uses_single_camera(self, auto_system, monkeypatch):
        _patch_probing(
            monkeypatch, auto_system, [0, 2], {0: (55.0, "color"), 2: (48.0, "color")}
        )
        assert auto_system._resolve_auto_devices() is True
        assert auto_system.device == 0
        assert auto_system.night_device is None

    def test_metadata_and_codec_nodes_are_skipped(self, auto_system, monkeypatch):
        calls = []

        def _probe(dev):
            calls.append(dev)
            return (55.0, "color") if dev == 0 else ((0.0, "mono") if dev == 2 else None)

        caps = {1: ("UVC Metadata", False), 3: ("bcm2835-codec", False)}
        monkeypatch.setattr(
            CCTVSystem, "_enumerate_video_devices", staticmethod(lambda: [0, 1, 2, 3])
        )
        monkeypatch.setattr(
            CCTVSystem,
            "_v4l2_device_caps",
            staticmethod(lambda i: caps.get(i, (f"Cam{i}", True))),
        )
        monkeypatch.setattr(auto_system, "_probe_device", _probe)

        assert auto_system._resolve_auto_devices() is True
        assert sorted(calls) == [0, 2]
        assert auto_system.device == 0
        assert auto_system.night_device == 2

    def test_duplicate_node_names_are_skipped(self, auto_system, monkeypatch):
        # Same physical camera exposing two capture nodes.
        calls = []

        def _probe(dev):
            calls.append(dev)
            return (55.0, "color") if dev == 0 else ((0.0, "mono") if dev == 2 else None)

        caps = {0: ("FHD Camera", True), 1: ("FHD Camera", True), 2: ("2K HD Camera", True)}
        monkeypatch.setattr(CCTVSystem, "_enumerate_video_devices", staticmethod(lambda: [0, 1, 2]))
        monkeypatch.setattr(CCTVSystem, "_v4l2_device_caps", staticmethod(lambda i: caps[i]))
        monkeypatch.setattr(auto_system, "_probe_device", _probe)

        assert auto_system._resolve_auto_devices() is True
        assert 1 not in calls
        assert auto_system.device == 0
        assert auto_system.night_device == 2

    def test_no_working_camera_returns_false(self, auto_system, monkeypatch):
        _patch_probing(monkeypatch, auto_system, [0, 2], {})
        assert auto_system._resolve_auto_devices() is False
        assert auto_system._auto_resolved is False


class TestOpenCapture:
    def test_open_capture_fails_when_auto_unresolved(self, auto_system, monkeypatch):
        monkeypatch.setattr(auto_system, "_resolve_auto_devices", lambda: False)
        assert auto_system._open_capture() is False
        assert auto_system._auto_resolved is False

    def test_open_capture_uses_resolved_devices(self, auto_system, monkeypatch):
        _patch_probing(
            monkeypatch, auto_system, [0, 2], {0: (55.0, "color"), 2: (0.0, "mono")}
        )
        monkeypatch.setattr("cv2.VideoCapture", lambda *a, **k: FakeCapture(640, 480, 15))

        assert auto_system._open_capture() is True
        assert auto_system._auto_resolved is True
        assert auto_system.cap is not None
        auto_system._release_capture()
