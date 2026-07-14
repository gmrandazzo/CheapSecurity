#!/usr/bin/env python3
"""
CheapSecurity diagnostic script.

Helps troubleshoot why recordings are not being created.
"""

import json
import shutil
import time
from pathlib import Path

import cv2


def main() -> None:
    print("=" * 60)
    print("CheapSecurity Diagnostics")
    print("=" * 60)

    # Load config
    try:
        with open("config.json") as f:
            cfg = json.load(f)
        print("\n✓ config.json loaded")
    except Exception as e:
        print(f"\n✗ Failed to load config.json: {e}")
        return

    cam_cfg = cfg["camera"]
    motion_cfg = cfg["motion"]
    rec_cfg = cfg["recording"]
    notif_cfg = cfg.get("notifications", {})

    device = cam_cfg["device"]
    print(f"\nCamera device: /dev/video{device}")
    print(f"Requested resolution: {cam_cfg['width']}x{cam_cfg['height']} @ {cam_cfg['fps']} fps")
    print(
        f"Motion threshold: {motion_cfg['threshold']}, min_area: {motion_cfg['min_area']}, scale: {motion_cfg.get('scale', 1.0)}"
    )
    print(f"Night mode: {cam_cfg.get('night_mode', False)}")
    print(f"Notifications enabled: {notif_cfg.get('enabled', False)}")

    # Check recordings directory
    record_dir = Path(rec_cfg["dir"]).resolve()
    print(f"\nRecordings directory: {record_dir}")
    if record_dir.exists():
        print("✓ Directory exists")
        free = shutil.disk_usage(record_dir).free
        print(f"  Free disk space: {free / (1024**3):.2f} GB")
        files = sorted(record_dir.iterdir())
        print(f"  Existing recordings: {len(files)}")
        for rec_file in files[:5]:
            print(f"    - {rec_file.name} ({rec_file.stat().st_size} bytes)")
        if len(files) > 5:
            print(f"    ... and {len(files) - 5} more")
    else:
        print("✗ Directory does not exist")

    # Try opening camera
    print("\nTrying to open camera...")
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        print("✗ Cannot open camera")
        return

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_cfg["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_cfg["height"])
    cap.set(cv2.CAP_PROP_FPS, cam_cfg["fps"])

    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    print("✓ Camera opened")
    print(f"  Actual resolution: {actual_width}x{actual_height} @ {actual_fps:.1f} fps")

    # Motion detection test
    print("\nRunning 5-second motion detection test...")
    print("Move in front of the camera to see 'MOTION DETECTED' messages.")

    threshold = motion_cfg["threshold"]
    min_area = motion_cfg["min_area"]
    scale = max(0.05, min(1.0, motion_cfg.get("scale", 1.0)))
    blur_size = max(1, motion_cfg["blur_size"] // 2 * 2 + 1)
    prev_gray = None
    motion_count = 0

    for _ in range(50):  # ~5 seconds at 10 fps
        ok, frame = cap.read()
        if not ok:
            print("✗ Frame capture failed")
            break

        small = cv2.resize(frame, (0, 0), fx=scale, fy=scale) if scale < 1.0 else frame

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (blur_size, blur_size), 0)

        if prev_gray is None:
            prev_gray = gray
            time.sleep(0.1)
            continue

        diff = cv2.absdiff(prev_gray, gray)
        _, thresh = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        prev_gray = gray

        area_factor = 1.0 / (scale**2)
        detected = any(cv2.contourArea(cnt) * area_factor >= min_area for cnt in contours)
        if detected:
            motion_count += 1
            print("  MOTION DETECTED")
        time.sleep(0.1)

    print(f"\nMotion detected in {motion_count}/50 frames")
    if motion_count == 0:
        print(
            "⚠ No motion was detected. Try waving in front of the camera or lowering motion.min_area."
        )

    cap.release()
    print("\nDiagnostics complete.")


if __name__ == "__main__":
    main()
