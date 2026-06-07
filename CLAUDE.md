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
- Media enumeration probes `/file/{id}/JPG` sequentially; stops after `MAX_MISSES=10` consecutive misses past both the trailing gap and the DB floor (to handle on-camera deletions)
- Auto-sync loop (`GARDEPRO_AUTO_CONNECT=1`): connect → enumerate → cache thumbs → analyze → disconnect, with `AUTO_SYNC_RETRIES=2`

### Cache layer (`web/db.py`)
SQLite at `~/.gardepro/cache.db`. Three tables: `media` (camera index + analysis), `saved_media` (Pi-local copies), `meta` (last_synced timestamp). Schema migrates forward with `ALTER TABLE ... ADD COLUMN` try/except. The `upsert_media()` method detects ID reuse after on-camera deletion by comparing `captured_at` timestamps and resets all cache flags.

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

- `GET /file/{id}/JPG` — full file (Content-Type determines jpg vs mp4)
- `GET /thumb/{id}/JPG` — thumbnail
- `GET /cmd/getSetting` — camera settings
- `GET /cmd/delete/{id}/{KIND}` — delete file
- `GET /cmd/standby/reset` — keepalive
- `GET /cmd/standby/now` — sleep camera
- `GET /cmd/format/start` — format SD card
