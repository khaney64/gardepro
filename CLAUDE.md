# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

**Systemd (production on Pi):**
```bash
sudo systemctl status gardepro
sudo systemctl restart gardepro   # pick up code changes
journalctl -u gardepro -f
```
Credentials live in `/etc/gardepro.env`. Web UI at `http://<pi-ip>:8080`.

**Manual:**
```bash
cd web
GARDEPRO_WIFI_PASSWORD=<pw> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```

**Dependencies:** `sudo apt-get install -y python3-fastapi python3-uvicorn ffmpeg`  
Python packages: `bleak requests pyyaml` (plus `anthropic` if using that backend)

## Architecture

### BLE + WiFi layer (`ble_wake.py`)
Imported directly by `web/server.py` via `sys.path` manipulation. Provides:
- `find_device()` — BLE scan for the camera
- `linux_connect_wifi()` / `linux_disconnect_wifi()` / `linux_wait_for_ip()` — nmcli-based WiFi management
- Wake sequence: writes `AT+WAKEPULSE=10\r\n` to BLE char `6e400004`; camera opens WiFi hotspot SSID `CAM8Z8_<MAC>`

### Backend (`web/server.py`)
FastAPI app with a single shared `_state` dict that drives all UI updates. Key patterns:
- All blocking I/O (requests, subprocess) runs via `asyncio.to_thread()`
- Real-time push uses SSE (`/api/events`); every state change calls `_broadcast()`
- Connection lifecycle: `_connection_flow()` → background tasks (keepalive, signal poll, thumb cache, analysis) → `_disconnect_flow()`
- Media enumeration uses `GET /list/detail/backward/{ts}/{count}` (native listing endpoint, same as the official app). Response: `{"code":0,"data":[{"id":N,"type":1|2,"date":"YYYY-MM-DD HH:MM:SS",...}]}` — type 1=jpg, type 2=mp4. Paging: `{ts}` is an **item id** (not epoch); pass `min(id)` from the current page as `{ts}` for the next call. End of card: empty `data` array. Falls back to the old sequential `/file/{id}/JPG` probe (`_enumerate_media_probe`) if the listing call fails or returns an unexpected schema.
- Auto-sync loop (`GARDEPRO_AUTO_CONNECT=1`): connect → enumerate → cache thumbs → analyze → disconnect, with `AUTO_SYNC_RETRIES=2`. Auto-sync is **paused** when the last-known battery level is at or below `BATTERY_AUTOSYNC_DISABLE_PCT` (default 10%) to preserve power; manual connect still works.
- **Battery monitoring** (`_battery_poll_loop`): polls `GET /cmd/info/2` every 60 s while connected; broadcasts `{"type":"battery","pct","mv","temp","ext_power","updated"}` over SSE and persists the reading to the DB (`set_battery`) so it shows in the UI badge while disconnected. `_evaluate_battery_alerts()` fires `alerter.send_battery_alert()` when crossing `battery_warning_threshold` (default 25%) and every 5% below that; resets on recovery. BLE connect/ops/disconnect all have hard `asyncio.wait_for` timeouts (`BLE_CONNECT_TIMEOUT=20s`, `BLE_OP_TIMEOUT=10s`, `BLE_DISCONNECT_TIMEOUT=5s`) to prevent BlueZ from wedging the connect flow.

### Cache layer (`web/db.py`)
SQLite at `~/.gardepro/cache.db`. Three tables: `media` (camera index + analysis), `saved_media` (Pi-local copies), `meta` (last_synced timestamp + last-known battery JSON). Schema migrates forward with `ALTER TABLE ... ADD COLUMN` try/except. The `upsert_media()` method detects ID reuse after on-camera deletion by comparing `captured_at` timestamps and resets all cache flags. `set_battery()`/`get_battery()` persist the last-known power status via the `meta` table so it survives restarts.

**Offline delete / pending_delete flag:** The `media` table has a `pending_delete` column (default 0). When the user deletes an image while disconnected, `mark_for_deletion()` sets this flag; the item is hidden from all queries (`get_all_media`, `get_uncached_thumbs`, `get_unanalyzed_media` all filter `pending_delete=0`). On the next connection, `_flush_pending_deletions()` runs before `_enumerate_media()`, deletes each flagged item from the camera, then permanently removes it from the DB. If the camera is unreachable mid-flush the item stays flagged and retries next connect. `upsert_media()` never clears the flag, so a re-scan cannot resurrect a marked item.

### LLM analysis (`web/analyzer.py`)
Two backends selectable at runtime:
- **Local** (`backend: "llm"`): llama.cpp OpenAI-compatible API at `GARDEPRO_LLM_URL`; sends thinking via `chat_template_kwargs`
- **Anthropic** (`backend: "anthropic"`): direct Anthropic Messages API; sends thinking via `thinking` block

Config persisted at `~/.gardepro/analysis_config.json`. Subject detection is keyword-matching over LLM text output (`_KEYWORDS` list).

**Thinking budget retry:** When a model exhausts its thinking budget it can return a response with no text — the Anthropic path returns `""` via `next(..., "")` and the local path guards against a `None` content field with `or ""`. Both `analyze_image()` and `chat_image()` detect an empty `description` after a successful call and retry once with `thinking_budget * 2`. Retries are logged as WARNING/INFO via Python's `logging` module (visible in `journalctl`).

### Alert engine (`web/alerter.py`)
Rules loaded from `~/.gardepro/alerts.yaml` at startup. Each rule has `name`, `keywords`, `action` (log|email), optional `catch_all: true`. Dedup: `_fired` set prevents re-alerting the same image; `_last_fired` dict enforces per-rule cooldown. Email sends inline thumbnail via CID attachment.

### Frontend (`web/static/`)
Vanilla HTML/JS/CSS — no build step. Connects to SSE stream on load; dispatches on `type` field (`state`, `log`, `media_progress`, `cache_progress`, `analysis_update`, `signal`, `media_deleted`). No framework or bundler.

## Key environment variables

| Variable | Purpose |
|---|---|
| `GARDEPRO_WIFI_PASSWORD` | Required for connect |
| `GARDEPRO_BLE_ADDRESS` | Skip BLE scan (e.g. `AA:BB:CC:DD:EE:FF`) |
| `GARDEPRO_WIFI_IFACE` | Override WiFi interface (default: first `wlx*`) |
| `GARDEPRO_AUTO_CONNECT` | Set to `1` to enable background auto-sync |
| `GARDEPRO_LLM_URL` | llama.cpp base URL |
| `GARDEPRO_LLM_MODEL` | Model name for local LLM |
| `ANTHROPIC_API_KEY` | Required for Anthropic backend |
| `GARDEPRO_ALERT_EMAIL` | Recipient for email alerts |
| `GARDEPRO_ALERT_SMTP_PASSWORD` | SMTP password or app token |

## Camera HTTP API (at 192.168.8.1:8080)

Endpoints below were verified by sniffing the official GardePro app's traffic
(WiFi monitor capture + WPA2 decrypt) against a **GardePro E6P** (firmware V6.2.110).
The app uses **GET for reads and POST for writes** — using the wrong method makes the
camera silently ignore the request (this is why the old `GET /cmd/format/start` never
formatted the card).

**Media / files (GET):**
- `GET /file/{id}/JPG` — full file (Content-Type determines jpg vs mp4)
- `GET /thumb/{id}/{JPG|MP4}` — thumbnail
- `GET /list/detail/forward/{id}/{count}` — media listing (JSON), forward from id
- `GET /list/detail/backward/{ts}/{count}` — media listing (JSON), returns up to `{count}` items with id < `{ts}`, newest-first. `{ts}` is an **item id** (not a Unix epoch); use `9999999999` for the first call, then `min(id)` from each page for the next. Response: `{"code":0,"data":[{"id":N,"type":1|2,"date":"YYYY-MM-DD HH:MM:SS","size":bytes,"uid":"hex"},...]}`; type 1=jpg, type 2=mp4. Empty `data` array means end of card.

**Reads (GET):**
- `GET /cmd/getSetting` — current settings (verbose keys: `pir`, `date_stamp`,
  `sd_override`, `video_length`, `standby_timeout`, `mode`, …). These are the keys
  `setSetting` writes.
- `GET /cmd/getParaSetting` — settings menu + valid-value lists (`pir_delay_list`,
  `video_len_list`, `timezone[]`, `photo_burst`, `mode_menu`, …). Different namespace
  from `getSetting`.
- `GET /cmd/info/1` — device info `{"brand","product","ver"}`
- `GET /cmd/info/2` — **power status** `{"temperature":°C,"voltage":pct,"vol_value":mV,"ext_power":n}`
  where `voltage` is battery **percent**, `vol_value` is millivolts, and `ext_power`
  is `0` on battery / non-zero on external (DC/solar) power. Polled by `_battery_poll_loop`.
- `GET /cmd/info/4` — clock + timezone `{"clock","tz"}`
- `GET /media/getIrStatus` — IR LED status `{"irStatus","irPower"}`
- `GET /cmd/standby/reset` — keepalive

**Writes (POST, body is JSON `application/json`):**
- `POST /cmd/setSetting` body `{"data":{"<key>":<int>}}` — change ONE setting per call
  (e.g. PIR sensitivity `{"data":{"pir":1}}`, loop/overwrite recording `{"data":{"sd_override":1}}`,
  date stamp `{"data":{"date_stamp":1}}`). Response `{"code":0}`; the value is NOT echoed,
  so re-read `getSetting` to refresh. PIR enum: **0=High, 1=Medium, 2=Low**.
- `POST /cmd/setGmtClock` body `{"data":"YYYY-MM-DD HH:MM:SS"}` — set clock
- `POST /cmd/format/start` body `{}` → `{"code":0,"desc":"OK"}`, then poll
  `POST /cmd/format/result` body `{}` until `{"code":0,"desc":"success"}`. See `api_format`.
- `POST /cmd/standby/now` — sleep camera

**Delete:** `GET /cmd/delete/{id}/{KIND}` — delete file
