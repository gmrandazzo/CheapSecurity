# CheapSecurity

This project provides a lightweight, self-hosted CCTV solution designed for Linux-based single-board computers (SBCs) and standard USB webcams. It offers an affordable, privacy-focused alternative for home monitoring by keeping your video data entirely under your control.
Project Philosophy

- Privacy-First: By storing all footage locally, this system eliminates the need for third-party cloud subscriptions and ensures your data never leaves your network.
- Cost-Effective: Leverage existing hardware—such as a spare Linux board and a USB webcam—to build a fully functional surveillance system without recurring fees.
- Minimalist Architecture: The software is optimized to run efficiently on low-power devices, ensuring high performance even on entry-level hardware.

Key Features

- Hardware Agnostic: Highly compatible with a wide range of standard USB webcams.
- Resource Efficient: Optimized specifically for Linux-based boards (e.g., Raspberry Pi, Orange Pi, or similar SBCs).
- Data Sovereignty: Full control over your storage path, retention policies, and access methods.
- Simple Deployment: Designed for quick setup and easy maintenance.

## Features

- **Live MJPEG stream** with a web dashboard
- **Motion detection** with frame differencing
- **Automatic recording** with a pre-motion buffer
- **Email alerts** with a snapshot picture when motion starts
- **Telegram integration**:
  - Automatic video upload after motion is recorded
  - Bot commands: `/snapshot`, `/video <seconds>`, `/help`
- **Night mode** low-light enhancement (software CLAHE + brightness/contrast boost)
- **Recordings bulk actions**: select all, send to Telegram, download ZIP, delete
- **Storage cleanup** by age, total size, and emergency low-disk cleanup
- **systemd autostart** and **nginx** reverse-proxy ready
- Licensed under **GNU AGPLv3**

## Requirements

- Python 3
- OpenCV with V4L2 support (must be installed manually on the target ARM board)
- A USB webcam (`/dev/video0` by default)
- Optional: a Telegram bot token for Telegram notifications
- Optional: SMTP credentials for email alerts

## Quick start

1. Verify OpenCV is available:

   ```bash
   python3 -c "import cv2; print(cv2.__version__)"
   ```

2. Create and activate the virtual environment:

   ```bash
   cd /home/marco/CheapSecurity
   python3 -m venv venv --system-site-packages
   source venv/bin/activate
   pip install -e .
   ```

3. Find your webcam device (usually `/dev/video0`):

   ```bash
   v4l2-ctl --list-devices
   ```

4. Copy `config.json.example` to `config.json` and edit it:
   ```bash
   cp config.json.example config.json
   nano config.json
   ```
   - Set camera device, resolution, and frame rate
   - Fill in SMTP credentials if you want email alerts
   - Fill in Telegram bot token and chat ID if you want Telegram uploads

   > **Security:** `config.json` is listed in `.gitignore` and must never be committed. It contains passwords and tokens. Always edit `config.json`, not `config.json.example`. If you add a new setting, update both files so the example stays in sync.

5. Run the app:

   ```bash
   ./venv/bin/python -m cheapsecurity.app
   ```

6. Open the dashboard in a browser:

   ```
   http://<odroid-ip>:5000
   ```

## Configuration

Edit `config.json`:

| Section | Key | Description |
|---------|-----|-------------|
| `camera` | `device` | V4L2 device index (`0` = `/dev/video0`) |
| `camera` | `width`, `height`, `fps` | Capture resolution and frame rate |
| `camera` | `night_mode` | Enable low-light enhancement |
| `camera` | `night_mode_fps` | Target FPS in night mode (camera may ignore this) |
| `camera` | `night_mode_gain` | Target analog gain in night mode (camera may ignore this) |
| `camera` | `night_mode_brightness` | Brightness boost in night mode |
| `camera` | `night_mode_contrast` | Contrast boost in night mode |
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
| `notifications` | `from`, `to`, `subject` | Email sender/recipients/subject (use `["a@...", "b@..."]` for multiple recipients) |
| `notifications` | `min_interval_minutes` | Minimum time between alert emails |
| `telegram` | `enabled` | Send videos to Telegram after motion is recorded |
| `telegram` | `bot_token`, `chat_id` | Telegram Bot API token and destination chat |
| `telegram` | `send_video` | Whether to upload the video file automatically |
| `telegram` | `min_interval_minutes` | Minimum time between Telegram uploads |
| `telegram` | `poll_commands` | Enable `/snapshot`, `/video`, and `/help` bot commands |
| `storage` | `max_age_days` | Delete recordings older than this (default 3 days = 72h) |
| `storage` | `max_size_gb` | Delete oldest files if total exceeds this |
| `storage` | `cleanup_interval_minutes` | How often storage cleanup runs |
| `storage` | `delete_old_on_startup` | If `false`, old recordings are kept when the app restarts |
| `storage` | `emergency_free_space_gb` | If free disk space drops below this, delete old recordings before a new one |
| `storage` | `emergency_delete_count` | How many oldest recordings to delete in an emergency cleanup |
| `web` | `host`, `port` | Dashboard bind address and port |
| `web` | `stream_scale` | Downscale factor for live stream (saves bandwidth/CPU) |
| `web.auth` | `enabled`, `username`, `password` | Optional HTTP Basic Auth |

## Telegram setup

### 1. Create a bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a display name and username.
3. Copy the **bot token** (looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`).
4. Keep this token secret — anyone with it can control your bot.

### 2. Get your chat ID

1. Start a private chat with your new bot and send any message (for example, `/start`).
2. Open this URL in a browser, replacing `<YOUR_BOT_TOKEN>` with the real token:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. Look for `"chat":{"id":123456789`. The number is your **chat ID**.
   - If `getUpdates` is empty, send another message to the bot and refresh.
   - If you want to use a group chat, add the bot to the group first and send a message there; the chat ID will be negative for groups.
4. Copy the chat ID exactly, including the `-` sign if it is a group.

### 3. Configure the app

Fill in the `telegram` section of `config.json`:

```json
"telegram": {
  "enabled": true,
  "bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
  "chat_id": "123456789",
  "send_video": true,
  "min_interval_minutes": 5,
  "poll_commands": true
}
```

Then restart the service:

```bash
sudo systemctl restart cheapsecurity@marco.service
```


### Automatic uploads

After a motion clip is saved, the video is uploaded to your Telegram chat. Uploads are rate-limited by `min_interval_minutes`.

### Bot commands

From your configured chat, send:

- `/snapshot` — receive the current camera picture
- `/video 10` — record and send a 10-second video (1–60 seconds, default 10)
- `/help` — list commands

The bot only responds to your configured `chat_id`.

**Motion has priority:** if the system is already recording because motion was detected, a `/video` request will not interrupt it. The bot will reply that a motion video is in progress and will be uploaded automatically.

## Email alerts

Configure the `notifications` section in `config.json`. A picture from the moment motion starts is attached. Alerts are rate-limited by `min_interval_minutes`.

### Gmail / Google Workspace setup

Google no longer allows "less secure apps" to use your regular Gmail password. You must create an **App Password**.

1. Enable **2-Step Verification** on your Google account:
   - https://myaccount.google.com/signinoptions/two-step-verification
2. Create an App Password:
   - Go to https://myaccount.google.com/apppasswords
   - Select app: **Mail**
   - Select device: **Other (Custom name)** — type "CheapSecurity"
   - Click **Generate** and copy the 16-character password (for example, `abcd efgh ijkl mnop`).
3. In `config.json`, set:
   ```json
   "notifications": {
     "enabled": true,
     "smtp": {
       "server": "smtp.gmail.com",
       "port": 465,
       "username": "you@gmail.com",
       "password": "abcdefghijklmnop",
       "use_tls": true
     },
     "from": "you@gmail.com",
     "to": "you@gmail.com",
     "subject": "CheapSecurity motion alert",
     "min_interval_minutes": 5
   }
   ```
   - Use the **App Password** (no spaces) in the `password` field, not your Google account password.
   - For Google Workspace accounts, the username is usually your full email address.
   - The app uses implicit TLS (`SMTP_SSL`) on the port you configure. Gmail accepts this on port 465.

### Multiple recipients

```json
"to": [
  "you@gmail.com",
  "family@example.com"
]
```

## Night mode

Night mode combines:

- **Software enhancement** (CLAHE on the L channel)
- **Camera brightness/contrast boost**
- Attempts to lower FPS and raise gain/ISO if the camera supports it

Toggle it from the dashboard. It is applied to the live stream, recordings, and alert pictures.

**Important:** most USB webcams do not expose ISO/gain/exposure controls via V4L2, so FPS/gain adjustments may be ignored. True night vision requires an **IR-sensitive camera** and an **IR illuminator**.

## Storage and cleanup

- Recordings are saved in `recordings/`.
- Recordings older than `max_age_days` are deleted during periodic cleanup, **not** on startup (unless `delete_old_on_startup` is `true`).
- If free disk space drops below `emergency_free_space_gb`, the oldest `emergency_delete_count` recordings are deleted before starting a new clip.
- Recordings older than `max_age_days` or exceeding `max_size_gb` are removed during periodic cleanup.

## Web interface

- Live stream
- Status panel (resolution, FPS, recording state, motion state)
- Settings toggles: night mode, email notifications, Telegram uploads, built-in basic auth
- Recordings list with per-row checkboxes and bulk actions:
  - **Select all**
  - **Send to Telegram**
  - **Download selected** (ZIP)
  - **Delete selected**

## Production deployment

Do **not** expose Flask's development server to the internet. Use **Gunicorn** + **nginx**.

### 1. Install Gunicorn

It is already defined in `pyproject.toml`:

```bash
cd /home/marco/CheapSecurity
source venv/bin/activate
pip install -e .
```

### 2. Run with systemd + Gunicorn

Copy the service template and enable it (replace `marco` with your username):

```bash
sudo cp cheapsecurity.service /etc/systemd/system/cheapsecurity@.service
sudo systemctl daemon-reload
sudo systemctl enable --now cheapsecurity@marco.service
```

This binds Gunicorn to `0.0.0.0:5000` with **one worker and four threads**, so the dashboard and stream are reachable directly on your network. Only one worker is used because the camera must be opened by a single process.

> **Security:** if you expose this to the internet, put nginx in front with HTTPS and authentication. If you only access it locally, keep the built-in auth enabled or use nginx basic auth.

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
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_buffering off;
    proxy_cache off;
}
```

### 4. HTTPS with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d cctv.example.com
```

### 5. Authentication in nginx

Disable the built-in Flask auth (`web.auth.enabled: false`) and let nginx handle it:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

Then uncomment the `auth_basic` lines in the nginx config and reload.

## Project structure

```
CheapSecurity/
├── src/
│   └── cheapsecurity/     # Python package
│       ├── app.py         # Development launcher
│       ├── cctv.py        # Motion detection, recording, alerts, Telegram bot
│       ├── web.py         # Flask dashboard and APIs
│       ├── wsgi.py        # Production WSGI entry point
│       ├── diagnose.py    # Diagnostic/troubleshooting script
│       ├── templates/     # HTML templates
│       └── static/        # CSS/JS
├── tests/                 # Test suite
├── config.json            # Your local settings (gitignored, never commit)
├── config.json.example    # Example settings template (committed)
├── pyproject.toml         # Package metadata and dependencies
├── cheapsecurity.service  # systemd template
├── nginx.example.conf     # Example nginx reverse proxy config
├── LICENSE                # GNU AGPLv3
└── recordings/            # Saved videos
```

## Troubleshooting

If recordings stop appearing:

1. Check the service is running:
   ```bash
   sudo systemctl status cheapsecurity@marco.service
   ```
2. Check logs:
   ```bash
   sudo journalctl -u cheapsecurity@marco.service -f
   ```
3. Run the diagnostic script:
   ```bash
   source venv/bin/activate
   python -m cheapsecurity.diagnose
   ```
4. Try lowering `motion.min_area` if no motion is detected.

## License

This project is licensed under the **GNU Affero General Public License v3.0 or later** (AGPLv3). See `LICENSE`.
