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
settings.

**Run:**
```bash
cd web
GARDEPRO_WIFI_PASSWORD=<password> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080
```
Then open `http://<pi-ip>:8080` from any device on the home network.

**Files:**
- `web/server.py` — FastAPI backend: BLE wake, WiFi connect, media proxy, HLS streaming
- `web/static/` — Vanilla HTML/JS/CSS frontend (no build step)
- `web/PLAN.md` — Full architecture, phase status, and open questions

**Dependencies:** `sudo apt-get install -y python3-fastapi python3-uvicorn ffmpeg`

Phase 1 (core gallery, live view, settings read) is complete. Phase 2 (settings
write, time sync) is blocked pending BLE Level 1–3 auth implementation. See
`web/PLAN.md` for details.
