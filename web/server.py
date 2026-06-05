"""
GardePro Camera Web Server

Manages BLE wake, WiFi connection, and proxies the camera HTTP/RTSP APIs
to a browser-accessible web interface.

Run:   cd /home/<user>/gardepro/web
       GARDEPRO_WIFI_PASSWORD=<pw> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080

Environment variables:
  GARDEPRO_WIFI_PASSWORD          Camera WiFi password (required for connect)
  GARDEPRO_BLE_ADDRESS            BLE address to skip scanning, e.g. AA:BB:CC:DD:EE:FF
  GARDEPRO_BLE_ADAPTER            Bluetooth HCI adapter (default: hci0)
  GARDEPRO_WIFI_IFACE             WiFi interface for camera (default: first wlx* found)
  GARDEPRO_RTSP_PORT              Local port for RTSP TCP proxy (default: 8554)
  GARDEPRO_AUTO_CONNECT           Set to 1 to enable periodic background sync
  GARDEPRO_SYNC_INTERVAL          Seconds between auto-sync attempts (default: 600)
  GARDEPRO_LLM_URL                Base URL of llama.cpp OpenAI API (default: http://devbox.lan:8080)
  GARDEPRO_LLM_MODEL              Model name for vision analysis (required for analysis)
  GARDEPRO_ALERT_EMAIL            Recipient address for email alerts
  GARDEPRO_ALERT_FROM_EMAIL       Sender address (defaults to GARDEPRO_ALERT_EMAIL)
  GARDEPRO_ALERT_SMTP_HOST        SMTP server hostname (default: smtp.gmail.com)
  GARDEPRO_ALERT_SMTP_PORT        SMTP server port (default: 587)
  GARDEPRO_ALERT_SMTP_SSL         Set to 1 for implicit SSL; auto-detected when port is 465
  GARDEPRO_ALERT_SMTP_USER        SMTP auth username (defaults to GARDEPRO_ALERT_FROM_EMAIL; use API token for Postmark)
  GARDEPRO_ALERT_SMTP_PASSWORD    SMTP password or app password
"""

import asyncio
import collections
import datetime
from email.utils import parsedate_to_datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Import WiFi/BLE helpers from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from ble_wake import (
    AT_CHAR,
    WAKE_CMD,
    bluez_args,
    find_device,
    linux_connect_wifi,
    linux_disconnect_wifi,
    linux_get_wifi_interface,
    linux_scan_wifi,
    linux_wait_for_ip,
)
from bleak import BleakClient

from db import CacheDB, THUMB_DIR, FILES_DIR, SAVED_DIR
import analyzer
import alerter

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_BLE_ADDRESS  = os.environ.get("GARDEPRO_BLE_ADDRESS") or None
BLE_ADAPTER         = os.environ.get("GARDEPRO_BLE_ADAPTER", "hci0")
WIFI_IFACE_OVERRIDE = os.environ.get("GARDEPRO_WIFI_IFACE") or None
CAMERA_IP           = "192.168.8.1"
CAMERA_PORT         = 8080
RTSP_PORT_LOCAL     = int(os.environ.get("GARDEPRO_RTSP_PORT", "8554"))
HLS_TMP_DIR         = Path("/tmp/gardepro_hls")
STATIC_DIR          = Path(__file__).parent / "static"
AUTO_CONNECT        = os.environ.get("GARDEPRO_AUTO_CONNECT", "").strip() in ("1", "true", "yes")
SYNC_INTERVAL       = int(os.environ.get("GARDEPRO_SYNC_INTERVAL", "600"))

# ── Shared camera session ─────────────────────────────────────────────────────

_cam_session = requests.Session()
_cam_session.trust_env = False

_db = CacheDB()

# ── Global state ──────────────────────────────────────────────────────────────

_state: dict = {
    "status":        "disconnected",  # disconnected|connecting|connected|disconnecting
    "step":          "",
    "camera_ip":     None,
    "my_ip":         None,
    "signal_dbm":    None,
    "signal_label":  None,
    "media_count":   0,
    "rtsp_url":      None,
    "hls_available": shutil.which("ffmpeg") is not None,
    "error":         None,
    "last_synced":   None,
    "last_event":    None,
}
# Full media list kept separate (too large to include in every SSE broadcast)
_media: list[dict] = []

_sse_queues:        list[asyncio.Queue] = []
_log_entries:       collections.deque = collections.deque(maxlen=200)
_connect_task:      Optional[asyncio.Task] = None
_background_tasks:  list[asyncio.Task] = []
_thumb_cache_task:  Optional[asyncio.Task] = None
_rtsp_server       = None
_hls_proc          = None
_shutting_down      = False
_alert_rules:       list[dict] = []
_analysis_config:   dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enabled_alert_rules() -> list[dict]:
    """Filter _alert_rules to only those enabled in _analysis_config."""
    enabled_map = _analysis_config.get("alert_rules_enabled") or {}
    return [r for r in _alert_rules if enabled_map.get(r.get("name"), True)]


def _parse_http_date(s: str) -> Optional[str]:
    try:
        dt = parsedate_to_datetime(s)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _dbm_label(dbm: int) -> str:
    if dbm >= -55: return "Excellent"
    if dbm >= -65: return "Good"
    if dbm >= -75: return "Fair"
    return "Poor"


def _get_pi_ip() -> str:
    """Outbound IP of this machine on the home network."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


def _broadcast_state() -> dict:
    """State snapshot safe to broadcast (excludes full media list)."""
    return {"type": "state", **_state}


async def _broadcast(data: dict):
    msg = json.dumps(data)
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass


def _log_sync(msg: str) -> dict:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg}
    _log_entries.append(entry)
    print(f"[{ts}] {msg}", flush=True)
    return entry


async def _log(msg: str):
    entry = _log_sync(msg)
    await _broadcast({"type": "log", **entry})


async def _set_step(step: str):
    _state["step"] = step
    _state["error"] = None
    await _log(step)
    await _broadcast(_broadcast_state())


# ── RTSP TCP proxy ────────────────────────────────────────────────────────────

async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle_rtsp_client(lr: asyncio.StreamReader, lw: asyncio.StreamWriter):
    try:
        cr, cw = await asyncio.open_connection(CAMERA_IP, 554)
        await asyncio.gather(_pipe(lr, cw), _pipe(cr, lw))
    except Exception:
        pass
    finally:
        try:
            lw.close()
        except Exception:
            pass


async def _start_rtsp_proxy():
    global _rtsp_server
    try:
        _rtsp_server = await asyncio.start_server(
            _handle_rtsp_client, "0.0.0.0", RTSP_PORT_LOCAL
        )
        _state["rtsp_url"] = f"rtsp://{_get_pi_ip()}:{RTSP_PORT_LOCAL}/live.sdp"
    except Exception as exc:
        _state["rtsp_url"] = None
        _log_sync(f"RTSP proxy failed on port {RTSP_PORT_LOCAL}: {exc}")


async def _stop_rtsp_proxy():
    global _rtsp_server
    if _rtsp_server:
        _rtsp_server.close()
        try:
            await _rtsp_server.wait_closed()
        except Exception:
            pass
        _rtsp_server = None
    _state["rtsp_url"] = None


# ── HLS via ffmpeg ────────────────────────────────────────────────────────────

async def _stop_hls():
    global _hls_proc
    if _hls_proc and _hls_proc.returncode is None:
        _hls_proc.terminate()
        try:
            await asyncio.wait_for(_hls_proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            _hls_proc.kill()
    _hls_proc = None


# ── Background tasks ──────────────────────────────────────────────────────────

async def _keepalive_loop():
    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/standby/reset"
    while _state["status"] == "connected":
        try:
            await asyncio.to_thread(
                lambda: _cam_session.get(url, timeout=(1.0, 3.0))
            )
        except Exception:
            pass
        await asyncio.sleep(60)


async def _signal_poll_loop(iface: str):
    while _state["status"] == "connected":
        try:
            out = await asyncio.to_thread(
                subprocess.check_output,
                ["iw", "dev", iface, "link"],
                text=True, stderr=subprocess.DEVNULL, timeout=5
            )
            m = re.search(r"signal:\s*([-\d]+)\s*dBm", out)
            if m:
                dbm = int(m.group(1))
                _state["signal_dbm"] = dbm
                _state["signal_label"] = _dbm_label(dbm)
                await _broadcast({"type": "signal", "dbm": dbm,
                                  "label": _dbm_label(dbm)})
        except Exception:
            pass
        await asyncio.sleep(10)


# ── Media enumeration ─────────────────────────────────────────────────────────

async def _enumerate_media() -> list[dict]:
    global _media
    results: list[dict] = []
    misses = 0
    MAX_MISSES = 10
    media_id = 1

    while misses < MAX_MISSES:
        # Probe /file/ (not /thumb/) — both /JPG and /MP4 suffixes return the
        # same underlying file; Content-Type tells us the actual type.
        url = f"http://{CAMERA_IP}:{CAMERA_PORT}/file/{media_id}/JPG"
        found = False
        try:
            resp = await asyncio.to_thread(
                lambda u=url: _cam_session.get(u, timeout=1.5, stream=True)
            )
            code = resp.status_code
            ct = resp.headers.get("content-type", "").lower()
            lm = resp.headers.get("last-modified")
            resp.close()
            if code == 200:
                kind = "mp4" if "video" in ct else "jpg"
                results.append({
                    "id": media_id, "kind": kind,
                    "captured_at": _parse_http_date(lm) if lm else None,
                })
                found = True
        except Exception:
            pass

        misses = 0 if found else misses + 1
        media_id += 1

        # Push incremental progress every 6 items
        if found and len(results) % 6 == 0:
            _media = list(results)
            _state["media_count"] = len(results)
            await _broadcast({"type": "media_progress", "count": len(results)})

    _media = results
    _state["media_count"] = len(results)

    def _sync_to_db(items):
        for item in items:
            _db.upsert_media(item["id"], item["kind"], item.get("captured_at"))
        _db.set_last_synced()
        _state["last_synced"] = _db.get_last_synced()
        _state["last_event"]  = _db.get_last_event_time()

    await asyncio.to_thread(_sync_to_db, results)
    return results


# ── Background tasks (cache) ───────────────────────────────────────────────────

async def _thumb_cache_loop():
    """Download uncached thumbnails to ~/.gardepro/thumbs/ in the background."""
    pending = await asyncio.to_thread(_db.get_uncached_thumbs)
    total = len(pending)
    if not total:
        return

    await _broadcast({"type": "cache_progress", "cached": 0, "total": total})
    for i, item in enumerate(pending):
        if _state["status"] != "connected":
            break
        id_, kind = item["id"], item["kind"]
        url = f"http://{CAMERA_IP}:{CAMERA_PORT}/thumb/{id_}/{kind.upper()}"
        dest = THUMB_DIR / f"{id_}_{kind}.jpg"
        try:
            resp = await asyncio.to_thread(
                lambda u=url: _cam_session.get(u, timeout=5)
            )
            if resp.status_code == 200:
                await asyncio.to_thread(dest.write_bytes, resp.content)
                await asyncio.to_thread(_db.mark_thumb_cached, id_, kind, str(dest))
        except Exception:
            pass
        if (i + 1) % 10 == 0 or i + 1 == total:
            await _broadcast({"type": "cache_progress",
                              "cached": i + 1, "total": total})


async def _sync_cache():
    """Re-enumerate and cache new thumbnails on an existing connection."""
    await _enumerate_media()
    await _thumb_cache_loop()
    await _analysis_loop()


async def _analysis_loop():
    """Analyze unanalyzed cached thumbnails with LLM and fire alerts."""
    if not _analysis_config.get("analyze_enabled", True):
        return
    pending = await asyncio.to_thread(_db.get_unanalyzed_media)
    if not pending:
        return
    await _log(f"Analysis: processing {len(pending)} image(s)…")
    pi_host = f"{_get_pi_ip()}:8080"
    cfg = dict(_analysis_config)
    for item in pending:
        id_, kind, thumb_path = item["id"], item["kind"], item.get("thumb_path") or ""
        if not thumb_path or not Path(thumb_path).exists():
            continue
        await _log(f"Analysis: [{kind.upper()} {id_}] analyzing…")
        result = await analyzer.analyze_image(thumb_path, cfg)
        result_json = json.dumps(result)
        await asyncio.to_thread(_db.update_analysis, id_, kind, result_json)
        subjects = result.get("subjects", [])
        await _broadcast({"type": "analysis_update", "id": id_, "kind": kind,
                          "subjects": subjects, "description": result.get("description", "")})
        if result.get("error"):
            await _log(f"Analysis: [{kind.upper()} {id_}] error — {result['error']}")
        else:
            subj_str = ', '.join(subjects) if subjects else 'nothing detected'
            snippet  = result.get('description', '').replace('\n', ' ')[:200]
            engine   = result.get('engine', '')
            await _log(f"Analysis: [{kind.upper()} {id_}] {subj_str} | {snippet}" + (f" [{engine}]" if engine else ""))
        if _alert_rules and subjects and _analysis_config.get("alerts_enabled", False):
            rules = _enabled_alert_rules()
            cooldown = float(_analysis_config.get("alert_cooldown_minutes", 30)) * 60
            triggered, alert_errors = await asyncio.to_thread(
                alerter.check_and_alert, result, id_, kind, rules, pi_host, cooldown
            )
            if triggered:
                await _log(f"Analysis: alert triggered — {', '.join(triggered)} (media {id_}/{kind})")
            for err in alert_errors:
                await _log(f"Analysis: {err}")
    await _log("Analysis: done")


async def _auto_sync_loop():
    """
    Periodic background sync.
    - If disconnected: connect → wait for thumbnail caching → disconnect.
    - If already connected (manual session): enumerate + cache only, no disconnect.
    Live stream requires a manual connection; auto-sync does not keep a persistent session.
    """
    await asyncio.sleep(10)  # let uvicorn fully start
    while not _shutting_down:
        await asyncio.sleep(SYNC_INTERVAL)
        if _shutting_down:
            break
        if _state["status"] == "connected":
            await _log("Auto-sync: re-enumerating on active session…")
            await _sync_cache()
            await _log("Auto-sync: done")
        elif _state["status"] == "disconnected":
            await _log("Auto-sync: starting background connection…")
            try:
                await _connection_flow()
                if _state["status"] == "connected":
                    if _thumb_cache_task and not _thumb_cache_task.done():
                        try:
                            await _thumb_cache_task
                        except Exception:
                            pass
                    await _analysis_loop()
                    await _disconnect_flow()
                    await _log("Auto-sync: completed successfully")
                else:
                    await _log("Auto-sync: connection failed (see error above)")
            except Exception as exc:
                await _log(f"Auto-sync: exception — {exc}")


# ── Connection flow ───────────────────────────────────────────────────────────

async def _connection_flow():
    global _media
    iface = await asyncio.to_thread(linux_get_wifi_interface, WIFI_IFACE_OVERRIDE)

    try:
        password = os.environ.get("GARDEPRO_WIFI_PASSWORD", "").strip()
        if not password:
            raise RuntimeError(
                "Camera WiFi password not set. "
                "Start the server with GARDEPRO_WIFI_PASSWORD=<password>"
            )

        _state["status"] = "connecting"
        await _set_step("Scanning for camera via Bluetooth…")

        address = await find_device(CAMERA_BLE_ADDRESS, BLE_ADAPTER)
        if not address:
            raise RuntimeError("Camera not found via BLE. Is it powered on and nearby?")

        await _set_step("Sending wake pulse…")

        ble_kwargs = {}
        adapter_args = bluez_args(BLE_ADAPTER)
        if adapter_args:
            ble_kwargs["bluez"] = adapter_args

        async with BleakClient(address, **ble_kwargs) as client:
            await client.start_notify(AT_CHAR, lambda _c, _d: None)
            for _ in range(3):
                await client.write_gatt_char(AT_CHAR, WAKE_CMD, response=True)
                await asyncio.sleep(0.4)

        raw_addr = getattr(address, "address", str(address))
        ssid = "CAM8Z8_" + raw_addr.replace(":", "").upper()
        await _set_step(f"Waiting for hotspot {ssid}…")

        found = False
        for _ in range(60):
            await asyncio.sleep(1)
            nets = await asyncio.to_thread(linux_scan_wifi, iface)
            if ssid in nets:
                found = True
                break
        if not found:
            raise RuntimeError(f"Hotspot {ssid!r} did not appear within 60 s.")

        await _set_step(f"Connecting {iface} to {ssid}…")
        ok = await asyncio.to_thread(linux_connect_wifi, ssid, password, iface)
        if not ok:
            raise RuntimeError("WiFi connection failed — check password.")

        await _set_step("Waiting for DHCP…")
        my_ip, _ = await asyncio.to_thread(linux_wait_for_ip, iface, 90)
        if not my_ip:
            raise RuntimeError("DHCP timed out — camera may need more time. Try again.")

        _state["camera_ip"] = CAMERA_IP
        _state["my_ip"] = my_ip

        await _set_step("Verifying camera HTTP…")
        try:
            probe = await asyncio.to_thread(
                lambda: _cam_session.get(
                    f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/getSetting", timeout=5
                )
            )
            if probe.status_code >= 500:
                raise RuntimeError(
                    f"Camera returned HTTP {probe.status_code} — WiFi session may have expired."
                )
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                "Camera not responding at 192.168.8.1:8080 — session may have timed out. Try again."
            )
        except requests.exceptions.Timeout:
            raise RuntimeError("Camera HTTP timed out. Try disconnecting and reconnecting.")

        await _set_step("Scanning media library…")
        await _enumerate_media()

        _state["status"] = "connected"
        _state["step"] = ""
        await _start_rtsp_proxy()
        await _broadcast(_broadcast_state())

        global _thumb_cache_task
        _background_tasks.clear()
        _background_tasks.append(asyncio.create_task(_keepalive_loop()))
        _background_tasks.append(asyncio.create_task(_signal_poll_loop(iface)))
        _thumb_cache_task = asyncio.create_task(_thumb_cache_loop())
        _background_tasks.append(_thumb_cache_task)
        async def _chain_analysis():
            try:
                await _thumb_cache_task
            except Exception:
                pass
            await _analysis_loop()
        _background_tasks.append(asyncio.create_task(_chain_analysis()))

    except asyncio.CancelledError:
        await _log("Connection cancelled")
        all_rows = await asyncio.to_thread(_db.get_all_media)
        _media[:] = [{"id": r["id"], "kind": r["kind"]} for r in all_rows]
        _state.update({
            "status": "disconnected", "step": "", "error": "Cancelled",
            "media_count": len(_media), "last_synced": _db.get_last_synced(),
        })
        try:
            await asyncio.to_thread(linux_disconnect_wifi, iface)
        except Exception:
            pass
        await _broadcast(_broadcast_state())
        raise

    except Exception as exc:
        await _log(f"Connection failed: {exc}")
        all_rows = await asyncio.to_thread(_db.get_all_media)
        _media[:] = [{"id": r["id"], "kind": r["kind"]} for r in all_rows]
        _state.update({
            "status": "disconnected", "step": "", "error": str(exc),
            "media_count": len(_media), "last_synced": _db.get_last_synced(),
        })
        try:
            await asyncio.to_thread(linux_disconnect_wifi, iface)
        except Exception:
            pass
        await _broadcast(_broadcast_state())


async def _disconnect_flow():
    global _media
    iface = await asyncio.to_thread(linux_get_wifi_interface, WIFI_IFACE_OVERRIDE)

    _state["status"] = "disconnecting"
    await _set_step("Disconnecting…")

    for t in _background_tasks:
        t.cancel()
    _background_tasks.clear()

    await _stop_hls()
    await _stop_rtsp_proxy()

    try:
        await asyncio.to_thread(
            lambda: _cam_session.get(
                f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/standby/now",
                timeout=(1.0, 3.0)
            )
        )
    except Exception:
        pass

    await asyncio.to_thread(linux_disconnect_wifi, iface)

    all_rows = await asyncio.to_thread(_db.get_all_media)
    _media[:] = [{"id": r["id"], "kind": r["kind"]} for r in all_rows]
    _state.update({
        "status": "disconnected", "step": "", "camera_ip": None,
        "my_ip": None, "signal_dbm": None, "signal_label": None,
        "media_count": len(_media), "rtsp_url": None, "error": None,
        "last_synced": _db.get_last_synced(),
    })
    await _broadcast(_broadcast_state())


# ── Startup / shutdown ────────────────────────────────────────────────────────

async def _resume_session(iface: str, my_ip: str):
    """Called as a background task if Edimax is already on the camera subnet at startup."""
    global _media
    await _log(f"Startup: resuming session on {iface} ({my_ip})")
    _state["step"] = "Reconnecting to camera…"
    await _broadcast(_broadcast_state())

    async def _restore_cached(error: str = ""):
        """Fall back to cached gallery and disconnect WiFi."""
        all_rows = await asyncio.to_thread(_db.get_all_media)
        _media[:] = [{"id": r["id"], "kind": r["kind"]} for r in all_rows]
        _state.update({
            "status": "disconnected", "step": "", "error": error or None,
            "media_count": len(_media),
            "last_synced": _db.get_last_synced(),
            "last_event":  _db.get_last_event_time(),
        })
        try:
            await asyncio.to_thread(linux_disconnect_wifi, iface)
        except Exception:
            pass
        await _broadcast(_broadcast_state())

    # Quick probe — camera may have gone to sleep since the interface still has its IP
    try:
        probe = await asyncio.to_thread(
            lambda: _cam_session.get(
                f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/getSetting", timeout=5
            )
        )
        if probe.status_code >= 500:
            raise RuntimeError(f"Camera returned HTTP {probe.status_code}")
    except Exception as exc:
        await _log(f"Startup: camera not reachable ({exc}) — restoring cached gallery")
        await _restore_cached()
        return

    try:
        await _enumerate_media()
        await _start_rtsp_proxy()
        _state.update({"status": "connected", "step": ""})
        await _broadcast(_broadcast_state())
        global _thumb_cache_task
        _background_tasks.append(asyncio.create_task(_keepalive_loop()))
        _background_tasks.append(asyncio.create_task(_signal_poll_loop(iface)))
        _thumb_cache_task = asyncio.create_task(_thumb_cache_loop())
        _background_tasks.append(_thumb_cache_task)
        async def _chain_analysis_resume():
            try:
                await _thumb_cache_task
            except Exception:
                pass
            await _analysis_loop()
        _background_tasks.append(asyncio.create_task(_chain_analysis_resume()))
    except Exception as exc:
        await _log(f"Startup: session resume failed — {exc}")
        await _restore_cached(str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _media, _alert_rules
    # Open cache DB and restore media list for offline gallery
    _db.open()
    _analysis_config.update(analyzer._load_config())
    _alert_rules = alerter.load_rules("~/.gardepro/alerts.yaml")
    if _alert_rules:
        _log_sync(f"Loaded {len(_alert_rules)} alert rule(s) from ~/.gardepro/alerts.yaml")
    all_rows = await asyncio.to_thread(_db.get_all_media)
    if all_rows:
        _media = [{"id": r["id"], "kind": r["kind"]} for r in all_rows]
        _state["media_count"] = len(_media)
        _state["last_synced"] = _db.get_last_synced()
        _state["last_event"]  = _db.get_last_event_time()

    if AUTO_CONNECT:
        asyncio.ensure_future(_auto_sync_loop())

    # Detect if already connected (e.g. server restarted while camera was up)
    iface = linux_get_wifi_interface(WIFI_IFACE_OVERRIDE)
    try:
        out = subprocess.check_output(
            ["ip", "addr", "show", iface], text=True, timeout=3
        )
        m = re.search(r"inet\s+(192\.168\.8\.\d+)/", out)
        if m:
            my_ip = m.group(1)
            # Stay in "connecting" until _resume_session finishes enumeration
            _state.update({
                "status": "connecting", "camera_ip": CAMERA_IP, "my_ip": my_ip,
            })
            asyncio.ensure_future(_resume_session(iface, my_ip))
    except Exception:
        pass

    yield

    # Shutdown (CTRL-C / SIGTERM) — signal SSE clients to close, then clean up
    global _shutting_down
    _shutting_down = True
    for q in list(_sse_queues):
        try:
            q.put_nowait(None)  # None sentinel causes generators to exit
        except Exception:
            pass
    await asyncio.sleep(0.3)  # brief pause for generators to drain

    for t in _background_tasks:
        t.cancel()
    await _stop_hls()
    await _stop_rtsp_proxy()

    if _state["status"] in ("connected", "connecting"):
        iface = linux_get_wifi_interface(WIFI_IFACE_OVERRIDE)
        try:
            await asyncio.to_thread(
                lambda: _cam_session.get(
                    f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/standby/now",
                    timeout=(1.0, 3.0)
                )
            )
        except Exception:
            pass
        try:
            await asyncio.to_thread(linux_disconnect_wifi, iface)
        except Exception:
            pass

    _db.close()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


@app.get("/api/status")
async def api_status():
    return {**_state, "media_count": len(_media)}


@app.get("/api/logs")
async def api_logs():
    return {"entries": list(_log_entries)}


@app.get("/api/events")
async def api_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_queues.append(q)

    async def gen():
        try:
            # Immediately send current state to new subscriber
            yield f"data: {json.dumps(_broadcast_state())}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    if msg is None:  # shutdown sentinel — close gracefully
                        break
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    if _shutting_down:
                        break
                    yield ": ping\n\n"
                if await request.is_disconnected():
                    break
        finally:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/sync")
async def api_sync():
    global _connect_task
    if _state["status"] == "connected":
        # Re-enumerate and cache on existing connection
        asyncio.create_task(_sync_cache())
        return {"status": "syncing"}
    elif _state["status"] == "disconnected":
        # Connect, sync, then auto-disconnect (same as auto-sync but on demand)
        async def _connect_sync_disconnect():
            global _thumb_cache_task
            try:
                await _connection_flow()
                if _state["status"] == "connected":
                    if _thumb_cache_task and not _thumb_cache_task.done():
                        try:
                            await _thumb_cache_task
                        except Exception:
                            pass
                    await _disconnect_flow()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                await _log(f"Sync: exception — {exc}")
        _connect_task = asyncio.create_task(_connect_sync_disconnect())
        return {"status": "connecting"}
    else:
        raise HTTPException(409, detail=f"Status is '{_state['status']}' — cannot sync now")


@app.post("/api/connect")
async def api_connect():
    global _connect_task
    if _state["status"] not in ("disconnected",):
        raise HTTPException(409, detail=f"Status is '{_state['status']}' — cannot connect")
    _connect_task = asyncio.create_task(_connection_flow())
    return {"status": "connecting"}


@app.post("/api/disconnect")
async def api_disconnect():
    global _connect_task
    if _connect_task and not _connect_task.done():
        _connect_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_connect_task), timeout=2)
        except Exception:
            pass
    if _state["status"] != "disconnected":
        asyncio.create_task(_disconnect_flow())
    return {"status": "disconnecting"}


@app.get("/api/media")
async def api_media(page: int = 0, size: int = 24):
    total = len(_media)
    items = _media[page * size: (page + 1) * size]
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
    }


@app.get("/api/analysis")
async def api_analysis():
    """Return analysis results for all analyzed media, keyed by 'id:kind'."""
    rows = await asyncio.to_thread(_db.get_media_with_analysis)
    result = {}
    for row in rows:
        key = f"{row['id']}:{row['kind']}"
        try:
            result[key] = json.loads(row["analysis_json"])
        except Exception:
            pass
    return result


@app.get("/api/analysis/config")
async def api_analysis_config_get():
    alert_email = os.environ.get("GARDEPRO_ALERT_EMAIL", "").strip()
    return {
        **_analysis_config,
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "alert_email": alert_email,
        "alert_rules": [r.get("name") for r in _alert_rules],
    }


@app.post("/api/analysis/config")
async def api_analysis_config_set(body: dict):
    scalar_fields = {
        "analyze_enabled": bool,
        "alerts_enabled": bool,
        "backend": str,
        "llm_url": str,
        "llm_model": str,
        "anthropic_model": str,
        "prompt": str,
        "max_tokens": int,
        "temperature": float,
        "alert_cooldown_minutes": int,
    }
    update = {}
    for key, typ in scalar_fields.items():
        if key in body:
            try:
                update[key] = typ(body[key])
            except (ValueError, TypeError):
                raise HTTPException(400, f"Invalid value for {key!r}")
    if "alert_rules_enabled" in body:
        val = body["alert_rules_enabled"]
        if not isinstance(val, dict):
            raise HTTPException(400, "alert_rules_enabled must be an object")
        update["alert_rules_enabled"] = {str(k): bool(v) for k, v in val.items()}
    saved = await asyncio.to_thread(analyzer.save_config, update)
    _analysis_config.update(saved)
    alert_email = os.environ.get("GARDEPRO_ALERT_EMAIL", "").strip()
    return {
        **_analysis_config,
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "alert_email": alert_email,
        "alert_rules": [r.get("name") for r in _alert_rules],
    }


@app.post("/api/alert/test-email")
async def api_alert_test_email():
    """Send a test email to verify alert email configuration."""
    try:
        await asyncio.to_thread(alerter.send_test_email)
        await _log("Alert: test email sent successfully")
        return {"success": True}
    except Exception as exc:
        await _log(f"Alert: test email failed — {exc}")
        raise HTTPException(500, str(exc))


@app.post("/api/analysis/run/saved/{saved_id}")
async def api_analysis_run_saved(saved_id: int):
    """Force re-analyze a saved media item using current config."""
    item = await asyncio.to_thread(_db.get_saved_by_id, saved_id)
    if not item:
        raise HTTPException(404, "Saved item not found")
    thumb = item.get("thumb_path") or ""
    if not thumb or not Path(thumb).exists():
        raise HTTPException(404, "Thumbnail not found — may have been moved")
    await _log(f"Analysis: [SAVED {saved_id}] analyzing…")
    result = await analyzer.analyze_image(thumb, _analysis_config)
    await asyncio.to_thread(_db.update_saved_analysis, saved_id, json.dumps(result))
    subjects = result.get("subjects", [])
    await _broadcast({"type": "saved_analysis_update", "saved_id": saved_id,
                      "subjects": subjects,
                      "description": result.get("description", "")})
    if result.get("error"):
        await _log(f"Analysis: [SAVED {saved_id}] error — {result['error']}")
    else:
        subj_str = ', '.join(subjects) if subjects else 'nothing detected'
        snippet  = result.get('description', '').replace('\n', ' ')[:200]
        engine   = result.get('engine', '')
        await _log(f"Analysis: [SAVED {saved_id}] {subj_str} | {snippet}" + (f" [{engine}]" if engine else ""))
    return {"saved_id": saved_id, **result}


@app.post("/api/analysis/run/{media_id}/{kind}")
async def api_analysis_run(media_id: int, kind: str):
    """Force re-analyze a single media item using current config."""
    kind_lower = kind.lower()
    cached = THUMB_DIR / f"{media_id}_{kind_lower}.jpg"
    if not cached.exists():
        raise HTTPException(404, "Thumbnail not cached — connect to camera first")
    await _log(f"Analysis: [{kind_lower.upper()} {media_id}] analyzing…")
    # Reset analyzed flag so it re-runs
    await asyncio.to_thread(_db.update_analysis, media_id, kind_lower, json.dumps({"subjects": [], "description": "", "pending": True}))
    result = await analyzer.analyze_image(str(cached), _analysis_config)
    await asyncio.to_thread(_db.update_analysis, media_id, kind_lower, json.dumps(result))
    subjects = result.get("subjects", [])
    await _broadcast({"type": "analysis_update", "id": media_id, "kind": kind_lower,
                      "subjects": subjects, "description": result.get("description", "")})
    if result.get("error"):
        await _log(f"Analysis: [{kind_lower.upper()} {media_id}] error — {result['error']}")
    else:
        subj_str = ', '.join(subjects) if subjects else 'nothing detected'
        snippet  = result.get('description', '').replace('\n', ' ')[:200]
        engine   = result.get('engine', '')
        await _log(f"Analysis: [{kind_lower.upper()} {media_id}] {subj_str} | {snippet}" + (f" [{engine}]" if engine else ""))
    if _alert_rules and subjects and _analysis_config.get("alerts_enabled", False):
        pi_host = f"{_get_pi_ip()}:8080"
        rules = _enabled_alert_rules()
        cooldown = float(_analysis_config.get("alert_cooldown_minutes", 30)) * 60
        triggered, alert_errors = await asyncio.to_thread(
            alerter.check_and_alert, result, media_id, kind_lower, rules, pi_host, cooldown
        )
        if triggered:
            await _log(f"Analysis: alert triggered — {', '.join(triggered)} (media {media_id}/{kind_lower})")
        for err in alert_errors:
            await _log(f"Analysis: {err}")
    return {"id": media_id, "kind": kind_lower, **result}


@app.get("/api/thumb/{media_id}/{kind}")
async def api_thumb(media_id: int, kind: str):
    # Serve from local cache first (works offline)
    cached = THUMB_DIR / f"{media_id}_{kind.lower()}.jpg"
    if cached.exists():
        return FileResponse(
            str(cached), media_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected and no cached thumbnail")
    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/thumb/{media_id}/{kind.upper()}"
    try:
        resp = await asyncio.to_thread(lambda: _cam_session.get(url, timeout=3))
        if resp.status_code != 200:
            raise HTTPException(404)
        return Response(
            content=resp.content,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/file/{media_id}/{kind}")
async def api_file(media_id: int, kind: str):
    kind_lower = kind.lower()
    mime = "video/mp4" if kind_lower == "mp4" else "image/jpeg"
    cached = FILES_DIR / f"{media_id}_{kind_lower}"

    # Serve from local cache (works offline)
    if cached.exists():
        return FileResponse(
            str(cached), media_type=mime,
            headers={"Cache-Control": "public, max-age=86400",
                     "Content-Disposition": f'inline; filename="cam_{media_id}.{kind_lower}"'},
        )

    if _state["status"] != "connected":
        raise HTTPException(404, "File not cached and camera not connected")

    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/file/{media_id}/{kind.upper()}"
    resp = await asyncio.to_thread(
        lambda: _cam_session.get(url, stream=True, timeout=(2, 120))
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code)

    it = resp.iter_content(chunk_size=65536)
    tmp = FILES_DIR / f".{media_id}_{kind_lower}.tmp"

    async def body():
        buf = bytearray()
        try:
            while True:
                chunk = await asyncio.to_thread(lambda: next(it, None))
                if chunk is None:
                    break
                buf.extend(chunk)
                yield bytes(chunk)
            # Full file received — persist to disk
            await asyncio.to_thread(tmp.write_bytes, bytes(buf))
            await asyncio.to_thread(tmp.rename, cached)
            await asyncio.to_thread(_db.mark_file_cached, media_id, kind_lower, str(cached))
        except Exception:
            await asyncio.to_thread(lambda: tmp.unlink(missing_ok=True))

    return StreamingResponse(
        body(), media_type=mime,
        headers={"Content-Disposition": f'inline; filename="cam_{media_id}.{kind_lower}"'},
    )


@app.delete("/api/file/{media_id}/{kind}")
async def api_delete(media_id: int, kind: str):
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/delete/{media_id}/{kind.upper()}"
    try:
        resp = await asyncio.to_thread(lambda: _cam_session.get(url, timeout=5))
        result = resp.json()
        if result.get("code") == 0:
            _media[:] = [m for m in _media
                         if not (m["id"] == media_id and m["kind"] == kind.lower())]
            _state["media_count"] = len(_media)
            await asyncio.to_thread(_db.delete_media, media_id, kind.lower())
            cached_thumb = THUMB_DIR / f"{media_id}_{kind.lower()}.jpg"
            await asyncio.to_thread(cached_thumb.unlink, True)
            await _broadcast({**_broadcast_state(), "type": "media_deleted",
                              "id": media_id, "kind": kind.lower()})
            return {"success": True}
        raise HTTPException(500, result.get("desc", "Delete failed"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/save/{media_id}/{kind}")
async def api_save(media_id: int, kind: str):
    kind_lower = kind.lower()
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.datetime.now()
    saved_at_iso = saved_at.strftime("%Y-%m-%dT%H:%M:%S")
    saved_at_fs  = saved_at.strftime("%Y%m%dT%H%M%S")

    # ── Thumbnail ──
    thumb_src = THUMB_DIR / f"{media_id}_{kind_lower}.jpg"
    if not thumb_src.exists():
        if _state["status"] != "connected":
            raise HTTPException(503, "Thumbnail not cached — connect to camera first")
        url = f"http://{CAMERA_IP}:{CAMERA_PORT}/thumb/{media_id}/{kind.upper()}"
        resp = await asyncio.to_thread(lambda: _cam_session.get(url, timeout=5))
        if resp.status_code != 200:
            raise HTTPException(404, "Thumbnail not found on camera")
        await asyncio.to_thread(thumb_src.write_bytes, resp.content)
        await asyncio.to_thread(_db.mark_thumb_cached, media_id, kind_lower, str(thumb_src))

    thumb_dst = SAVED_DIR / f"{saved_at_fs}_{media_id}_{kind_lower}_thumb.jpg"
    await asyncio.to_thread(shutil.copy2, str(thumb_src), str(thumb_dst))

    # ── Full file ──
    file_src = FILES_DIR / f"{media_id}_{kind_lower}"
    if not file_src.exists():
        if _state["status"] != "connected":
            raise HTTPException(
                503,
                "Full file not cached — open it at full resolution first, or connect to camera"
            )
        url = f"http://{CAMERA_IP}:{CAMERA_PORT}/file/{media_id}/{kind.upper()}"
        resp = await asyncio.to_thread(
            lambda: _cam_session.get(url, timeout=(2, 120))
        )
        if resp.status_code != 200:
            raise HTTPException(404, "File not found on camera")
        await asyncio.to_thread(file_src.write_bytes, resp.content)
        await asyncio.to_thread(_db.mark_file_cached, media_id, kind_lower, str(file_src))

    file_dst = SAVED_DIR / f"{saved_at_fs}_{media_id}_{kind_lower}"
    await asyncio.to_thread(shutil.copy2, str(file_src), str(file_dst))

    saved_id = await asyncio.to_thread(
        _db.save_media, media_id, kind_lower, saved_at_iso,
        str(thumb_dst), str(file_dst)
    )
    return {"saved_id": saved_id, "saved_at": saved_at_iso}


@app.get("/api/saved")
async def api_saved():
    rows = await asyncio.to_thread(_db.get_saved_media)
    items = []
    for row in rows:
        item = dict(row)
        if item.get("analysis_json"):
            try:
                item["analysis"] = json.loads(item["analysis_json"])
            except Exception:
                item["analysis"] = None
        else:
            item["analysis"] = None
        del item["analysis_json"]
        items.append(item)
    return {"items": items}


@app.get("/api/saved/thumb/{saved_id}")
async def api_saved_thumb(saved_id: int):
    item = await asyncio.to_thread(_db.get_saved_by_id, saved_id)
    if not item or not item["thumb_path"]:
        raise HTTPException(404)
    path = Path(item["thumb_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/saved/file/{saved_id}")
async def api_saved_file(saved_id: int):
    item = await asyncio.to_thread(_db.get_saved_by_id, saved_id)
    if not item or not item["file_path"]:
        raise HTTPException(404)
    path = Path(item["file_path"])
    if not path.exists():
        raise HTTPException(404)
    mime = "video/mp4" if item["kind"] == "mp4" else "image/jpeg"
    return FileResponse(str(path), media_type=mime)


@app.delete("/api/saved/{saved_id}")
async def api_saved_delete(saved_id: int):
    row = await asyncio.to_thread(_db.delete_saved, saved_id)
    if not row:
        raise HTTPException(404)
    for key in ("thumb_path", "file_path"):
        if row.get(key):
            Path(row[key]).unlink(missing_ok=True)
    return {"success": True}


@app.get("/api/settings")
async def api_settings():
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
    base = f"http://{CAMERA_IP}:{CAMERA_PORT}"
    out = {}
    for key, path in (("settings", "/cmd/getSetting"),
                      ("params", "/cmd/getParaSetting")):
        try:
            r = await asyncio.to_thread(
                lambda p=path: _cam_session.get(f"{base}{p}", timeout=5)
            )
            out[key] = r.json()
        except Exception as exc:
            out[key] = {"error": str(exc)}
    return out


@app.post("/api/settings/format")
async def api_format(body: dict):
    if body.get("confirm") != "CONFIRM":
        raise HTTPException(400, 'Send {"confirm": "CONFIRM"} to proceed')
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/cmd/format/start"
    try:
        r = await asyncio.to_thread(lambda: _cam_session.get(url, timeout=30))
        return r.json()
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/stream/info")
async def api_stream_info():
    return {
        "rtsp_url": _state.get("rtsp_url"),
        "hls_available": _state["hls_available"],
        "hls_active": _hls_proc is not None and _hls_proc.returncode is None,
    }


@app.post("/api/stream/hls/start")
async def api_hls_start():
    global _hls_proc
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
    if not _state["hls_available"]:
        raise HTTPException(501, "ffmpeg not installed — run: sudo apt-get install ffmpeg")
    await _stop_hls()
    # Clear stale segments from any previous session before starting
    if HLS_TMP_DIR.exists():
        shutil.rmtree(HLS_TMP_DIR)
    HLS_TMP_DIR.mkdir(parents=True)
    _hls_proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-i", f"rtsp://{CAMERA_IP}:554/live.sdp",
        "-c:v", "copy",
        "-f", "hls",
        "-hls_time", "1",
        "-hls_list_size", "3",
        "-hls_flags", "delete_segments+split_by_time",
        str(HLS_TMP_DIR / "live.m3u8"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Wait for first segments to appear (1-second segments appear quickly)
    for _ in range(15):
        await asyncio.sleep(1)
        if (HLS_TMP_DIR / "live.m3u8").exists():
            break
    return {"status": "started"}


@app.post("/api/stream/hls/stop")
async def api_hls_stop():
    await _stop_hls()
    return {"status": "stopped"}


@app.get("/api/stream/hls/{filename}")
async def api_hls_file(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = HLS_TMP_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    mime = ("application/vnd.apple.mpegurl"
            if filename.endswith(".m3u8") else "video/MP2T")
    return FileResponse(str(path), media_type=mime,
                        headers={"Cache-Control": "no-cache"})


# Static files and SPA fallback
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    return FileResponse(str(STATIC_DIR / "index.html"))
