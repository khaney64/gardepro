# GardePro E6P Programmatic Access Notes

This repository contains scripts and notes from an investigation into programmatic
access for a GardePro E6P trail camera. The main focus is waking the camera over
Bluetooth LE, connecting to the temporary WiFi hotspot, and probing the camera's
local HTTP and RTSP services.

The raw Android bugreports, Bluetooth snoop logs, APK/DEX artifacts, downloaded
media, and generated dumps are intentionally excluded from git. Those files are
large and may contain device identifiers, local network details, or other private
state that is not appropriate for a public repository.

Both scripts work on Windows and Linux (including Raspberry Pi). See the Scripts section
in `gardepro-e6p-investigation.md` for platform-specific setup and usage.

## Files

- `ble_scan.py` scans nearby BLE devices and prints names, addresses, and RSSI.
- `ble_wake.py` wakes the camera over BLE, optionally joins its WiFi hotspot, and
  probes local HTTP/RTSP endpoints.
- `ble_wake-original.py` preserves the earlier working wake/connect script for
  comparison.
- `parse_btsnoop.py` parses Android btsnoop HCI logs and extracts BLE ATT writes
  and notifications.
- `gardepro-e6p-investigation.md` contains the detailed investigation notes,
  confirmed endpoints, protocol observations, and command examples.

For implementation details, endpoint findings, and current open questions, start
with `gardepro-e6p-investigation.md`.

## Web Interface

A self-hosted web app (`web/`) runs on the Raspberry Pi and replaces the vendor
mobile app for browsing media, deleting files, live streaming, and reading camera
settings. A local SQLite cache keeps the gallery available offline between syncs.

**Run (systemd):**
```bash
sudo systemctl status gardepro      # check status
journalctl -u gardepro -f           # follow logs
sudo systemctl restart gardepro     # pick up code changes
```
Credentials are in `/etc/gardepro.env`. Open `http://<pi-ip>:8080` from any device on the home network.

**Run (manual):**
```bash
cd web
GARDEPRO_WIFI_PASSWORD=<password> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```

**Features:**
- Gallery with pagination, sort, multi-select delete, full-resolution lightbox
- Live RTSP proxy + HLS in-browser streaming (requires ffmpeg)
- Camera settings read; SD card format
- Local tab — save individual photos/videos to Pi-local storage (`~/.gardepro/saved/`); files are named `{timestamp}_{cam_id}_{kind}` so they survive camera SD resets; viewable and deletable offline
- Connection progress modal — shows each BLE/WiFi step in real-time, stays open on failure with error detail and Cancel/Close controls
- Sync Now button — on-demand sync from the header; connects, syncs, and auto-disconnects if offline
- Logs tab — terminal-style view of recent server logs (last 200 entries), updated live via SSE; useful for diagnosing auto-sync failures
- Last Event indicator — header shows how long ago the newest media item was discovered; updates every 60 seconds
- Offline gallery — cached thumbnails/media remain browsable when disconnected; "Last synced" warns (⚠ orange) if stale beyond 10 minutes
- Auto-sync — background BLE wake + WiFi connect + thumbnail cache every 10 minutes (`GARDEPRO_AUTO_CONNECT=1`)
- LLM image analysis — thumbnails sent to local llama.cpp or Anthropic Claude for animal/person detection; results shown as subject badges and colored borders on thumbnails, description in lightbox
- Alert rules — keyword-based rules in `~/.gardepro/alerts.yaml`; actions: log or email (configurable SMTP — Gmail, Postmark, etc.); per-rule enable/disable and cooldown in Settings UI; "Send test" button to verify email config; failures surfaced in Logs tab

**Files:**
- `web/server.py` — FastAPI backend: BLE wake, WiFi connect, media proxy, HLS streaming, auto-sync, in-memory log buffer, saved-media endpoints
- `web/db.py` — SQLite cache layer: media index, thumbnail/file tracking, saved_media table
- `web/static/` — Vanilla HTML/JS/CSS frontend (no build step)
- `web/PLAN.md` — Full architecture, phase status, and open questions

**Dependencies:** `sudo apt-get install -y python3-fastapi python3-uvicorn ffmpeg`

**Local cache** (on Pi): `~/.gardepro/cache.db`, `~/.gardepro/thumbs/`, `~/.gardepro/files/`, `~/.gardepro/saved/`

**Systemd notes:**
- `TimeoutStopSec=10` is set in the unit to prevent hanging stops (SSE connections delay graceful uvicorn shutdown)
- Force-kill if stuck: `sudo systemctl kill -s KILL gardepro`
- On restart, if the WiFi interface still has the camera IP, the server probes the camera first; if it's unreachable (went to sleep) it restores the cached gallery immediately rather than timing out

**Phase status:**
- Phase 1 (core gallery, live view, settings read) — ✅ complete
- Phase 2 (settings write, time sync) — ⏸ blocked on BLE Level 1–3 auth
- Phase 3 (SQLite cache, offline gallery, auto-sync, systemd) — ✅ complete
- Phase 3.5 (connection dialog, logs tab, stale-sync warning, last-event, sync-now, local save tab) — ✅ complete
- Phase 4 (LLM image analysis, alert rules, email alerts, per-rule UI controls) — ✅ complete
- Phase 5 (webhook alerts, animal sighting analytics, alert rule UI editing) — 📋 planned

See `web/PLAN.md` for full details.
