# GardePro E6P Trail Camera — Programmatic Access Investigation

## Status
BLE wake confirmed working. WiFi hotspot comes up most reliably when camera is idle
and the phone/laptop is not already committed to another WiFi connection.
**WiFi password confirmed:** `<redacted-camera-wifi-password>`. Windows can join the camera hotspot,
receives `192.168.8.30/24`, and sees `192.168.8.1` in ARP, but Windows does not
receive a default gateway. The camera service window is short, so probes must run
immediately after DHCP.

Confirmed local services:
- HTTP API: `http://192.168.8.1:8080`
- RTSP live video: `rtsp://192.168.8.1:554/live.sdp`

APK disassembly confirms the app's local proxy is a raw TCP proxy:
- API/media proxy target: `192.168.8.1:8080`
- RTSP proxy target: `192.168.8.1:554`

Therefore app-local URLs like `http://127.0.0.1:41137/thumb/49/JPG` should map
directly to `http://192.168.8.1:8080/thumb/49/JPG`.

Current blocker: identify any true gallery-listing endpoint. Direct scanning works,
but no count/next header has been found.

Hotspot/session lifetime clues from APK/logcat:
- Device settings include `"standby_timeout":300`, likely 300 seconds.
- App logs `keepCameraAlive start` after WiFi connection succeeds.
- APK strings include `keepCameraAlive`, `sendKeepAlive`, `_startKeepAliveTimer`,
  `/cmd/standby/reset`, and `/cmd/standby/now`.
- App logs `_disconnect_g5a6r7p8r9o - STANDBY_NOW command sent successfully` when
  disconnecting, so `/cmd/standby/now` should be treated as a shutdown/standby command.
- Best keepalive candidate to test is:
  `GET http://192.168.8.1:8080/cmd/standby/reset`

Confirmed media route:
- Reliable scans should use `Connection: close`; reused keep-alive sockets can
  corrupt subsequent responses.
- `GET http://192.168.8.1:8080/thumb/<id>/JPG` returns `HTTP 200`, `image/jpeg`
  for current IDs. `/thumb/<id>/MP4` appears to be an equivalent thumbnail alias.
- Thumbnail headers include `ZP-UID` and `ZP-Date`; no count, next, page, total,
  or range header has been observed.
- `GET http://192.168.8.1:8080/file/<id>/JPG` and `/file/<id>/MP4` both return
  the same underlying media for that ID; use response `Content-Type` to determine
  whether the item is a photo or video.
- On 2026-06-02 with camera set to "2 photos then 1 video", IDs `1..42`
  initially existed with no gaps. IDs divisible by 3 returned `video/mp4`; the
  rest returned `image/jpeg`.
- After deleting items in the app/browser, IDs are stable and sparse. Deleting
  IDs `39`, `40`, and `41` left gaps; ID `42` remained ID `42`. Do not stop a
  scan at the first `HTTP 500`.
- Practical enumeration rule: scan upward from `1`, tolerate gaps, and stop only
  after a conservative run of misses past the highest seen ID.
- `DELETE http://192.168.8.1:8080/file/41/JPG` timed out and did not delete
  anything. Post-delete rescan still showed IDs `1..42`, with UID `bac117bd`
  still at ID `41`.
- Confirmed delete endpoint:
  `GET http://192.168.8.1:8080/cmd/delete/<id>/<JPG|MP4>`.
  Example: `GET /cmd/delete/40/JPG` returned `{"code":0,"desc":"success"}`.
  Retrying returned `{"code":-2,"desc":"file does not exist"}`.
  `GET /cmd/delete/<id>` returns `{"code":-1,"desc":"wrong file para"}`.
- Earlier `GET http://192.168.8.1:8080/file/39/MP4` returned `HTTP 200`,
  `video/mp4`.
  First saved attempt was truncated: MP4 `mdat` expected end at `28,385,906`
  bytes, but file had `8,293,376` bytes. Use longer download timeout and start
  the MP4 route earlier in the fast path.
  Another attempt reached `14,459,904` bytes before the camera reset the HTTP
  connection. RTSP still worked afterward, so this appears to be an HTTP/file
  transfer limit or short API window, not total WiFi failure. The script now
  supports `--resume-samples` via HTTP `Range` if the camera honors it.
- `GET http://192.168.8.1:8080/media/getIrStatus` returns:
  `{"code":0,"data":{"irStatus":"ir","irPower":0}}`.

This working folder was moved to this machine because it has the Bluetooth adapter
and phone connectivity needed for live capture.

---

## How the Camera Connects

1. **Bluetooth (BLE)** — Always-on, low-power. Used to wake the camera's WiFi hotspot
   via AT command on a custom characteristic. The app also runs a multi-level BLE login
   protocol (Levels 0–3) on the standard NUS channels to exchange encrypted device state.
2. **WiFi (2.4GHz)** — Camera creates a local WiFi AP (WPA2). The app connects to this
   for file transfer and camera control. The WiFi password/session is believed to be
   negotiated via BLE (Level 2/3), not hardcoded. Likely plain HTTP once connected.

---

## Confirmed BLE Details

| Field | Value |
|---|---|
| BLE device name | `CAM8Z8_NoName_G_E6P` |
| BLE address | `<redacted-camera-ble-address>` |
| BLE module | Shenzhen RF-star RF_BM_BG22A1A2 (Silicon Labs EFR32BG22) |
| BLE firmware | V0.3.2_2022.09.15 |
| Wake characteristic | `6e400004-b5a3-f393-e0a9-e50e24dcca9e` (non-standard NUS char) |
| Wake command | `AT+WAKEPULSE=10\r\n` |
| Camera response | `OK\r\n` |

### BLE Services on Device

```
Generic Attribute Profile (0x1801)
Generic Access Profile (0x1800)
Device Information (0x180a)
  - Manufacturer: Shenzhen RF-star Technology Co., Ltd.
  - HW revision:  RF_BM_BG22A1A2
  - FW revision:  V0.3.2_2022.09.15
Nordic UART Service (6e400001)
  - 6e400002 [write, write-without-response]  NUS RX  (phone→camera)
  - 6e400003 [notify]                         NUS TX  (camera→phone, binary protocol)
  - 6e400004 [write, notify, write-without-response]  ← WAKE TARGET (AT commands)
Unknown service (1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0)
  - f7bf3564 [write]
  - 984227f3 [write, write-without-response]
```

### BLE Login Protocol (4 Levels)

Discovered from APK string analysis of `libapp.so`:

| Level | Function | Purpose |
|---|---|---|
| 0 | `loginDeviceByBleLevel0` | Wake via AT+WAKEPULSE on `6e400004`. Log: `loginDeviceByBleLevel0. bleMac <addr>` |
| 1 | `_loginDeviceByBleLevel1_g5a6r7p8r9o` | ECDH key exchange on NUS RX/TX. Log: `BLE connect success _loginDeviceByBleLevel1` |
| 2 | `_loginDeviceByBleLevel2_g5a6r7p8r9o` | Authentication using derived key |
| 3 | `_loginDeviceByBleLevel3_g5a6r7p8r9o` | Device settings exchange — camera sends `wifiPassword` here |

**Key findings from APK:**
- Level 1 uses ECDH key exchange. A 64-byte EC public key was found adjacent to
  `loginDeviceByBleLevel0` in the binary.
- Level 2/3 messages are encrypted with **ChaCha20** (`ChaCha20Engine` from pointycastle
  found adjacent to `ble_device_g5a6r7p8r9o.dart`).
- The `wifiPassword` field arrives via `BLE received message:` log path — it is
  **received from the camera**, not hardcoded in the app.
- `pwdStr converted password` log with `dart:convert` nearby suggests the raw bytes
  are decoded to a UTF-8 string (not hashed or further transformed).
- `secret_g5a6r7p8r9o.dart` — a Dart source file literally named "secret", likely
  contains the static ChaCha20 key or ECDH parameters. Two hex blobs found nearby:
  - `004d696e67687561517512d8f03431fce63b88f4` (20 bytes)
  - `036768ae8e18bb92cfcf005c949aa2c6d94853d0e660bbf854b1c9505fe95a` (31 bytes)

### AT Command Interface Notes

- Only `AT+WAKEPULSE=<n>` is implemented. All other AT queries (`AT+WIFIPASS?`,
  `AT+WIFICFG?`, etc.) are silently ignored — no response.
- **Warning:** Writing garbage to `6e400002` (NUS RX) via probe commands put the
  camera BLE firmware in a stuck state that persisted across connections. Power cycle
  recovered it. Do not use `--probe` mode unless debugging BLE protocol.
- The `--probe` flag in `ble_wake.py` now correctly gates NUS TX subscription and
  NUS RX writes behind the flag; normal wake-only runs are unaffected.

---

## Confirmed WiFi Hotspot Details

| Field | Value |
|---|---|
| SSID pattern | `CAM8Z8_<MAC-no-colons>` |
| SSID (this device) | `CAM8Z8_<redacted-device-id>` |
| BSSID | `<redacted-camera-bssid>` |
| Security | WPA2 (+ WPA3 SAE transition mode) |
| Hotspot spin-up time | ~16 seconds after BLE wake command |
| Camera IP (observed) | `192.168.8.1` from ARP scan after DHCP |
| Laptop IP (observed) | `192.168.8.30` |
| Gateway | None assigned by Windows DHCP |

### WiFi Password

`<redacted-camera-wifi-password>` is confirmed working from Windows with
`ble_wake.py -p <password>`.

Earlier manual attempts appeared to fail, likely because the hotspot was not fully
ready, Windows was still associated elsewhere, or the camera accepts clients only
after the BLE wake/app flow settles. The app log also confirms this password.

---

## Backend Infrastructure (from APK)

| Endpoint | Purpose |
|---|---|
| `https://us.api.zopudt.com` | Production API |
| `https://dev.api.zopudt.com` | Dev/staging API |
| `https://vice.dev.zopudt.com` | Additional dev environment |
| `https://www.zopudt.com/p2pserver` | P2P signaling server |
| `s3://res.zopudt.com` (us-east-2) | Firmware, FAQ, privacy docs |
| `https://tc-app-debug.s3-accelerate.amazonaws.com` | Debug media uploads |
| `192.168.5.20:9081` | Hardcoded dev LAN server (internal) |

Known API routes (from dev endpoint paths in binary):
- `/gardepro/quickstart/deviceshare`
- `/gardepro/quickstart/gallery`
- `/gardepro/quickstart/plans`, `/plans2`

App package: `com.zpszjs.gardepro.mobile` / Dart package: `trail_camera_gardepro`

---

## HTTP API (Camera Local — Partially Confirmed)

Camera service is on `192.168.8.1`. Windows receives an IP but no default gateway,
so `ble_wake.py` adds the likely `.1` host without waiting for a gateway.

The service window appears short. The script now runs fast protocol probes
immediately after DHCP, before slower diagnostics or full port scans.

**Confirmed local endpoints:**

```bash
# Live stream
RTSP rtsp://192.168.8.1:554/live.sdp

# Media thumbnails and files
GET http://192.168.8.1:8080/thumb/<id>/JPG
GET http://192.168.8.1:8080/thumb/<id>/MP4
GET http://192.168.8.1:8080/file/<id>/JPG
GET http://192.168.8.1:8080/file/<id>/MP4

# Delete media. Type suffix must match the stored item type.
GET http://192.168.8.1:8080/cmd/delete/<id>/<JPG|MP4>

# Session keepalive / standby controls
GET http://192.168.8.1:8080/cmd/standby/reset
GET http://192.168.8.1:8080/cmd/standby/now

# Status / settings-adjacent commands
GET http://192.168.8.1:8080/media/getIrStatus
GET http://192.168.8.1:8080/media/setDayNightMode

# Camera command/status/settings endpoints
GET http://192.168.8.1:8080/cmd/getSetting
GET http://192.168.8.1:8080/cmd/getParaSetting
GET http://192.168.8.1:8080/cmd/info/
GET http://192.168.8.1:8080/cmd/format/result
GET http://192.168.8.1:8080/cmd/result/
GET http://192.168.8.1:8080/cmd/upgrade/result
```

`/cmd/getSetting` returns the current camera configuration. Confirmed fields include
`photo_or_video`, `photo_quality`, `video_quality`, `video_length`,
`video_length_night`, `frame_rate`, `pir`, `time_zone`, `date_format`,
`time_format`, `date_stamp`, `standby_timeout`, `wifi`, and `version`.

`/cmd/getParaSetting` returns capability/options metadata. Confirmed fields include
`video_len_list`, `night_video_len_list`, `pir_delay_list`, a full `timezone`
list, and other menu/range flags used by the app settings UI.

Status probe results:
- `/cmd/info/` returns `{"code": -1}` on this camera so far.
- `/cmd/format/result` returns `{"code":0,"desc":"success"}` even when no format
  was just started. Treat this as status/idle OK, not proof that formatting ran.
- `/cmd/result/` returns `{"code": -100}`.
- `/cmd/upgrade/result` returns `{"code":0,"desc":"success"}`.

**Unconfirmed or not useful on this device:**

```bash
# Storage mode + file listing candidates; returned HTTP 500 with empty body
GET http://192.168.8.1:8080/SetMode?Storage
GET http://192.168.8.1:8080/Storage?GetDirFileInfo
GET http://192.168.8.1:8080/Storage?GetFilePage=0&type=Photo
GET http://192.168.8.1:8080/Storage?Download=<fid>

# Found in older/other camera clues; not confirmed here
GET http://192.168.8.1:8080/Misc?PowerOff
```

**APK-discovered command routes:**

```bash
GET http://192.168.8.1:8080/cmd/delete/<id>/<JPG|MP4>  # confirmed, mutates media
GET http://192.168.8.1:8080/cmd/format/result          # confirmed status
GET http://192.168.8.1:8080/cmd/format/start           # destructive; not casually probed
GET http://192.168.8.1:8080/cmd/getParaSetting         # confirmed
GET http://192.168.8.1:8080/cmd/getSetting             # confirmed
GET http://192.168.8.1:8080/cmd/info/                  # confirmed but returns code -1
GET http://192.168.8.1:8080/cmd/reboot                 # mutating; not casually probed
GET http://192.168.8.1:8080/cmd/resetFact              # destructive; not probed
GET http://192.168.8.1:8080/cmd/result/                # confirmed status-ish
GET http://192.168.8.1:8080/cmd/setGmtClock            # likely time sync; params unknown
GET http://192.168.8.1:8080/cmd/setSetting             # likely settings write; params unknown
GET http://192.168.8.1:8080/cmd/standby/now            # likely ends/standbys session
GET http://192.168.8.1:8080/cmd/standby/reset          # confirmed keepalive
GET http://192.168.8.1:8080/cmd/upgrade/result         # confirmed status
GET http://192.168.8.1:8080/cmd/upgrade/start          # mutating; not probed
```

The APK exposes `/cmd/setGmtClock` and `/cmd/setSetting`, but string analysis has
not revealed their parameter format. The safest next step is to capture app HTTP
traffic while using "sync time" or changing one harmless setting, then replay the
observed camera-local URL.

The app creates phone-local proxy ports:
- API proxy: `127.0.0.1:41137`
- RTSP proxy: `127.0.0.1:37999`

App gallery logs show local URLs like `http://127.0.0.1:41137/thumb/...` and
`http://127.0.0.1:41137/file/39/MP4`. The next reverse-engineering target is the
proxy implementation in the APK.

APK disassembly result:

```text
WiFiCameraProxy.start():
  new TcpIpProxy("192.168.8.1", 8080, 0, apiPortListener)
  new TcpIpProxy("192.168.8.1", 554, 0, rtspPortListener)

Connection.run():
  new Socket(remoteIp, remotePort)
  new Proxy(clientSocket, serverConnection)
  new Proxy(serverConnection, clientSocket)
```

This means the proxy forwards bytes unchanged. Known app-local gallery routes from
logcat should be valid raw camera routes on port 8080:

```bash
GET http://192.168.8.1:8080/thumb/49/JPG
GET http://192.168.8.1:8080/thumb/48/MP4
GET http://192.168.8.1:8080/thumb/47/JPG
GET http://192.168.8.1:8080/thumb/46/JPG
GET http://192.168.8.1:8080/thumb/45/MP4
GET http://192.168.8.1:8080/thumb/44/JPG
GET http://192.168.8.1:8080/thumb/43/JPG
GET http://192.168.8.1:8080/file/39/MP4
```

---

## Scripts

All scripts in the repository root (works on Windows and Linux):

| Script | Purpose |
|---|---|
| `ble_scan.py` | Scan for nearby BLE devices — find camera by name |
| `ble_wake.py` | Full pipeline: BLE wake → detect hotspot → auto WiFi connect → HTTP probe |
| `ble_wake-original.py` | Original working version from start of session (reference) |
| `parse_btsnoop.py` | Parse Android btsnoop HCI log for BLE ATT writes |

### ble_wake.py usage

`--wifi-interface <iface>` selects the adapter used for camera WiFi. On Linux it
defaults to the first `wlx*` USB adapter found; on Windows it is auto-detected and
this flag is not needed.

**Windows:**
```
python ble_wake.py                             # wake + wait for hotspot
python ble_wake.py --wake-only                 # wake, exit once hotspot visible
python ble_wake.py --wake-only --wait 90       # same, wait up to 90s
python ble_wake.py -p <password>               # wake + auto-connect + HTTP probe
python ble_wake.py -p <password> --no-reconnect --wait 90
python ble_wake.py -p ""                       # explicitly test open/no-password WiFi
python ble_wake.py -p <pw> --no-reconnect      # stay on camera WiFi when done
python ble_wake.py -p <pw> -r <home-ssid>      # explicit home network to return to
python ble_wake.py --probe                     # AT probe (debug only — may confuse camera)
python ble_wake.py -p <password> --skip-http-probe  # port scan only
python ble_wake.py --list-ble-adapters         # show Bluetooth adapter status
python ble_wake.py -p <password> --no-reconnect --wait 90 --fast-only --save-samples --resume-samples --max-download-mb 250 --download-timeout 180
python ble_wake.py -p <password> --no-reconnect --wait 90 --fast-only --save-samples --resume-samples --keepalive-interval 30 --max-download-mb 250 --download-timeout 180
python ble_wake.py -p <password> --no-reconnect --wait 90 --hold-session --keepalive-interval 30
python ble_wake.py -p <password> --no-reconnect --wait 90 --cmd-probe-only --keepalive-interval 30
python ble_wake.py -p <password> --no-reconnect --wait 90 --cmd-probe --fast-only
```

**Linux / Raspberry Pi** (Edimax adapter for camera; `wlan0` stays on home network):
```bash
python3 ble_wake.py --address <camera-ble-address> --ble-adapter hci0 \
  --wifi-interface <usb-wifi-iface> -p <password>          # wake + connect + fast probe

python3 ble_wake.py --address <camera-ble-address> --ble-adapter hci0 \
  --wifi-interface <usb-wifi-iface> -p <pw> --wake-only    # wake only, no connect

python3 ble_wake.py --address <camera-ble-address> --ble-adapter hci0 \
  --wifi-interface <usb-wifi-iface> -p <pw> \
  --hold-session --keepalive-interval 60                  # keep session open

python3 ble_wake.py --list-ble-adapters                   # show hci adapters
```

After the script has connected to the camera WiFi session, normal exit or Ctrl-C
stops the keepalive thread and sends `GET /cmd/standby/now`. On Windows it then
reconnects to the home network; on Linux it tears down the camera interface while
`wlan0` remains connected to the home network throughout.

### Dependencies

**Windows:**
```
pip install bleak requests
```
Python: `C:/Python312/python.exe`

**Raspberry Pi / Debian / Ubuntu:**
```bash
sudo apt-get install -y python3-bleak
# requests is pre-installed on Ubuntu-based Pi images; if not:
# sudo apt-get install -y python3-requests
```
Python: `python3`

### Pi / Linux setup

On a Raspberry Pi (or other Linux host), the script manages the camera WiFi on a
**dedicated USB adapter** while the built-in WiFi stays on the home network throughout.

Tested configuration (Raspberry Pi):
- `wlan0` — built-in adapter, stays on home network (managed by netplan); never touched
- `<usb-wifi-iface>` — Edimax USB adapter, used only for the camera hotspot
- `hci0` — built-in Bluetooth (UART), used for BLE wake

The `--wifi-interface` argument selects the camera adapter. On Linux it defaults to
the first `wlx*` interface found via `ip link show`.

Required tools (available by default on Raspberry Pi OS): `wpa_supplicant`, `dhcpcd`, `iw`.
The script calls these with `sudo`; configure `NOPASSWD` in sudoers or enter the password
when prompted.

**Browsing the camera from a laptop while the Pi holds the session:**

```bash
# SSH port forward — open specific ports, no browser config needed:
ssh -L 18080:192.168.8.1:8080 -L 18554:192.168.8.1:554 -N user@<pi-ip>
# Then open: http://localhost:18080/cmd/getSetting
#      RTSP: rtsp://localhost:18554/live.sdp

# SSH SOCKS5 proxy — full access to the 192.168.8.x subnet:
ssh -D 1080 -N user@<pi-ip>
# Configure browser SOCKS5: localhost:1080
# Then browse: http://192.168.8.1:8080/cmd/getSetting
```

---

## BLE Snoop Log Analysis

### Original capture
Captured via Android HCI snoop log (`btsnoop_hci.log`), parsed with `parse_btsnoop.py`.

Key findings:
- Camera uses a **binary protocol** over NUS RX/TX (`6e400002`/`6e400003`) for the
  main data channel — this is the Level 1–3 protocol, ChaCha20-encrypted.
- Camera uses `6e400004` for AT commands — this is the Level 0 / wake channel.
- A second unknown service (`1d14d6ee`) handles device config/info exchanges.
- WiFi password is **not in the AT command stream** — it arrives as a field in the
  binary Level 2/3 response on `6e400003` (NUS TX).
- The original analysis concluded "password not in BLE" because it only examined
  AT command traffic; the encrypted binary channel was not decoded.

Current parser status:
- `parse_btsnoop.py` is now present in this folder.
- It tracks ATT handle maps per HCI ACL connection handle.
- It reassembles fragmented L2CAP packets before decoding ATT writes/notifies.
- It can filter by UUID, ATT handle, connection handle, direction, peer address,
  or free text, and can export matching events to JSONL.
- Existing `btsnoop_hci.log` confirms three writes of `AT+WAKEPULSE=10\r\n` to
  `6e400004` and three `OK\r\n` notifications from the camera.

Useful parser commands:
```
python parse_btsnoop.py btsnoop_hci.log
python parse_btsnoop.py btsnoop_hci.log --uuid 6e4000
python parse_btsnoop.py btsnoop_hci.log --uuid 6e4000 --jsonl gardepro_frames.jsonl
python parse_btsnoop.py btsnoop_hci.log --direction CAM->APP
```

### Next capture needed
A fresh snoop log captured during a successful GardePro app WiFi connection session
is needed to obtain the raw Level 2/3 frames containing `wifiPassword`.

---

## APK Analysis

Pulled from device:
```
adb shell pm path com.zpszjs.gardepro.mobile
adb pull /data/app/.../base.apk gardepro.apk
```

Key files in APK:
- `lib/arm64-v8a/libapp.so` — compiled Dart code (AOT), 18MB, all app logic here
- `lib/arm64-v8a/libflutter.so` — Flutter engine
- `assets/flutter_assets/` — images, HTML, localization strings

String analysis of `libapp.so` confirmed:
- 4-level BLE login protocol
- `wifiPassword` received from camera over BLE (not hardcoded)
- ChaCha20 encryption on BLE binary channel
- ECDH key exchange at Level 1
- `secret_g5a6r7p8r9o.dart` contains static key material
- `/media/setDayNightMode` HTTP endpoint (previously unknown)

---

## Reference

- Dsoon H8WIFI investigation (similar architecture, different BLE protocol):
  https://geekitguide.com/wifi-ble-ble-trailcam-investigation-part-1/
