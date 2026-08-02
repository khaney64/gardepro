# GardePro Web Interface — Plan & Status

## Overview

A self-hosted web app running on a Raspberry Pi that replaces the
vendor mobile app for browsing camera media, deleting files, live streaming, and
controlling camera settings. The backend handles auto-connect, media caching,
AI-powered animal/person detection, and keyword-based alerting; see Phase status
below for what's shipped vs. still planned.

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
    cache.db      — SQLite: media index, saved copies, battery + sync metadata
    thumbs/       — cached thumbnail JPEGs (320×180, ~20 KB each)
    files/        — cached full files (tee-written on first view)
    saved/        — Local-tab copies saved permanently from the Pi
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
| `GARDEPRO_SYNC_INTERVAL` | `900` | Seconds between auto-sync attempts |
| `GARDEPRO_LLM_URL` | `http://devbox.lan:8080` | Base URL of local llama.cpp OpenAI-compatible API |
| `GARDEPRO_LLM_MODEL` | — | Model name for local vision analysis |
| `ANTHROPIC_API_KEY` | — | Required when analysis backend is set to `anthropic` |
| `GARDEPRO_ALERT_EMAIL` | — | Recipient address for email alerts |
| `GARDEPRO_ALERT_FROM_EMAIL` | `GARDEPRO_ALERT_EMAIL` | Sender address (if different from recipient) |
| `GARDEPRO_ALERT_SMTP_HOST` | `smtp.gmail.com` | SMTP server hostname |
| `GARDEPRO_ALERT_SMTP_PORT` | `587` | SMTP server port |
| `GARDEPRO_ALERT_SMTP_SSL` | auto (465=SSL) | Set to `1` to force implicit SSL; default uses STARTTLS |
| `GARDEPRO_ALERT_SMTP_USER` | `GARDEPRO_ALERT_FROM_EMAIL` | SMTP auth username (use API token for Postmark) |
| `GARDEPRO_ALERT_SMTP_PASSWORD` | — | SMTP password or app password |

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
| Responsive gallery grid: 2 cols phone portrait / 3 phone landscape / 4 tablet / 5 desktop | ✅ (superseded by Phase 5; see below) |
| Bottom nav bar on phone, tab bar in header on desktop | ✅ |
| Touch targets ≥ 44 px; no hover-only interactions | ✅ |
| Safe-area padding for notched phones | ✅ |
| ~~Full-screen connect panel gate~~ — removed in Phase 5; see below | ⚠ superseded |
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
| Thumbnail browser caching (`Cache-Control: no-cache` + ETag for revalidation) | ✅ |

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

During monitor mode capture, the camera's own MAC address was observed sending
802.11 Probe Requests for the tester's home WiFi SSID — suggesting the camera has
a dual-mode capability (AP hotspot + STA client to home WiFi). If confirmed, this
could enable auto-sync without BLE wake if the camera joins the home network directly.

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
| Scan floor: scan continues past `MAX_MISSES` gap until beyond highest previously-known ID (handles on-camera deletion gaps > 10) | ✅ |
| Reused ID detection: `upsert_media` resets all cache flags when `captured_at` differs (prevents stale thumb after on-camera delete + reshoot) | ✅ |
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
| Gallery grid: 2 cols phone portrait / 3 phone landscape / 4 tablet / 5 desktop (see Phase 5 — reduced from 6 to 5 on desktop) | ✅ |

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
| Subject extraction: structured JSON detections (`{"detections":[{label,name,confidence}]}`) with keyword-scan fallback for free-text responses (see Phase 5) | ✅ |
| Retry on empty response or all-low-confidence (≤3) detections, doubling thinking budget | ✅ (see Phase 5) |
| Store `analyzed` + `analysis_json` in `cache.db` (both `media` and `saved_media` tables) | ✅ |
| SSE push `analysis_update` / `saved_analysis_update` to browser on completion | ✅ |
| Re-analyze button in lightbox (🔬, force re-run on demand) | ✅ |
| Analysis config persisted in `~/.gardepro/analysis_config.json` | ✅ |
| Thinking budget — extended reasoning token budget sent per request; 0 = off | ✅ |
| — Local LLM: `chat_template_kwargs: {enable_thinking, thinking_budget}` appended to payload | ✅ |
| — Anthropic: `thinking: {type: enabled, budget_tokens}` block; response text extracted past thinking block | ✅ |
| — `max_tokens` sent = `thinking_budget + max_tokens` (answer headroom) | ✅ |
| One-shot image chat (`chat_image`) — custom prompt, raw text response, no subject parsing | ✅ |
| Chat API endpoints: `POST /api/analysis/chat/{id}/{kind}` and `.../chat/saved/{id}` | ✅ |

### Analysis UI

| Feature | Status |
|---|---|
| Subject badge on thumbnail cards (raccoon 🦝, cat 🐱, person 🚶, etc.) | ✅ |
| Colored thumbnail border by category (red=wild, blue=person, green=pet, orange=other) | ✅ |
| Analysis description text shown in lightbox | ✅ |
| Analysis Settings: backend, URL, model, prompt, max tokens, thinking budget, temperature | ✅ |
| Chat toggle in Analysis Settings — enables 💬 button in lightbox | ✅ |
| Chat dialog: prompt textarea pre-filled from settings; editable; Submit → response shown inline | ✅ |
| Chat is one-shot per submit; no conversation history; response not stored | ✅ |
| Multi-select / delete blocked when not connected (long-press guard + auto-exit on disconnect) | ✅ |

### Alerting (`web/alerter.py`)

| Feature | Status |
|---|---|
| Alert rules loaded from `~/.gardepro/alerts.yaml` at startup | ✅ |
| Keyword matching against subjects + description | ✅ |
| `action: log` — writes to server log | ✅ |
| `action: email` — configurable SMTP (requires env vars in `/etc/gardepro.env`) | ✅ |
| Email failures surfaced in UI log (no longer silently dropped) | ✅ |
| "Send test" button in Alert Settings (visible when email configured) | ✅ |
| Per-image dedup — never re-alert on the same photo | ✅ |
| Per-rule cooldown — suppress repeat alerts within configurable window (default 30 min) | ✅ |
| Per-rule enable/disable from Alert Settings UI | ✅ |
| Alert rule order in UI follows `alerts.yaml` order: Person → Cat → Raccoon → Wildlife → Other | ✅ |
| Alert Settings section in Settings tab (send alerts toggle, email status, cooldown, per-rule toggles) | ✅ |

### Email setup

To enable email alerts:
1. Set `action: email` on the desired rule in `~/.gardepro/alerts.yaml`
2. Add to `/etc/gardepro.env`:
   ```
   GARDEPRO_ALERT_EMAIL=you@example.com
   GARDEPRO_ALERT_SMTP_HOST=smtp.postmarkapp.com
   GARDEPRO_ALERT_SMTP_PORT=587
   GARDEPRO_ALERT_SMTP_USER=<server-api-token>
   GARDEPRO_ALERT_SMTP_PASSWORD=<server-api-token>
   ```
   For Gmail: omit `SMTP_HOST`/`SMTP_PORT` (defaults apply), set `SMTP_USER` to the sending address and `SMTP_PASSWORD` to an App Password (myaccount.google.com → Security → App passwords).
3. Use the **Send test** button in Settings → Alert Settings to verify before relying on live alerts.
4. `sudo systemctl restart gardepro`

Email format — Subject: `GardePro alert: raccoon detected`; Body: plain text with
detection name, image URL (`http://pi:8080/api/file/{id}/{kind}`), and analysis description.

### Deferred (see Phase 6)

- **HTTP webhook action** — `action: webhook` in alerts.yaml not yet implemented
- **Cat tracking / time-of-day analytics** — no aggregation queries or UI panel for sighting patterns
- **Alert rule editing from UI** — keywords and action type still require editing alerts.yaml directly

---

## Phase 5 — Battery Monitoring, Privacy Filter & Operational Hardening ✅ Complete

Phase 5 turned out to be a round of reliability, privacy, and UX hardening rather
than the analytics/webhook work originally sketched for it — that work is still
wanted and now lives in Phase 6, below.

### Battery monitoring (`server.py`)

| Feature | Status |
|---|---|
| `_battery_poll_loop()` polls `GET /cmd/info/2` every 60 s while connected; broadcasts `type: "battery"` over SSE | ✅ |
| Last-known battery reading persisted to DB (`meta` table via `set_battery`/`get_battery`) — survives disconnect/restart, shown in header badge while offline | ✅ |
| Auto-sync paused at/below `BATTERY_AUTOSYNC_DISABLE_PCT` (default 10%) to preserve power; manual connect still allowed | ✅ |
| Low-battery alerts: fire at the configurable `battery_warning_threshold` (default 25%) and every 5% below; reset once the level recovers above threshold | ✅ |
| BLE/WiFi ops all have hard `asyncio.wait_for` timeouts (connect 20 s, op 10 s, disconnect 5 s) to prevent BlueZ from wedging the connect flow | ✅ |

### Media sync & cache reliability

| Feature | Status |
|---|---|
| Media enumeration refactored onto the native `/list/detail/backward/{ts}/{count}` listing endpoint (was sequential per-ID probing); old probe kept as fallback on schema mismatch | ✅ |
| After each successful scan, DB rows no longer present on camera are purged (handles on-camera delete + SD card format) | ✅ |
| A scan that returns zero results still stamps `last_synced` and preserves the cached gallery, instead of wiping it or looping reconnect attempts | ✅ |
| Gallery sorted by `captured_at` instead of `id` — fixes ordering after an SD card format resets IDs | ✅ |
| Thumbnail cache permanently skips retry on a camera 4xx/5xx response (corrupt/missing file) instead of retrying every sync | ✅ |
| Auto-sync retry: up to `AUTO_SYNC_RETRIES` (2) extra attempts on connect failure, 30 s apart | ✅ |
| Analysis now runs after disconnect in the auto-sync path, releasing the camera connection sooner | ✅ |
| DHCP timeout tightened from 90 s to 30 s; `dhcpcd --noarp` skips the ARP conflict probe that was failing against the camera's isolated AP ~50% of the time | ✅ |
| BlueZ stuck-discovery guard: `bluetoothctl scan off` runs before every BLE scan to clear a lingering "Discovering: yes" state left by a crashed prior scan | ✅ |
| Stale-sync warning threshold raised from 12 to 20 minutes (reduce false "stale" flags around normal 15-min sync cadence) | ✅ |
| Offline delete: mark an image `pending_delete` while disconnected (long-press → delete); flushed to the camera + DB on the next connect, retried if the camera is unreachable mid-flush | ✅ |

### Analysis: structured detections + privacy filter (`analyzer.py`)

| Feature | Status |
|---|---|
| LLM response parsed as structured JSON (`{"detections":[{"label","name","confidence"}]}`) with per-detection confidence; falls back to free-text keyword scan if the response isn't valid JSON | ✅ |
| Chat responses can include a `reasoning` field per detection when the model returns the structured format; shown inline under each detection in the chat dialog | ✅ |
| Prompt text sent before image data in the request payload to enable LLM prompt-prefix caching | ✅ |
| **Privacy filter** — "Remove new Person only images" (Analysis Settings, nested under the Person alert rule): deletes a newly analyzed image immediately if every detected subject is person/human/legs and nothing else. Deletes on-camera + DB if connected, or queues via the offline-delete flag if not. Mutually exclusive with the Person alert rule in the UI | ✅ |
| Named-pet recognition — the keyword list can call out specific known pets by name (in addition to generic species like "cat"/"dog"); badge and keyword lists are configured locally per household, not meant to be shared verbatim in this repo | ✅ |
| Alert scope fix: disabled alert rules no longer leak their keywords into the catch-all rule's matching | ✅ |
| Log timestamps shown in US Eastern time (auto-tracks EST/EDT) | ✅ |

### Alerting

| Feature | Status |
|---|---|
| Multiple `action: email` rules triggered by the same image are consolidated into a single email instead of one per rule | ✅ |

### Frontend / UX

| Feature | Status |
|---|---|
| Full-screen "Connect Camera" gate removed — header/gallery/nav are always visible; offline bar (with dynamic text) is now the sole connect entry point when disconnected | ✅ |
| On first page load with an empty gallery, UI auto-switches to the Local tab | ✅ |
| Ctrl+Click multi-select added for desktop gallery (touch long-press remains the mobile equivalent) | ✅ |
| Desktop gallery grid reduced from 6 to 5 columns at ≥1025px (thumbnails were rendering below native 320 px res at 6-wide on common desktop widths) | ✅ |
| Cached gallery now shown during the "connecting" transition, not just once fully connected | ✅ |
| Analysis config loaded at server startup so the chat button reflects its enabled state without first visiting Settings | ✅ |
| Saving a photo/video to the Local tab carries over the camera-side analysis result immediately (no need to re-run analysis on the saved copy) | ✅ |
| Lightbox arrow-key navigation suppressed while a text input (e.g. the chat prompt) has focus | ✅ |

---

## Phase 6 — Analytics + Webhook Alerts (planned)

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
- Named-pet tracking: running log of all named-pet detections with timestamps

### Alert rule *definition* editing from UI

Note the scope here: Settings → Alert Settings already lets you enable/disable each
existing rule, set the cooldown, set the battery-warning threshold, toggle the
person-only removal filter, and send a test email (all Phase 4/5, already ✅ —
see the Alerting tables above). What's still missing is editing what a rule
*is*:

- Read/write `~/.gardepro/alerts.yaml` from the web UI
- Add/remove rules, and edit an existing rule's name, keyword list, or action
  type (log/email/webhook) — currently requires hand-editing the YAML file
- No YAML editing required once this lands

---

## Known Unknowns

| Item | Notes |
|---|---|
| `setGmtClock` params | Unknown format — needs app HTTP capture |
| `setSetting` params | Unknown format — needs app HTTP capture |
| LLM vision support (local) | ✅ confirmed — llama.cpp with `qwen36-35b-a3b` + mmproj file |
| Camera media count limit | Max observed ID ~437; no server-side count API |
| Camera dual-mode STA/AP | Probe requests for home WiFi seen — unconfirmed if joinable |
