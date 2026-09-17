#!/usr/bin/env python3

import cv2
import numpy as np
import pytest

from cheapsecurity.cctv import CCTVSystem


def _write_test_avi(path, fps=30.0, frames=10, frame_size=(64, 48)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), fps, frame_size)
    assert writer.isOpened()
    for _ in range(frames):
        writer.write(np.zeros((frame_size[1], frame_size[0], 3), dtype=np.uint8))
    writer.release()


def _read_fps_and_frames(path):
    cap = cv2.VideoCapture(str(path))
    assert cap.isOpened()
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return fps, frame_count


def test_patch_avi_fps_updates_rate(tmp_path):
    video = tmp_path / "t.avi"
    _write_test_avi(video, fps=30.0, frames=10)

    assert CCTVSystem._patch_avi_fps(video, 10.0) is True

    fps, frames = _read_fps_and_frames(video)
    assert abs(fps - 10.0) < 0.5
    assert frames == 10


def test_patch_avi_fps_fractional_rate(tmp_path):
    video = tmp_path / "t.avi"
    _write_test_avi(video, fps=30.0, frames=21)

    assert CCTVSystem._patch_avi_fps(video, 10.5) is True

    fps, _ = _read_fps_and_frames(video)
    assert abs(fps - 10.5) < 0.2


def test_fix_video_duration_patches_header(patched_config, tmp_path):
    system = CCTVSystem(patched_config)
    video = tmp_path / "t.avi"
    _write_test_avi(video, fps=30.0, frames=10)

    system._fix_video_duration(
        video, actual_duration=5.0, frames_written=10, writer_fps=30.0, device=0
    )

    fps, frames = _read_fps_and_frames(video)
    assert abs(fps - 2.0) < 0.2
    assert frames == 10
    assert system._learned_record_fps[("idx", 0)] == pytest.approx(2.0)


def test_fix_video_duration_skips_small_drift(patched_config, tmp_path):
    system = CCTVSystem(patched_config)
    video = tmp_path / "t.avi"
    _write_test_avi(video, fps=30.0, frames=300)

    system._fix_video_duration(video, actual_duration=10.1, frames_written=300, writer_fps=30.0)

    fps, _ = _read_fps_and_frames(video)
    assert abs(fps - 30.0) < 0.5


def test_create_writer_prefers_learned_fps(patched_config, tmp_path):
    system = CCTVSystem(patched_config)
    system._learned_record_fps[("idx", 0)] = 12.0

    writer = system._create_writer(str(tmp_path / "t.avi"), 64, 48)
    assert writer is not None and writer.isOpened()
    writer.release()

    assert system._writer_fps == 12.0


def test_create_writer_falls_back_to_measured_fps(patched_config, tmp_path):
    system = CCTVSystem(patched_config)
    system.measured_fps = 14.0

    writer = system._create_writer(str(tmp_path / "t.avi"), 64, 48)
    assert writer is not None and writer.isOpened()
    writer.release()

    assert system._writer_fps == 14.0
