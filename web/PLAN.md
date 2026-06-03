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
```

**Stack:**
- Backend: Python 3.12 + FastAPI + uvicorn (apt packages)
- Frontend: Vanilla HTML/JS/CSS — no build step, works on any browser
- Streaming: asyncio TCP proxy for RTSP; ffmpeg → HLS for in-browser video
- Installed: `sudo apt-get install -y python3-fastapi python3-uvicorn ffmpeg`

**Run:**
```bash
cd /home/<user>/gardepro/web
GARDEPRO_WIFI_PASSWORD=<password> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```
Then open `http://<pi-ip>:8080` from any device on the home network.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `GARDEPRO_WIFI_PASSWORD` | (required) | Camera WiFi password |
| `GARDEPRO_BLE_ADDRESS` | auto-scan | BLE address to skip scanning |
| `GARDEPRO_BLE_ADAPTER` | `hci0` | Bluetooth HCI adapter |
| `GARDEPRO_WIFI_IFACE` | first `wlx*` | WiFi interface for camera hotspot |
| `GARDEPRO_RTSP_PORT` | `8554` | Local port for RTSP TCP proxy |

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

## Phase 2 — Settings Write (planned)

- Display of all `getSetting` / `getParaSetting` fields — ✅ done
- **Format SD card** — ✅ done  
- **Sync camera time** via `/cmd/setGmtClock` — ⏳ blocked pending parameter format
  - Need to capture one real HTTP request from the GardePro app (Wireshark or
    mitmproxy) to confirm the parameter format before implementing
- **Write settings** via `/cmd/setSetting` — ⏳ same blocker
- Editable settings fields in UI (photo quality, video length, PIR, etc.)

---

## Phase 3 — Media Cache + Auto-Connect (planned)

- SQLite database (`~/.gardepro/cache.db`): id, kind, thumb_cached, file_path,
  analyzed, analysis_json
- Download and store thumbnails locally after enumeration
- Serve cached thumbnails without camera connection
- On connect: fetch only new IDs not already in cache
- Auto-connect mode (`--auto-connect` flag or systemd timer)
- Web UI shows "Last synced: 2h ago" when operating from cache
- Systemd service for automatic startup at boot:

```ini
[Unit]
Description=GardePro Camera Server
After=network-online.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/gardepro/web
Environment=GARDEPRO_WIFI_PASSWORD=<password>
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Phase 4 — LLM Image Analysis + Alerting (planned)

- **devbox** at `<devbox-ip>` — llama.cpp server with `qwen36-35b-a3b`
- llama.cpp OpenAI-compatible API: `http://<devbox-ip>:<port>/v1/chat/completions`
- **Prerequisite:** confirm whether model supports vision (image inputs).
  Qwen2.5-VL supports it; Qwen2.5-Instruct does not.
  Check: `curl http://<devbox-ip>:<port>/v1/models`
- Background analysis queue: after caching a new image, base64-encode and send
  to llama.cpp with a prompt describing animal/person detection
- Store result JSON in `cache.db`; show analysis text on thumbnail hover
- Alert rules in `~/.gardepro/alerts.yaml`:
  - Match keywords in analysis text (raccoon, cat, unknown animal, person, etc.)
  - Actions: log only, email, or HTTP webhook
- Web UI: colored border on thumbnails that triggered an alert
- Cat tracking: log entries, time-of-day patterns
- Raccoon/unexpected animal alerts via email/webhook

---

## Known Unknowns

| Item | Notes |
|---|---|
| `setGmtClock` params | Unknown format — needs app HTTP capture |
| `setSetting` params | Unknown format — needs app HTTP capture |
| LLM vision support | Confirm `qwen36-35b-a3b` accepts image inputs |
| llama.cpp server port | Configurable — check devbox |
| Camera media count limit | Max observed ID ~276; no server-side count API |
