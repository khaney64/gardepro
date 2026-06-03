"""
GardePro Camera Web Server

Manages BLE wake, WiFi connection, and proxies the camera HTTP/RTSP APIs
to a browser-accessible web interface.

Run:   cd /home/<user>/gardepro/web
       GARDEPRO_WIFI_PASSWORD=<pw> python3 -m uvicorn server:app --host 0.0.0.0 --port 8080

Environment variables:
  GARDEPRO_WIFI_PASSWORD   Camera WiFi password (required for connect)
  GARDEPRO_BLE_ADDRESS     BLE address to skip scanning, e.g. AA:BB:CC:DD:EE:FF
  GARDEPRO_BLE_ADAPTER     Bluetooth HCI adapter (default: hci0)
  GARDEPRO_WIFI_IFACE      WiFi interface for camera (default: first wlx* found)
  GARDEPRO_RTSP_PORT       Local port for RTSP TCP proxy (default: 8554)
"""

import asyncio
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

# ── Configuration ─────────────────────────────────────────────────────────────

CAMERA_BLE_ADDRESS = os.environ.get("GARDEPRO_BLE_ADDRESS") or None
BLE_ADAPTER        = os.environ.get("GARDEPRO_BLE_ADAPTER", "hci0")
WIFI_IFACE_OVERRIDE = os.environ.get("GARDEPRO_WIFI_IFACE") or None
CAMERA_IP          = "192.168.8.1"
CAMERA_PORT        = 8080
RTSP_PORT_LOCAL    = int(os.environ.get("GARDEPRO_RTSP_PORT", "8554"))
HLS_TMP_DIR        = Path("/tmp/gardepro_hls")
STATIC_DIR         = Path(__file__).parent / "static"

# ── Shared camera session ─────────────────────────────────────────────────────

_cam_session = requests.Session()
_cam_session.trust_env = False

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
}
# Full media list kept separate (too large to include in every SSE broadcast)
_media: list[dict] = []

_sse_queues:        list[asyncio.Queue] = []
_connect_task:      Optional[asyncio.Task] = None
_background_tasks:  list[asyncio.Task] = []
_rtsp_server       = None
_hls_proc          = None
_shutting_down      = False


# ── Helpers ───────────────────────────────────────────────────────────────────

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


async def _set_step(step: str):
    _state["step"] = step
    _state["error"] = None
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
        print(f"[rtsp proxy] failed to start on port {RTSP_PORT_LOCAL}: {exc}")


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
            resp.close()
            if code == 200:
                kind = "mp4" if "video" in ct else "jpg"
                results.append({"id": media_id, "kind": kind})
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
    return results


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
        my_ip, _ = await asyncio.to_thread(linux_wait_for_ip, iface, 40)
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

        _background_tasks.clear()
        _background_tasks.append(asyncio.create_task(_keepalive_loop()))
        _background_tasks.append(asyncio.create_task(_signal_poll_loop(iface)))

    except asyncio.CancelledError:
        _state.update({"status": "disconnected", "step": "", "error": "Cancelled"})
        try:
            await asyncio.to_thread(linux_disconnect_wifi, iface)
        except Exception:
            pass
        await _broadcast(_broadcast_state())
        raise

    except Exception as exc:
        _state.update({"status": "disconnected", "step": "", "error": str(exc)})
        _media = []
        _state["media_count"] = 0
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

    _media = []
    _state.update({
        "status": "disconnected", "step": "", "camera_ip": None,
        "my_ip": None, "signal_dbm": None, "signal_label": None,
        "media_count": 0, "rtsp_url": None, "error": None,
    })
    await _broadcast(_broadcast_state())


# ── Startup / shutdown ────────────────────────────────────────────────────────

async def _resume_session(iface: str, my_ip: str):
    """Called as a background task if Edimax is already on the camera subnet at startup."""
    print(f"[startup] resuming session on {iface} ({my_ip})")
    _state["step"] = "Reconnecting to camera…"
    await _broadcast(_broadcast_state())
    try:
        await _enumerate_media()
        await _start_rtsp_proxy()
        _state.update({"status": "connected", "step": ""})
        await _broadcast(_broadcast_state())
        _background_tasks.append(asyncio.create_task(_keepalive_loop()))
        _background_tasks.append(asyncio.create_task(_signal_poll_loop(iface)))
    except Exception as exc:
        _state.update({"status": "disconnected", "step": "", "error": str(exc)})
        await _broadcast(_broadcast_state())


@asynccontextmanager
async def lifespan(app: FastAPI):
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


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


@app.get("/api/status")
async def api_status():
    return {**_state, "media_count": len(_media)}


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


@app.get("/api/thumb/{media_id}/{kind}")
async def api_thumb(media_id: int, kind: str):
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
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
    if _state["status"] != "connected":
        raise HTTPException(503, "Not connected")
    url = f"http://{CAMERA_IP}:{CAMERA_PORT}/file/{media_id}/{kind.upper()}"
    kind_lower = kind.lower()
    mime = "video/mp4" if kind_lower == "mp4" else "image/jpeg"

    resp = await asyncio.to_thread(
        lambda: _cam_session.get(url, stream=True, timeout=(2, 120))
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code)

    it = resp.iter_content(chunk_size=65536)

    async def body():
        while True:
            chunk = await asyncio.to_thread(lambda: next(it, None))
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(
        body(),
        media_type=mime,
        headers={
            "Content-Disposition": f'inline; filename="cam_{media_id}.{kind_lower}"',
        },
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
            await _broadcast({**_broadcast_state(), "type": "media_deleted",
                              "id": media_id, "kind": kind.lower()})
            return {"success": True}
        raise HTTPException(500, result.get("desc", "Delete failed"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


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
