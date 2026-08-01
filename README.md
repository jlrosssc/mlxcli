# Geofence Alert Server

A Python-based push notification server that broadcasts zone-based weather and 
emergency alerts to iOS devices via Apple Push Notification service (APNs).
Includes a Mac admin GUI for composing and sending alerts, and a Cloudflare 
tunnel for remote device connectivity.

---

## Overview

```
Mac Admin GUI (client_gui.py)
        |
        | HTTP POST /send-briefing-alert
        |
CentOS/Linux Server (server_backend.py)
        |                    |
        | APNs HTTP/2        | Cloudflare Tunnel
        |                    |
Apple APNs              iOS Devices (geofencetracking app)
```

When an alert is sent:
1. Admin selects a zone (SE-1 through MW-4), writes a banner and impact statement,
   and optionally attaches a PDF
2. Server builds an HTML briefing page and writes it to the web root
3. Server sends an APNs push to all registered devices
4. Device displays a banner notification; tapping opens the full briefing in-app

---

## Prerequisites

### Apple Developer Account Requirements

You must have an active Apple Developer Program membership ($99/year).
The following are required and specific to your account:

| Item | Where to find it | Example |
|---|---|---|
| Team ID | developer.apple.com → Account → Membership | `T3WCS856JZ` |
| Key ID | developer.apple.com → Certificates, IDs & Profiles → Keys | `PZ2286475V` |
| .p8 private key file | Downloaded when creating the key (one time only) | `AuthKey_PZ2286475V.p8` |
| Bundle ID | Your iOS app's bundle identifier | `com.yourname.appname` |

**To create an APNs key:**
1. Go to developer.apple.com → Certificates, Identifiers & Profiles → Keys
2. Click + to create a new key
3. Enable Apple Push Notifications service (APNs)
4. Download the .p8 file — you can only download it once
5. Note the Key ID shown on the key detail page

**APNs endpoint:**
- Development (Xcode / direct device): `https://api.development.push.apple.com`
- Production (TestFlight / App Store): `https://api.push.apple.com`

Change `APNS_HOST` in `server_backend.py` to match your build type.

---

## Server Setup

### 1. System Requirements

- Linux (tested on CentOS 7 / RHEL 7)
- Python 3.7+ recommended (works on 3.6 with limitations)
- Apache web server (for serving briefing HTML pages)
- curl with HTTP/2 support (see step 4)
- cloudflared (see step 5)

### 2. Install Python dependencies

```bash
pip3 install fastapi uvicorn pyjwt cryptography
# or for Python 3.6 specifically:
/usr/bin/python3.6 -m pip install fastapi uvicorn pyjwt cryptography --user
```

### 3. Place your APNs key

Create a directory for your credentials and copy your .p8 file there:

```bash
mkdir -p ~/AlertSystem
cp /path/to/AuthKey_YOURKEYID.p8 ~/AlertSystem/
```

### 4. Install curl with HTTP/2 support

CentOS 7's default curl does not support HTTP/2. Install a static binary:

```bash
cd /tmp
curl -L https://github.com/stunnel/static-curl/releases/download/8.7.1/curl-linux-x86_64-8.7.1.tar.xz \
     -o curl-h2.tar.xz
tar xf curl-h2.tar.xz
./curl --version | grep HTTP2   # should show HTTP2 in Features
sudo cp /tmp/curl /usr/local/bin/curl-h2
sudo chmod 755 /usr/local/bin/curl-h2
```

Verify it can reach Apple:
```bash
curl-h2 --http2 --cacert /etc/pki/tls/certs/ca-bundle.crt \
    -s -o /dev/null -w "%{http_code}" \
    https://api.development.push.apple.com
# Should return 405 — this is correct (GET not allowed, but TLS works)
```

**Note on CA certificates:**
The static curl binary looks for CA certs in `/etc/ssl/certs/ca-certificates.crt`
which does not exist on CentOS 7. The correct path is:
`/etc/pki/tls/certs/ca-bundle.crt`

This is already set as `CURL_CACERT` in `server_backend.py`.
On Ubuntu/Debian the path is `/etc/ssl/certs/ca-certificates.crt` — update
`CURL_CACERT` accordingly.

### 5. Install cloudflared

```bash
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.rpm
sudo rpm -ivh cloudflared-linux-amd64.rpm
cloudflared --version
```

cloudflared creates a temporary public HTTPS tunnel to your server so iOS
devices can reach it from anywhere. The tunnel URL changes each restart —
the admin GUI fetches it automatically via `/config`.

### 6. Set up Apache briefings directory

The server writes HTML briefing pages to your web root:

```bash
sudo mkdir -p /var/www/html/briefings
sudo chown yourusername:yourusername /var/www/html/briefings
sudo chmod 755 /var/www/html/briefings
# CentOS/RHEL only — fix SELinux context:
sudo chcon -t httpd_sys_content_t /var/www/html/briefings
```

### 7. Configure server_backend.py

Open `server_backend.py` and update the configuration block at the top:

```python
# ── APNs configuration ────────────────────────────────────────────────────────
APNS_KEY_ID    = "YOUR_KEY_ID"           # 10-char string from developer.apple.com
APNS_TEAM_ID   = "YOUR_TEAM_ID"         # 10-char string from developer.apple.com
APNS_BUNDLE_ID = "com.yourname.appname" # must match your iOS app exactly
APNS_P8_PATH   = "/home/youruser/AlertSystem/AuthKey_YOUR_KEY_ID.p8"
CURL_H2        = "/usr/local/bin/curl-h2"
CURL_CACERT    = "/etc/pki/tls/certs/ca-bundle.crt"  # adjust for your distro

# Development or production APNs endpoint
APNS_HOST      = "https://api.development.push.apple.com"
# Use this for TestFlight / App Store builds:
# APNS_HOST    = "https://api.push.apple.com"

# ── Briefing configuration ────────────────────────────────────────────────────
BRIEFING_DIR      = "/var/www/html/briefings"          # local path Apache serves
BRIEFING_BASE_URL = "https://yourdomain.com/briefings" # public URL
```

### 8. Install as a systemd service (auto-start on boot)

```bash
sudo bash << 'INSTALL'
SERVICE_FILE="/etc/systemd/system/geofence-server.service"
LOG_FILE="/var/log/geofence-server.log"
RUN_USER="youruser"
PYTHON="/usr/bin/python3.6"   # or python3 / python3.9 etc.
SCRIPT="/home/$RUN_USER/server_backend.py"

touch "$LOG_FILE"
chown "$RUN_USER":"$RUN_USER" "$LOG_FILE"

cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=Geofence Alert Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=/home/$RUN_USER
ExecStart=$PYTHON $SCRIPT
Restart=always
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable geofence-server
systemctl start geofence-server
systemctl status geofence-server
INSTALL
```

### 9. Verify the server is running

```bash
# Check service status
sudo systemctl status geofence-server

# Watch live log
tail -f /var/log/geofence-server.log

# Check health endpoint
curl http://localhost:8000/health

# Verify Apache is serving briefings
curl -s -o /dev/null -w "%{http_code}" https://yourdomain.com/briefings/
# Returns 200 or 403 — both mean Apache is handling the path
```

---

## Admin GUI Setup (Mac)

### Requirements

- macOS with Python 3.7+
- PyQt5, requests, qrcode, pillow

```bash
pip3 install PyQt5 requests qrcode pillow
```

### Configuration

Open `client_gui.py` and update the server address:

```python
MANAGEMENT_URL = "http://YOUR_SERVER_IP:8000"
```

Use the local IP when on the same network as the server.
The GUI fetches the Cloudflare tunnel URL automatically — devices
connect via the tunnel, not the local IP.

### Running

```bash
python3 client_gui.py
```

---

## iOS App Requirements

The companion iOS app (`geofencetracking`) must be built in Xcode with:

- Push Notifications capability enabled
- Background Modes → Remote notifications enabled
- The same Bundle ID as configured in `APNS_BUNDLE_ID` above
- Deployment target iOS 16+

The app registers with the server on launch and polls `/get-commands/{token}`
as a fallback if APNs push delivery fails.

---

## Zone Reference

| Zone ID | Name | Region |
|---|---|---|
| SE-1 | Charlotte Area | NC |
| SE-2 | Winston-Salem | NC |
| SE-3 | Shelby Area | NC |
| SE-4 | N. Wilkesboro | NC |
| SE-5 | Macon Area | GA |
| SE-6 | Rome Area | GA |
| MW-1 | Midwest Zone 1 | — |
| MW-2 | Midwest Zone 2 | — |
| MW-3 | Midwest Zone 3 | — |
| MW-4 | Midwest Zone 4 | — |

---

## File Reference

| File | Purpose |
|---|---|
| `server_backend.py` | FastAPI server — APNs push, briefing page generation, device registry |
| `client_gui.py` | PyQt5 Mac admin GUI — compose and send alerts |
| `geofencetracking` (Xcode) | iOS app — receives pushes, displays briefings |
| `AuthKey_KEYID.p8` | APNs private key — keep secure, never commit to git |
| `/var/www/html/briefings/` | Generated briefing HTML pages — one per zone, overwritten each send |
| `/var/log/geofence-server.log` | Server log |
| `~/.geofence_config.json` | (future) per-user config — not yet implemented |

---

## Security Notes

- **Never commit your .p8 file to git.** It cannot be re-downloaded and
  compromising it allows anyone to send push notifications to your users.
- The server runs on port 8000 without authentication — restrict access
  with a firewall so only the admin Mac can reach it directly.
- Cloudflare tunnel traffic is encrypted in transit.
- Briefing HTML pages on your web server are publicly accessible by URL —
  do not include sensitive information you would not want publicly visible.

---

## Troubleshooting

**APNs returns 403:**
Key ID, Team ID, or Bundle ID mismatch. Double-check all three against
developer.apple.com.

**APNs returns 400:**
Malformed payload or invalid device token. Check that the phone has
re-registered after the latest app build.

**APNs returns 410:**
Device token is no longer active — the app was uninstalled or the token
expired. Have the user reinstall and re-register.

**APNs returns 000 (no response):**
curl cannot reach Apple's servers. Check firewall, outbound port 443,
and CA certificate path.

**Push delivered but no notification on phone:**
- Confirm notification permission is granted in iOS Settings
- Confirm the app's Bundle ID matches APNS_BUNDLE_ID exactly
- Check that the device token in `/devices` is a real APNs token
  (64-char hex assigned by Apple) not a locally-generated hash

**Briefing page returns 403:**
SELinux is blocking Apache. Run:
```bash
sudo chcon -t httpd_sys_content_t /var/www/html/briefings/
sudo chcon -R -t httpd_sys_content_t /var/www/html/briefings/
```
