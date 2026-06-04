# GardePro Web Interface — Plan & Status

## Overview

A self-hosted web app running on a Raspberry Pi that replaces the
vendor mobile app for browsing camera media, deleting files, live streaming, and
controlling camera settings. A smart backend eventually handles auto-connect,
media caching, and AI-powered animal detection.

## Architecture

```
Browser (phone/laptop on home network)
    ↕  HTTP  (Pi port 8080)
FastAPI server  (web/server.py)
    ↕  asyncio tasks
Connection Manager  (imports from ../ble_wake.py)
    ↕  BLE (hci0)     ↕  HTTP proxy (wlx* → 192.168.8.1:8080)
Camera (GardePro E6P)

Cache (web/db.py → ~/.gardepro/)
    cache.db      — SQLite: media index, sync metadata
    thumbs/       — cached thumbnail JPEGs (320×180, ~20 KB each)
    files/        — cached full files (tee-written on first view)
```

**Stack:**
- Backend: Python 3.12 + FastAPI + uvicorn (apt packages)
- Cache: stdlib `sqlite3` + local filesystem
- Frontend: Vanilla HTML/JS/CSS — no build step, works on any browser
- Streaming: asyncio TCP proxy for RTSP; ffmpeg → HLS for in-browser video
- Installed: `sudo apt-get install -y python3-fastapi python3-uvicorn ffmpeg`

**Run (manual):**
```bash
cd /home/<user>/gardepro/web
GARDEPRO_WIFI_PASSWORD=<password> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```
Then open `http://<pi-ip>:8080` from any device on the home network.

**Run (systemd):**
```bash
sudo systemctl status gardepro      # check status
journalctl -u gardepro -f           # follow logs
sudo systemctl restart gardepro     # restart after code changes
```
Credentials live in `/etc/gardepro.env` (chmod 600, not in git).

**First-time systemd setup:**
```bash
sed 's/<user>/YOUR_USERNAME/g' web/gardepro.service.example \
  | sudo tee /etc/systemd/system/gardepro.service
sudo systemctl daemon-reload && sudo systemctl enable --now gardepro
```

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `GARDEPRO_WIFI_PASSWORD` | (required) | Camera WiFi password |
| `GARDEPRO_BLE_ADDRESS` | auto-scan | BLE address to skip scanning |
| `GARDEPRO_BLE_ADAPTER` | `hci0` | Bluetooth HCI adapter |
| `GARDEPRO_WIFI_IFACE` | first `wlx*` | WiFi interface for camera hotspot |
| `GARDEPRO_RTSP_PORT` | `8554` | Local port for RTSP TCP proxy |
| `GARDEPRO_AUTO_CONNECT` | `0` | Set to `1` to enable periodic background sync |
| `GARDEPRO_SYNC_INTERVAL` | `600` | Seconds between auto-sync attempts |

---

## Phase 1 — Core Web UI ✅ Complete

### Backend (`server.py`)

| Feature | Status |
|---|---|
| BLE scan → wake pulse → hotspot detection | ✅ |
| WiFi connect via wpa_supplicant + dhcpcd on Edimax (`wlx*`) | ✅ |
| `wlan0` (home network) never disconnected | ✅ |
| DHCP on Edimax, camera at `192.168.8.1`; 40 s timeout | ✅ |
| HTTP connectivity check before enumeration (fast fail with clear error) | ✅ |
| Media enumeration: probe `/file/{id}/JPG`, type from `Content-Type` header | ✅ |
| SSE broadcast for real-time status updates; graceful shutdown on CTRL-C | ✅ |
| Session keepalive (`/cmd/standby/reset` every 60 s) | ✅ |
| `standby/now` sent on disconnect and on server shutdown (CTRL-C/SIGTERM) | ✅ |
| WiFi signal strength poll every 10 s (`iw dev … link`) | ✅ |
| RTSP TCP proxy on Pi port 8554 → camera port 554 | ✅ |
| HLS streaming: 1-second segments, stale segment cleanup, low-latency config | ✅ |
| Camera HTTP proxy: thumbnails, full files (streaming), delete | ✅ |
| Settings read (`/cmd/getSetting` + `/cmd/getParaSetting`) | ✅ |
| Format SD card (`/cmd/format/start`) | ✅ |
| Server-restart detection (resumes session if Edimax already connected) | ✅ |

**Media type detection note:** `/thumb/{id}/JPG` and `/thumb/{id}/MP4` both return
`HTTP 200 image/jpeg` for any valid ID — the thumbnail endpoint carries no type
information. Type is determined by probing `/file/{id}/JPG` with `stream=True` and
reading the `Content-Type` response header (`image/jpeg` → JPG, `video/mp4` → MP4).
The `/JPG` vs `/MP4` suffix in the file URL does not affect which file is returned;
the camera serves the same underlying media either way.

### Frontend (`static/`)

| Feature | Status |
|---|---|
| Responsive layout: 2 cols phone / 4 tablet / 6 desktop | ✅ |
| Bottom nav bar on phone, tab bar in header on desktop | ✅ |
| Touch targets ≥ 44 px; no hover-only interactions | ✅ |
| Safe-area padding for notched phones | ✅ |
| Connect panel with live step-by-step status log (SSE) | ✅ |
| Disconnect state: log clears, button shows correct label | ✅ |
| WiFi signal badge in header (Excellent/Good/Fair/Poor + dBm) | ✅ |
| Gallery: newest-first default sort with toggle (persisted in localStorage) | ✅ |
| Gallery with page size selector (12/24/48) and pagination | ✅ |
| MP4 thumbnails: orange `▶ MP4` badge + border; JPG thumbnails plain | ✅ |
| Lightbox: full JPG viewer + MP4 player (`playsinline` for iOS, no autoplay) | ✅ |
| Swipe left/right in lightbox (phone); arrow keys (desktop) | ✅ |
| Long-press → multi-select mode; select all; bulk delete | ✅ |
| Live tab: RTSP URL display + copy; in-browser HLS player | ✅ |
| HLS stops automatically when switching away from Live tab | ✅ |
| HLS.js from CDN; low-latency config (`liveSyncDurationCount: 2`) | ✅ |
| Settings tab: camera config table + Format SD button | ✅ |
| Thumbnail browser caching (`Cache-Control: public, max-age=3600`) | ✅ |

---

## Phase 2 — Settings Write ⏸ Deferred

- Display of all `getSetting` / `getParaSetting` fields — ✅ done
- **Format SD card** (`/cmd/format/start`) — ✅ done
- **Sync camera time** via `/cmd/setGmtClock` — ❌ blocked (see below)
- **Write settings** via `/cmd/setSetting` — ❌ blocked (same root cause)
- Editable settings fields in UI (photo quality, video length, PIR, etc.) — ❌ deferred

### Root cause: BLE Level 1–3 authentication required for write commands

Extensive testing confirmed that `/cmd/setGmtClock` and `/cmd/setSetting` return
error codes regardless of URL parameter format:

| Endpoint | Error | Meaning |
|---|---|---|
| `/cmd/setGmtClock?<any params>` | `code: -3` | Auth rejected — even with no params |
| `/cmd/setSetting?pir=<any value>` | `code: -2` | Auth rejected or invalid params |

The GardePro app goes through **BLE Level 1–3** (ECDH key exchange + ChaCha20
session) before WiFi connection. This handshake produces a session token the app
includes in write HTTP requests. Our Level 0 wake-only BLE connection never
establishes this token, so write commands are rejected.

Read commands (getSetting, getParaSetting, thumb, file, delete) work without auth —
only write commands are gated.

### Traffic capture attempts (all unsuccessful)

| Method | Outcome |
|---|---|
| mitmproxy + phone system proxy | GardePro app manages WiFi itself (WifiManager API), ignores system proxy |
| ARP spoof + tcpdump on Edimax | Camera hotspot only allows **one WiFi client** — Pi joining kicks phone off |
| WiFi monitor mode (Edimax) + WPA2 decrypt | Phone uses PMKID fast-reconnect; 4-way handshake never captured across 4 attempts |
| PCAPdroid (Android traffic capture) | App detects the VPN interface PCAPdroid requires and refuses to connect |

### Side discovery: camera scans for home WiFi

During monitor mode capture, the camera MAC (`0c:cf:89:7e:e9:c3`) was observed
sending 802.11 Probe Requests for `HaneyNet` — suggesting the camera has a
dual-mode capability (AP hotspot + STA client to home WiFi). If confirmed, this
could enable auto-sync without BLE wake if the camera joins home network directly.

### Path to unblock

To implement write commands, one of:
1. **Implement BLE Level 1–3 auth** — ECDH + ChaCha20 using key material in the
   APK (`secret_g5a6r7p8r9o.dart`). Significant reverse-engineering work.
2. **Capture traffic from a rooted Android** — `adb shell tcpdump` on a rooted
   device would bypass all WiFi/VPN capture limitations.
3. **Accept write limitation** — Format SD (already working) covers the most
   destructive operation; time sync and settings write remain manual via the app.

---

## Phase 3 — Media Cache + Auto-Connect ✅ Complete

### Cache layer (`web/db.py`)

SQLite DB at `~/.gardepro/cache.db` with two tables:

```sql
media (id, kind, thumb_cached, thumb_path, file_cached, file_path,
       analyzed, analysis_json)
meta  (key, value)   -- stores last_synced timestamp
```

Files on disk:
- `~/.gardepro/thumbs/{id}_{kind}.jpg` — thumbnail JPEGs (320×180 native)
- `~/.gardepro/files/{id}_{kind}` — full-resolution files (tee-cached on first view)

### Backend (`server.py`)

| Feature | Status |
|---|---|
| DB opens at startup; cached media loaded immediately (offline-first) | ✅ |
| Thumbnail download after each connect (`_thumb_cache_loop`) | ✅ |
| Thumbnails served from `~/.gardepro/thumbs/` first; camera proxy as fallback | ✅ |
| Full files tee-cached to `~/.gardepro/files/` on first view | ✅ |
| Full files served from local cache (works offline); 404 if neither cached nor connected | ✅ |
| `_enumerate_media` upserts all items to DB and writes `last_synced` timestamp | ✅ |
| On disconnect: `_media` repopulated from DB (gallery persists offline) | ✅ |
| Delete removes DB row + both thumbnail and full file from disk | ✅ |
| `GARDEPRO_AUTO_CONNECT=1` enables periodic background sync loop | ✅ |
| Auto-sync: if disconnected → connect → enumerate → cache → disconnect | ✅ |
| Auto-sync: if already connected → re-enumerate + cache only (no disconnect) | ✅ |
| `GARDEPRO_SYNC_INTERVAL` configures sync interval (default 600 s) | ✅ |
| Systemd unit template (`web/gardepro.service.example`); fill `<user>` and install to `/etc/systemd/system/` | ✅ |

### Frontend

| Feature | Status |
|---|---|
| Gallery and nav visible when disconnected if cache has media | ✅ |
| Offline bar (sticky, orange) shows "Last synced: Xm ago" + Connect button | ✅ |
| Offline bar Connect button shows state: disabled + "Connecting…" during auto-sync | ✅ |
| Cache-progress bar counts thumbnail downloads in gallery panel | ✅ |
| Lightbox always requests `/api/file/`; falls back to thumbnail on 404 | ✅ |
| Lightbox shows full-res from local cache when offline (previously viewed files) | ✅ |
| Lightbox offline fallback: cached thumbnail + "connect for full resolution" note | ✅ |
| MP4 offline: thumbnail + "Connect to play video" note | ✅ |
| Delete button hidden in lightbox when disconnected | ✅ |
| Settings tab shows "Connect to the camera to view settings." when offline | ✅ |
| Gallery grid: 2 cols mobile / 4 tablet / 6 desktop (max 6 on any width) | ✅ |

### Thumbnail details

Camera-native thumbnail size: **320×180 px** (16:9), ~20 KB JPG, ~11 KB for MP4 frames.
Gallery cards use a 4:3 aspect ratio with `object-fit: cover` (slight top/bottom crop).
At 6 columns on a 1440 px screen, thumbnails render at 240 px — below native, so no
upscaling. At 1920 px, each column is 320 px — exactly native resolution.

---

## Phase 4 — LLM Image Analysis + Alerting ✅ Complete

### Analysis (`web/analyzer.py`)

| Feature | Status |
|---|---|
| LLM backend: local llama.cpp (OpenAI-compatible API) | ✅ |
| LLM backend: Anthropic Claude | ✅ |
| Base64-encode thumbnail → send to LLM with configurable prompt | ✅ |
| Background analysis queue — runs after thumbnail caching per connect/sync | ✅ |
| Subject keyword extraction from LLM response | ✅ |
| Store `analyzed` + `analysis_json` in `cache.db` (both `media` and `saved_media` tables) | ✅ |
| SSE push `analysis_update` / `saved_analysis_update` to browser on completion | ✅ |
| Re-analyze button in lightbox (force re-run on demand) | ✅ |
| Analysis config persisted in `~/.gardepro/analysis_config.json` | ✅ |

### Analysis UI

| Feature | Status |
|---|---|
| Subject badge on thumbnail cards (raccoon 🦝, cat 🐱, person 🚶, etc.) | ✅ |
| Colored thumbnail border by category (red=wild, blue=person, green=pet, orange=other) | ✅ |
| Analysis description text shown in lightbox | ✅ |
| Analysis Settings section in Settings tab (backend, URL, model, prompt, tokens, temp) | ✅ |

### Alerting (`web/alerter.py`)

| Feature | Status |
|---|---|
| Alert rules loaded from `~/.gardepro/alerts.yaml` at startup | ✅ |
| Keyword matching against subjects + description | ✅ |
| `action: log` — writes to server log | ✅ |
| `action: email` — Gmail SMTP (requires env vars in `/etc/gardepro.env`) | ✅ |
| Per-image dedup — never re-alert on the same photo | ✅ |
| Per-rule cooldown — suppress repeat alerts within configurable window (default 30 min) | ✅ |
| Per-rule enable/disable from Alert Settings UI | ✅ |
| Alert Settings section in Settings tab (send alerts toggle, email status, cooldown, per-rule toggles) | ✅ |

### Email setup

To enable email alerts:
1. Set `action: email` on the desired rule in `~/.gardepro/alerts.yaml`
2. Add to `/etc/gardepro.env`:
   ```
   GARDEPRO_ALERT_EMAIL=you@gmail.com
   GARDEPRO_ALERT_SMTP_PASSWORD=<gmail-app-password>
   ```
   Gmail App Password: myaccount.google.com → Security → App passwords
3. `sudo systemctl restart gardepro`

Email format — Subject: `GardePro alert: raccoon detected`; Body: plain text with
detection name, image URL (`http://pi:8080/api/file/{id}/{kind}`), and analysis description.

### Deferred to Phase 5

- **HTTP webhook action** — `action: webhook` in alerts.yaml not yet implemented
- **Cat tracking / time-of-day analytics** — no aggregation queries or UI panel for sighting patterns
- **Alert rule editing from UI** — keywords and action type still require editing alerts.yaml directly

---

## Phase 5 — Analytics + Webhook Alerts (planned)

### Webhook alert action

- Add `action: webhook` support in `web/alerter.py`
- Rule config: `webhook_url: https://...`
- POST JSON payload: `{rule, subjects, description, image_url, timestamp}`
- Enables integration with Home Assistant, ntfy, Pushover, etc.

### Animal sighting analytics

- DB query: aggregate detections by subject + hour-of-day / day-of-week
- New UI panel (Settings tab or dedicated Analytics tab):
  - Sighting frequency per animal type (bar chart or table)
  - Time-of-day histogram (e.g., raccoon mostly 11pm–2am)
  - Most recent sighting per animal type
- Cat tracking: running log of all cat detections with timestamps

### Alert rule management from UI

- Read/write `~/.gardepro/alerts.yaml` from the web UI
- Add/remove/edit rules: name, keywords list, action, webhook URL
- No YAML editing required for basic rule management

---

## Known Unknowns

| Item | Notes |
|---|---|
| `setGmtClock` params | Unknown format — needs app HTTP capture |
| `setSetting` params | Unknown format — needs app HTTP capture |
| LLM vision support (local) | ✅ confirmed — llama.cpp with `qwen36-35b-a3b` + mmproj file |
| Camera media count limit | Max observed ID ~437; no server-side count API |
| Camera dual-mode STA/AP | Probe requests for home WiFi seen — unconfirmed if joinable |
