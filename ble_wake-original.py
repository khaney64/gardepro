"""
Wake the GardePro E6P WiFi hotspot via BLE AT command, then auto-connect.

Device:  GardePro E6P / CAM8Z8-series BLE device
Module:  Shenzhen RF-star RF_BM_BG22A1A2 (EFR32BG22)

Wake command confirmed from BLE snoop log:
  Write  AT+WAKEPULSE=10\\r\\n  to char 6e400004 (unknown NUS char)
  Camera replies: OK\\r\\n
  WiFi SSID: CAM8Z8_<MAC-no-colons>

Usage:
    python ble_wake.py                          # wake, detect hotspot, print instructions
    python ble_wake.py -p <password>            # wake + auto-connect to hotspot
    python ble_wake.py -p <pw> --no-reconnect   # stay on camera WiFi when done
    python ble_wake.py -p <pw> -r <home-ssid>   # explicit home network to return to
    python ble_wake.py --probe                  # also run AT WiFi config queries
"""
import argparse
import asyncio
import os
import re
import subprocess
import tempfile
import time
import requests
from bleak import BleakClient, BleakScanner

CAMERA_ADDRESS  = None
NAME_HINTS      = ["gardepro", "cam8z8", "e6p", "noname"]
HOTSPOT_POLL_SECS = 30
WIFI_CONNECT_TIMEOUT = 20
CAMERA_IPS      = ["192.168.1.1", "192.168.1.8"]

AT_CHAR  = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
WAKE_CMD = b"AT+WAKEPULSE=10\r\n"

PROBE_CMDS = [
    b"AT+WIFIPASS?\r\n",
    b"AT+WIFIPWD?\r\n",
    b"AT+APPASSWORD?\r\n",
    b"AT+WIFICFG?\r\n",
    b"AT+WIFI?\r\n",
    b"AT?\r\n",
]

WIFI_PROFILE_WPA2 = """\
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>WPA2PSK</authentication>
      <encryption>AES</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
    <sharedKey>
      <keyType>passPhrase</keyType>
      <protected>false</protected>
      <keyMaterial>{password}</keyMaterial>
    </sharedKey>
  </security></MSM>
</WLANProfile>
"""

WIFI_PROFILE_OPEN = """\
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>open</authentication>
      <encryption>none</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
  </security></MSM>
</WLANProfile>
"""

# ── netsh helpers ─────────────────────────────────────────────────────────────

def netsh(*args, timeout=10):
    try:
        return subprocess.check_output(
            ["netsh"] + list(args),
            text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except Exception:
        return ""


def get_wifi_interface():
    """Return the name of the first active WLAN interface (e.g. 'Wi-Fi')."""
    out = netsh("wlan", "show", "interfaces")
    for line in out.splitlines():
        m = re.match(r"^\s+Name\s*:\s*(.+)$", line)
        if m:
            return m.group(1).strip()
    return "Wi-Fi"


def get_active_ssid():
    """Return the SSID the adapter is currently connected to, or None."""
    out = netsh("wlan", "show", "interfaces")
    for line in out.splitlines():
        m = re.match(r"^\s+(?:Profile|SSID)\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if val and val != "Profile":
                return val
    return None


def scan_wifi():
    out = netsh("wlan", "show", "networks", "mode=bssid")
    networks = {}
    current_ssid = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^SSID\s+\d+\s*:\s*(.+)$", line)
        if m:
            current_ssid = m.group(1).strip()
        m2 = re.match(r"^BSSID\s+\d+\s*:\s*([\da-fA-F:]{17})$", line)
        if m2 and current_ssid:
            networks[current_ssid] = m2.group(1)
    return networks


def wait_for_ip(interface, timeout=20):
    """Wait until the interface has a non-APIPA IP. Returns the IP or None."""
    print(f"  Waiting for DHCP on {interface} ...")
    for i in range(timeout):
        time.sleep(1)
        out = netsh("interface", "ip", "show", "addresses", interface)
        for line in out.splitlines():
            m = re.search(r"IP Address:\s+([\d.]+)", line)
            if m:
                ip = m.group(1)
                if not ip.startswith("169.254"):
                    print(f"  IP assigned: {ip} (after {i+1}s)")
                    return ip
    return None


def connect_wifi(ssid, password, interface):
    """Create a temporary profile and connect. Returns True on success."""
    xml = (WIFI_PROFILE_OPEN.format(ssid=ssid) if password == ""
           else WIFI_PROFILE_WPA2.format(ssid=ssid, password=password))
    tmp = os.path.join(tempfile.gettempdir(), "cam_wifi_profile.xml")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(xml)
        netsh("wlan", "add", "profile", f"filename={tmp}", "user=current")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    print(f"  Connecting to {ssid} on {interface} ...")
    netsh("wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={interface}")

    for i in range(WIFI_CONNECT_TIMEOUT):
        time.sleep(1)
        out = netsh("wlan", "show", "interfaces")
        connected = ("State" in out and "connected" in out and
                     f"SSID" in out)
        # confirm we're on the right SSID
        for line in out.splitlines():
            if re.match(r"^\s+SSID\s*:\s*" + re.escape(ssid), line):
                print(f"  Connected to {ssid} after {i+1}s.")
                return True
        if i % 5 == 4:
            print(f"  ...{i+1}s waiting for connection")
    return False


def reconnect_wifi(ssid, interface):
    print(f"  Reconnecting to {ssid} ...")
    netsh("wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={interface}")


def probe_http_api():
    """Try the known camera HTTP API endpoints and print results."""
    for ip in CAMERA_IPS:
        url = f"http://{ip}/Storage?GetDirFileInfo"
        print(f"  GET {url}")
        try:
            resp = requests.get(url, timeout=5)
            print(f"  HTTP {resp.status_code}  ({len(resp.content)} bytes)")
            print(f"  Body: {resp.text[:500]!r}")
            if resp.ok:
                return ip
        except requests.exceptions.ConnectionError as e:
            print(f"  ConnectionError: {e}")
        except requests.exceptions.Timeout:
            print(f"  Timeout after 5s")
        except Exception as e:
            print(f"  Error: {e}")
    return None


# ── WiFi scan display ─────────────────────────────────────────────────────────

def print_networks(networks, label):
    if not networks:
        print(f"  {label}: (none visible)")
        return
    print(f"  {label}:")
    for ssid, bssid in sorted(networks.items()):
        print(f"    {ssid:<40} {bssid}")


# ── BLE ───────────────────────────────────────────────────────────────────────

def expected_ssid(address):
    return "CAM8Z8_" + address.replace(":", "").upper()


async def find_device(address):
    if address:
        return address
    print("Scanning for camera...")
    results = await BleakScanner.discover(timeout=10, return_adv=True)
    matches = [d for d, _ in results.values()
               if d.name and any(h in d.name.lower() for h in NAME_HINTS)]
    if not matches:
        print("Camera not found. Pass --address explicitly.")
        return None
    print(f"Found: {matches[0].name!r}  [{matches[0].address}]")
    return matches[0].address


async def wake(address, password, home_ssid, no_reconnect, probe):
    responses = []
    ssid = expected_ssid(address)
    interface = get_wifi_interface()

    def on_notify(char, data):
        text = data.decode(errors="replace")
        print(f"  << [{char.uuid[:8]}] {data.hex()}  {text!r}")
        responses.append(data)

    # Baseline
    print(f"WiFi interface : {interface}")
    print(f"Home network   : {home_ssid or '(none detected)'}")
    print(f"Camera hotspot : {ssid}\n")

    wifi_before = scan_wifi()
    hotspot_already_up = ssid in wifi_before

    print(f"Connecting to BLE {address} ...")
    async with BleakClient(address) as client:
        print("BLE connected.\n")
        await client.start_notify(AT_CHAR, on_notify)

        if probe:
            print("-- AT probe --")
            for cmd in PROBE_CMDS:
                print(f"  >> {cmd!r}")
                try:
                    await client.write_gatt_char(AT_CHAR, cmd, response=True)
                    await asyncio.sleep(0.4)
                except Exception as e:
                    print(f"     (error: {e})")
            print()

        for i in range(3):
            print(f"Sending WAKEPULSE ({i+1}/3) ...")
            await client.write_gatt_char(AT_CHAR, WAKE_CMD, response=True)
            await asyncio.sleep(0.4)

        ok = any(b"OK" in r for r in responses)
        print(f"\n{'[OK]' if ok else '[?]'} Camera {'acknowledged' if ok else 'no response'}. "
              f"Waiting up to {HOTSPOT_POLL_SECS}s for hotspot ...")

        found = hotspot_already_up
        for i in range(HOTSPOT_POLL_SECS):
            await asyncio.sleep(1)
            after = scan_wifi()
            if ssid in after:
                if not found:
                    print(f"\n*** Hotspot appeared after {i+1}s: {ssid}  [{after[ssid]}] ***")
                found = True
                break
            if i % 5 == 4:
                print(f"  ...{i+1}s — not yet visible")

        if not found:
            print(f"\nHotspot {ssid!r} not detected after {HOTSPOT_POLL_SECS}s.")
            print_networks(scan_wifi(), "Visible networks")
            return

    # BLE session ends here — camera WiFi is up, no need to hold BLE open
    if not password:
        print(f"\nHotspot is up. Re-run with -p <password> to auto-connect:")
        print(f"  python ble_wake.py -p <password>")
        return

    print(f"\nConnecting laptop to {ssid} ...")
    if not connect_wifi(ssid, password, interface):
        print(f"  Failed to connect to {ssid}. Check password and try again.")
        return

    ip = wait_for_ip(interface)
    if not ip:
        print("  DHCP timed out — no IP assigned. Check password or try --password \"\"")
        return

    print("\nProbing HTTP API ...")
    camera_ip = probe_http_api()
    if camera_ip:
        print(f"\nCamera IP confirmed: {camera_ip}")
        print("You can now use the HTTP API:")
        print(f"  curl \"http://{camera_ip}/SetMode?Storage\"")
        print(f"  curl \"http://{camera_ip}/Storage?GetFilePage=0&type=Photo\"")
        print(f"  curl \"http://{camera_ip}/Storage?Download=<fid>\" -o photo.jpg")

    if not no_reconnect and home_ssid:
        print()
        reconnect_wifi(home_ssid, interface)
    elif not no_reconnect:
        print("\n(No home network to reconnect to — staying on camera WiFi)")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="GardePro E6P BLE wake + WiFi connect")
    parser.add_argument("--address", default=CAMERA_ADDRESS,
                        help="BLE address; if omitted, scan by camera name hints")
    parser.add_argument("-p", "--password", default=None,
                        help="Camera WiFi password; use \"\" for open/no-password network")
    parser.add_argument("-r", "--reconnect", default=None,
                        help="Home SSID to reconnect to when done (default: auto-detected)")
    parser.add_argument("--no-reconnect", action="store_true",
                        help="Stay on camera WiFi when done")
    parser.add_argument("--probe", action="store_true",
                        help="Send AT WiFi config queries before wake command")
    args = parser.parse_args()

    address = await find_device(args.address)
    if not address:
        return

    home_ssid = args.reconnect or get_active_ssid()
    await wake(address, args.password, home_ssid, args.no_reconnect, args.probe)


asyncio.run(main())
