# CheapSecurity

A lightweight CCTV system for the Odroid XU4 (or any Linux board) with a USB webcam.

- **Motion detection** with frame differencing
- **Automatic recording** with a pre-motion buffer
- **Web dashboard** for live streaming and playback
- **Storage cleanup** by age and total size

## Requirements

- Python 3
- OpenCV with V4L2 support (must be installed manually on the board)
- A USB webcam (`/dev/video0` by default)

## Quick start

1. Verify OpenCV is available:

   ```bash
   python3 -c "import cv2; print(cv2.__version__)"
   ```

2. Create and activate the virtual environment:

   ```bash
   python3 -m venv venv --system-site-packages
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Find your webcam device (usually `/dev/video0`):

   ```bash
   v4l2-ctl --list-devices
   ```

4. Adjust `config.json` if needed (camera device, resolution, motion sensitivity, etc.).
5. If you want email alerts, fill in the `notifications.smtp` section with real credentials and recipients.

6. Run the app:

   ```bash
   ./venv/bin/python app.py
   ```

7. Open the dashboard in a browser:

   ```
   http://<odroid-ip>:5000
   ```

## Autostart with systemd

Copy the service template and enable it for your user (replace `marco` with your username):

```bash
sudo cp cheapsecurity.service /etc/systemd/system/cheapsecurity@.service
sudo systemctl daemon-reload
sudo systemctl enable --now cheapsecurity@marco.service
```

View logs:

```bash
sudo journalctl -u cheapsecurity@marco.service -f
```

## Configuration

Edit `config.json`:

| Section | Key | Description |
|---------|-----|-------------|
| `camera` | `device` | V4L2 device index (`0` = `/dev/video0`) |
| `camera` | `width`, `height`, `fps` | Capture resolution and frame rate |
| `motion` | `threshold` | Pixel difference threshold (0-255) |
| `motion` | `min_area` | Minimum contour area to trigger motion (full-res pixels) |
| `motion` | `cooldown_seconds` | Keep recording after motion stops |
| `motion` | `scale` | Downscale factor for motion detection (saves CPU) |
| `recording` | `dir` | Where videos are saved |
| `recording` | `max_duration_seconds` | Maximum length of one clip |
| `recording` | `pre_buffer_seconds` | Seconds before motion included in clip |
| `recording` | `codec` | Preferred FourCC codec (`MJPG` for low CPU, `mp4v` for smaller files) |
| `notifications` | `enabled` | Send email alerts on motion |
| `notifications` | `smtp` | SMTP server, port, username, password, TLS |
| `notifications` | `from`, `to`, `subject` | Email sender/recipients/subject |
| `notifications` | `min_interval_minutes` | Minimum time between alert emails |
| `storage` | `max_age_days` | Delete recordings older than this (default 3 days = 72h) |
| `storage` | `max_size_gb` | Delete oldest files if total exceeds this |
| `storage` | `cleanup_interval_minutes` | How often storage cleanup runs |
| `storage` | `emergency_free_space_gb` | If free disk space drops below this, delete old recordings before a new one |
| `storage` | `emergency_delete_count` | How many oldest recordings to delete in an emergency cleanup |
| `web` | `host`, `port` | Dashboard bind address and port |
| `web` | `stream_scale` | Downscale factor for live stream (saves bandwidth/CPU) |
| `web.auth` | `enabled`, `username`, `password` | Optional HTTP Basic Auth |

## Project structure

```
CheapSecurity/
├── app.py                 # Main launcher
├── cctv.py                # Motion detection & recording engine
├── web.py                 # Flask dashboard & APIs
├── config.json            # Settings
├── requirements.txt       # Python deps (OpenCV installed manually)
├── cheapsecurity.service  # systemd template
├── recordings/            # Saved videos
├── templates/             # HTML templates
└── static/                # CSS/JS
```

## Production deployment

Do **not** expose Flask's development server to the internet. Use **Gunicorn** + **nginx**.

### 1. Install Gunicorn

It is already in `requirements.txt` and the venv:

```bash
cd /home/marco/CheapSecurity
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Run with systemd + Gunicorn

Copy the service template and enable it (replace `marco` with your username):

```bash
sudo cp cheapsecurity.service /etc/systemd/system/cheapsecurity@.service
sudo systemctl daemon-reload
sudo systemctl enable --now cheapsecurity@marco.service
```

This binds Gunicorn to `127.0.0.1:5000` with **one worker and four threads**. Only one worker is used because the camera must be opened by a single process.

View logs:

```bash
sudo journalctl -u cheapsecurity@marco.service -f
```

### 3. Put nginx in front

Install nginx and use the provided example:

```bash
sudo apt install nginx
sudo cp nginx.example.conf /etc/nginx/sites-available/cheapsecurity
sudo nano /etc/nginx/sites-available/cheapsecurity   # edit server_name and paths
sudo ln -s /etc/nginx/sites-available/cheapsecurity /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Key nginx settings for the MJPEG stream:

```nginx
proxy_buffering off;
proxy_cache off;
```

### 4. HTTPS with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d cctv.example.com
```

### 5. Authentication

Disable the built-in Flask auth (`web.auth.enabled: false`) and let nginx handle it:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

Then uncomment the `auth_basic` lines in the nginx config and reload.

## Notes

- The default config is set to **2560×1440 (2K)**. If the Odroid XU4 struggles, lower the camera resolution or increase `motion.scale`/`web.stream_scale`.
- `motion.scale` lets motion detection run on a smaller image (default 0.25 = 640×400 for 2K input), saving CPU. `min_area` is still expressed in full-resolution pixels.
- `web.stream_scale` sends a smaller live stream (default 0.5 = 1280×720 for 2K input) to reduce bandwidth and CPU.
- Recordings are always saved at the full camera resolution.
- The default codec is `MJPG` (`.avi`) because it is much easier on the Odroid CPU than `mp4v`. You can switch to `mp4v` (`.mp4`) for smaller files if CPU usage is acceptable.
- The pre-motion buffer stores compressed JPEG frames instead of raw images to keep RAM usage reasonable at high resolutions.
- **Storage**: recordings older than 72 hours are deleted automatically. If free disk space drops below `emergency_free_space_gb`, the oldest 4 recordings are deleted before starting a new clip.
- **Email alerts**: configure the `notifications` section in `config.json`. A picture from the moment motion starts is attached. Alerts are rate-limited by `min_interval_minutes` to avoid spam.
- **Web toggles**: the dashboard has checkboxes to enable/disable email notifications and built-in basic auth. Changes are saved to `config.json`.
- You can disable `web.auth.enabled` and handle authentication in **nginx** instead. Restart the app after changing auth settings from the UI so nginx/Flask state stays consistent.
- For production use, put the app behind a reverse proxy (nginx/Caddy) with HTTPS instead of exposing Flask directly.
- If `mp4v` does not work, the recorder automatically falls back to `MJPG` or `XVID`.
- The live stream is MJPEG and can be viewed directly at `/video_feed`.
