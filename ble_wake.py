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

Linux-specific options:
    python ble_wake.py --wifi-interface wlx74da388d4fd6 -p <pw>   # use Edimax for camera
    python ble_wake.py --ble-adapter hci0 --wifi-interface wlx74da388d4fd6 -p <pw>
"""
import argparse
import asyncio
import json
import os
import platform
import re
import socket
import subprocess
import tempfile
import threading
import time
import builtins
from pathlib import Path
import requests
from bleak import BleakClient, BleakScanner

CAMERA_ADDRESS  = None
NAME_HINTS      = ["gardepro", "cam8z8", "e6p", "noname"]
HOTSPOT_POLL_SECS_DEFAULT = 60
WIFI_CONNECT_TIMEOUT = 20
TCP_PROBE_PORTS_DEFAULT = [
    21, 23, 80, 81, 82, 88, 443, 554, 5000, 7000, 8000, 8001,
    8080, 8081, 8088, 8181, 8221, 8554, 8899, 9000, 9080, 9081,
    10000,
]
HTTP_PROBE_PORTS_DEFAULT = [
    80, 81, 82, 88, 8000, 8001, 8080, 8081, 8088, 8181, 8221, 8899,
    9000, 9080, 9081,
]
RTSP_PATHS_DEFAULT = [
    "/live.sdp",
    "",
    "/",
    "/live",
    "/stream",
    "/stream1",
    "/stream0",
    "/video",
    "/videoMain",
    "/h264",
    "/h264.sdp",
    "/11",
    "/12",
    "/1",
    "/0",
    "/ch0_0.264",
    "/live/ch00_0",
    "/Streaming/Channels/101",
    "/h264/ch1/main/av_stream",
]
CAMERA_HTTP_PATHS = [
    "/",
    "/SetMode?Storage",
    "/Storage?GetDirFileInfo",
    "/Storage?GetFilePage=0&type=Photo",
    "/Storage?GetFilePage=0&type=Video",
    "/Storage?GetFilePage=0&type=ALL",
    "/Storage?Download=1",
    "/media/setDayNightMode",
    "/cgi-bin/hi3510/param.cgi?cmd=getserverinfo",
]
FAST_HTTP_PORTS = [8080]
FAST_HTTP_PATHS = [
    "/",
    "/thumb/49/JPG",
    "/file/39/MP4",
    "/thumb/48/MP4",
    "/thumb/47/JPG",
    "/thumb/46/JPG",
    "/thumb/45/MP4",
    "/thumb/44/JPG",
    "/thumb/43/JPG",
    "/media/setDayNightMode",
    "/media/getIrStatus",
    "/Storage?GetDirFileInfo",
    "/Storage?GetFilePage=0&type=Photo",
]
FAST_RTSP_PATHS = ["/live.sdp", "", "/"]
READ_ONLY_CMD_PATHS = [
    "/cmd/getSetting",
    "/cmd/getParaSetting",
    "/cmd/info/",
    "/cmd/format/result",
    "/cmd/result/",
    "/cmd/upgrade/result",
]

AT_CHAR  = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX   = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # camera→phone notify channel
NUS_RX   = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # phone→camera write channel
WAKE_CMD = b"AT+WAKEPULSE=10\r\n"

PROBE_CMDS = [
    # standard AT help / list
    b"AT\r\n",
    b"AT?\r\n",
    b"AT+?\r\n",
    # WiFi AP credentials
    b"AT+WIFIPASS?\r\n",
    b"AT+WIFIPWD?\r\n",
    b"AT+APPASSWORD?\r\n",
    b"AT+APKEY?\r\n",
    b"AT+APPASS?\r\n",
    b"AT+WIFICFG?\r\n",
    b"AT+WIFI?\r\n",
    b"AT+WIFIAP?\r\n",
    # SSID / network name
    b"AT+SSID?\r\n",
    b"AT+APSSID?\r\n",
    # broad status
    b"AT+STATUS?\r\n",
    b"AT+INFO?\r\n",
    b"AT+VERSION?\r\n",
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

# ── platform helpers ──────────────────────────────────────────────────────────

def is_linux():
    return platform.system().lower() == "linux"


def is_windows():
    return platform.system().lower() == "windows"


def command_output(args, timeout=10):
    try:
        return subprocess.check_output(
            args, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except Exception as exc:
        return f"(failed to run {' '.join(str(a) for a in args)}: {exc})"


def bluez_args(adapter):
    if adapter and is_linux():
        return {"adapter": adapter}
    return {}


def print_ble_adapter_warning(adapter):
    if adapter and is_windows():
        print(
            f"Bluetooth adapter requested: {adapter}; Windows/Bleak uses the "
            "OS-selected radio and cannot select a specific adapter."
        )
    elif adapter and is_linux():
        print(f"Bluetooth adapter : {adapter}")
    elif is_windows():
        print("Bluetooth adapter : Windows OS-selected radio")
    else:
        print("Bluetooth adapter : Bleak default")


def list_ble_adapters():
    if is_windows():
        print("Windows Bluetooth adapters/devices:")
        out = command_output(["pnputil", "/enum-devices", "/class", "Bluetooth"], timeout=20)
        blocks = [b.strip() for b in re.split(r"\n\s*\n", out) if b.strip()]
        shown = 0
        for block in blocks:
            instance = description = status = problem = None
            for line in block.splitlines():
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key == "instance id":
                    instance = value
                elif key == "device description":
                    description = value
                elif key == "status":
                    status = value
                elif key == "problem code":
                    problem = value
            text = f"{instance or ''} {description or ''}".lower()
            is_physical = (
                (instance or "").upper().startswith("USB\\")
                or "adapter" in text
                or "microsoft bluetooth enumerator" in text
                or "microsoft bluetooth le enumerator" in text
            )
            if not is_physical:
                continue
            suffix = f"; problem={problem}" if problem else ""
            print(f"  {status or '?':<12} {description or '(unknown)'}{suffix}")
            print(f"               {instance or '(unknown instance)'}")
            shown += 1
        if not shown:
            print(out.strip() or "(none reported)")
        print("\nNote: Bleak on Windows uses the OS-selected Bluetooth radio.")
        return

    if is_linux():
        print("Linux Bluetooth adapters:")
        out = command_output(["bluetoothctl", "list"], timeout=10)
        print(out.strip() or "(none reported by bluetoothctl)")
        return

    print(f"Bluetooth adapter listing is not implemented for {platform.system()}.")


def parse_ports(value):
    ports = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            port = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid port: {item!r}") from exc
        if port < 1 or port > 65535:
            raise argparse.ArgumentTypeError(f"port out of range: {port}")
        if port not in ports:
            ports.append(port)
    if not ports:
        raise argparse.ArgumentTypeError("at least one port is required")
    return ports


def parse_csv(value):
    items = []
    for item in value.split(","):
        item = item.strip()
        if item and item not in items:
            items.append(item)
    if not items:
        raise argparse.ArgumentTypeError("at least one value is required")
    return items


# ── Windows-only netsh helpers ────────────────────────────────────────────────

def netsh(*args, timeout=10):
    try:
        return subprocess.check_output(
            ["netsh"] + list(args),
            text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except Exception:
        return ""


def _windows_get_wifi_interface():
    """Return the name of the first active WLAN interface (e.g. 'Wi-Fi')."""
    out = netsh("wlan", "show", "interfaces")
    for line in out.splitlines():
        m = re.match(r"^\s+Name\s*:\s*(.+)$", line)
        if m:
            return m.group(1).strip()
    return "Wi-Fi"


def _windows_get_active_ssid():
    """Return the SSID the adapter is currently connected to, or None."""
    out = netsh("wlan", "show", "interfaces")
    for line in out.splitlines():
        m = re.match(r"^\s+(?:Profile|SSID)\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if val and val != "Profile":
                return val
    return None


def _windows_scan_wifi():
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


def _windows_wait_for_ip(interface, timeout=20):
    """Wait until the interface has a non-APIPA IP. Returns (my_ip, gateway) or (None, None)."""
    print(f"  Waiting for DHCP on {interface} ...")
    for i in range(timeout):
        time.sleep(1)
        out = netsh("interface", "ip", "show", "config", f"name={interface}")
        my_ip = gateway = None
        for line in out.splitlines():
            m = re.search(r"IP Address:\s+([\d.]+)", line)
            if m and not m.group(1).startswith("169.254"):
                my_ip = m.group(1)
            m2 = re.search(r"Default Gateway:\s+([\d.]+)", line)
            if m2 and not m2.group(1).startswith("0.0.0.0"):
                gateway = m2.group(1)
        if my_ip:
            print(f"  My IP  : {my_ip} (after {i+1}s)")
            print(f"  Gateway: {gateway or '(none yet)'}")
            return my_ip, gateway
    return None, None


def _windows_arp_scan_subnet(my_ip):
    """Ping-sweep the /24 subnet and return IPs that reply (from ARP table)."""
    prefix = ".".join(my_ip.split(".")[:3])
    prefix_dot = prefix + "."
    print(f"  ARP scan of {prefix}.0/24 ...")
    try:
        subprocess.run(
            f"for /L %i in (1,1,254) do @ping -n 1 -w 200 {prefix}.%i >nul",
            shell=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        pass
    found = []
    try:
        out = subprocess.check_output(["arp", "-a"], text=True,
                                      encoding="utf-8", errors="replace")
        for line in out.splitlines():
            m = re.match(r"\s+([\d.]+)\s+[\da-f-]{17}\s+dynamic", line, re.I)
            if m and m.group(1).startswith(prefix_dot):
                found.append(m.group(1))
    except Exception:
        pass
    return found


def _windows_arp_table_hosts(my_ip):
    """Return already-known ARP table entries on the /24 subnet without ping sweep."""
    prefix_dot = ".".join(my_ip.split(".")[:3]) + "."
    found = []
    try:
        out = subprocess.check_output(["arp", "-a"], text=True,
                                      encoding="utf-8", errors="replace")
        for line in out.splitlines():
            m = re.match(r"\s+([\d.]+)\s+[\da-f-]{17}\s+dynamic", line, re.I)
            if m and m.group(1).startswith(prefix_dot):
                found.append(m.group(1))
    except Exception:
        pass
    return found


def _windows_connect_wifi(ssid, password, interface):
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
        for line in out.splitlines():
            if re.match(r"^\s+SSID\s*:\s*" + re.escape(ssid), line):
                print(f"  Connected to {ssid} after {i+1}s.")
                return True
        if i % 5 == 4:
            print(f"  ...{i+1}s waiting for connection")
    return False


def _windows_reconnect_wifi(ssid, interface):
    print(f"  Reconnecting to {ssid} ...")
    netsh("wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={interface}")


def _windows_print_network_diagnostics(interface, my_ip):
    print("\nNetwork diagnostics:")
    config = netsh("interface", "ip", "show", "config", f"name={interface}")
    if config.strip():
        print("  netsh interface ip show config:")
        for line in config.splitlines():
            if line.strip():
                print(f"    {line}")

    prefix = ".".join(my_ip.split(".")[:3]) + "."
    arp = command_output(["arp", "-a"])
    arp_lines = [line for line in arp.splitlines() if prefix in line]
    if arp_lines:
        print("  ARP entries on camera subnet:")
        for line in arp_lines:
            print(f"    {line}")
    else:
        print("  ARP entries on camera subnet: (none)")

    route = command_output(["route", "print", my_ip])
    interesting = [
        line for line in route.splitlines()
        if prefix in line or "0.0.0.0" in line or "Interface List" in line
    ]
    if interesting:
        print("  Route hints:")
        for line in interesting[:20]:
            print(f"    {line}")


# ── Linux WiFi helpers ────────────────────────────────────────────────────────

def linux_get_wifi_interface(override=None):
    """Return the interface to use for camera WiFi on Linux.

    Prefers an explicitly specified interface, then the first wlx* USB adapter,
    then falls back to wlan1.
    """
    if override:
        return override
    try:
        out = subprocess.check_output(
            ["ip", "link", "show"], text=True, errors="replace", timeout=5
        )
        for line in out.splitlines():
            m = re.match(r"^\d+:\s+(wlx\S+):", line)
            if m:
                return m.group(1).rstrip(":")
    except Exception:
        pass
    return "wlan1"


def linux_get_active_ssid(home_iface="wlan0"):
    """Return the SSID that the home interface is connected to."""
    try:
        out = subprocess.check_output(
            ["iw", "dev", home_iface, "link"],
            text=True, errors="replace", timeout=5
        )
        for line in out.splitlines():
            m = re.search(r"SSID:\s+(.+)", line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def linux_scan_wifi(iface):
    """Scan for nearby WiFi SSIDs on iface. Returns {ssid: bssid} dict.

    Requires iface to be UP; brings it up if needed. Requires sudo for iw scan.
    """
    try:
        subprocess.run(
            ["sudo", "ip", "link", "set", iface, "up"],
            timeout=5, capture_output=True
        )
    except Exception:
        pass

    networks = {}
    try:
        out = subprocess.check_output(
            ["sudo", "iw", "dev", iface, "scan"],
            text=True, errors="replace", timeout=15,
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError as exc:
        # iw scan returns non-zero on some error conditions but still has output
        out = exc.output or ""
    except Exception:
        return networks

    current_bssid = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^BSS\s+([\da-fA-F:]{17})", line)
        if m:
            current_bssid = m.group(1)
        m2 = re.search(r"^SSID:\s+(.+)$", line)
        if m2 and current_bssid:
            ssid = m2.group(1).strip()
            if ssid:
                networks[ssid] = current_bssid
    return networks


def linux_connect_wifi(ssid, password, iface):
    """Connect iface to ssid using wpa_supplicant + dhcpcd. Returns True on success."""
    # Build wpa_supplicant config
    if password == "":
        network_block = 'network={\n    ssid="%s"\n    key_mgmt=NONE\n}\n' % ssid
    else:
        escaped_pw = password.replace("\\", "\\\\").replace('"', '\\"')
        network_block = (
            'network={\n'
            '    ssid="%s"\n'
            '    psk="%s"\n'
            '    key_mgmt=WPA-PSK\n'
            '}\n'
        ) % (ssid, escaped_pw)

    conf_content = (
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n"
        + network_block
    )

    conf_path = "/tmp/cam_wpa.conf"
    try:
        with open(conf_path, "w") as f:
            f.write(conf_content)
    except OSError as exc:
        print(f"  Failed to write wpa_supplicant config: {exc}")
        return False

    # Kill any existing wpa_supplicant on this interface
    _linux_kill_wpa_supplicant(iface)

    # Bring interface up
    try:
        subprocess.run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=True)
    except Exception as exc:
        print(f"  ip link set {iface} up failed: {exc}")
        return False

    print(f"  Starting wpa_supplicant on {iface} for {ssid!r} ...")
    try:
        subprocess.run(
            ["sudo", "wpa_supplicant", "-B", "-i", iface,
             "-c", conf_path, "-D", "nl80211,wext"],
            timeout=10, check=True, capture_output=True
        )
    except Exception as exc:
        print(f"  wpa_supplicant failed: {exc}")
        return False

    # Wait for association
    for i in range(WIFI_CONNECT_TIMEOUT):
        time.sleep(1)
        try:
            out = subprocess.check_output(
                ["iw", "dev", iface, "link"],
                text=True, errors="replace", timeout=5
            )
            if "Connected to" in out or "SSID" in out:
                # Check it's the right SSID
                for line in out.splitlines():
                    m = re.search(r"SSID:\s+(.+)", line)
                    if m and m.group(1).strip() == ssid:
                        print(f"  Connected to {ssid} after {i+1}s.")
                        return True
        except Exception:
            pass
        if i % 5 == 4:
            print(f"  ...{i+1}s waiting for association")
    print(f"  wpa_supplicant timed out after {WIFI_CONNECT_TIMEOUT}s.")
    return False


def linux_wait_for_ip(iface, timeout=20):
    """Wait until iface has a non-APIPA IP. Returns (my_ip, gateway) or (None, None)."""
    print(f"  Requesting DHCP on {iface} (dhcpcd) ...")
    # Kill any prior dhcpcd on this interface before starting a new one.
    try:
        subprocess.run(["sudo", "pkill", "-f", f"dhcpcd.*{iface}"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run(
            ["sudo", "dhcpcd", "-1", "--noarp", iface],
            timeout=timeout + 5, capture_output=True
        )
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        print(f"  dhcpcd error: {exc}")

    for i in range(timeout):
        time.sleep(1)
        try:
            out = subprocess.check_output(
                ["ip", "addr", "show", iface],
                text=True, errors="replace", timeout=5
            )
            for line in out.splitlines():
                m = re.search(r"inet\s+([\d.]+)/\d+", line)
                if m and not m.group(1).startswith("169.254"):
                    my_ip = m.group(1)
                    gateway = _linux_get_gateway(iface)
                    print(f"  My IP  : {my_ip} (after {i+1}s)")
                    print(f"  Gateway: {gateway or '(none yet)'}")
                    return my_ip, gateway
        except Exception:
            pass
    return None, None


def _linux_get_gateway(iface):
    """Return the default gateway for iface, or None."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "dev", iface],
            text=True, errors="replace", timeout=5
        )
        for line in out.splitlines():
            m = re.search(r"default via\s+([\d.]+)", line)
            if m:
                return m.group(1)
            # Also handle "192.168.8.0/24 via 192.168.8.1 ..." style
            m2 = re.search(r"via\s+([\d.]+)", line)
            if m2:
                return m2.group(1)
    except Exception:
        pass
    return None


def linux_arp_table_hosts(my_ip):
    """Return hosts already in the ARP/neighbor table on the /24 subnet."""
    prefix_dot = ".".join(my_ip.split(".")[:3]) + "."
    found = []
    try:
        out = subprocess.check_output(
            ["ip", "neigh", "show"],
            text=True, errors="replace", timeout=5
        )
        for line in out.splitlines():
            m = re.match(r"([\d.]+)\s+", line)
            if m and m.group(1).startswith(prefix_dot) and m.group(1) != my_ip:
                if m.group(1) not in found:
                    found.append(m.group(1))
    except Exception:
        pass
    return found


def linux_arp_scan_subnet(my_ip, iface):
    """Ping-sweep the /24 subnet via iface and return IPs that appear in neigh table."""
    prefix = ".".join(my_ip.split(".")[:3])
    prefix_dot = prefix + "."
    print(f"  ARP scan of {prefix}.0/24 on {iface} ...")
    procs = []
    try:
        for i in range(1, 255):
            p = subprocess.Popen(
                ["ping", "-c", "1", "-W", "1", "-I", iface, f"{prefix}.{i}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            procs.append(p)
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    except Exception:
        pass

    found = []
    try:
        out = subprocess.check_output(
            ["ip", "neigh", "show"],
            text=True, errors="replace", timeout=5
        )
        for line in out.splitlines():
            m = re.match(r"([\d.]+)\s+", line)
            if m and m.group(1).startswith(prefix_dot) and m.group(1) != my_ip:
                if m.group(1) not in found:
                    found.append(m.group(1))
    except Exception:
        pass
    return found


def _linux_kill_wpa_supplicant(iface):
    """Kill any wpa_supplicant running on iface."""
    try:
        pid_file = f"/run/wpa_supplicant/{iface}.pid"
        if os.path.exists(pid_file):
            subprocess.run(["sudo", "wpa_cli", "-i", iface, "terminate"],
                           timeout=5, capture_output=True)
            time.sleep(0.5)
        # Also try killing by process search as fallback
        subprocess.run(
            ["sudo", "pkill", "-f", f"wpa_supplicant.*{iface}"],
            timeout=5, capture_output=True
        )
        time.sleep(0.3)
    except Exception:
        pass


def linux_disconnect_wifi(iface):
    """Tear down the camera WiFi connection on iface (wlan0 is unaffected)."""
    print(f"  Disconnecting {iface} from camera network ...")
    _linux_kill_wpa_supplicant(iface)
    # Kill any orphaned dhcpcd on this interface. When subprocess.run(timeout=...)
    # fires, it SIGKILLs sudo but dhcpcd (a grandchild) survives as an orphan.
    try:
        subprocess.run(["sudo", "pkill", "-f", f"dhcpcd.*{iface}"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run(["sudo", "ip", "addr", "flush", "dev", iface],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run(["sudo", "ip", "link", "set", iface, "down"],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def linux_print_network_diagnostics(iface, my_ip):
    print("\nNetwork diagnostics:")
    for cmd, label in [
        (["ip", "addr", "show", iface], f"ip addr show {iface}"),
        (["ip", "route", "show", "dev", iface], f"ip route show dev {iface}"),
        (["ip", "neigh", "show"], "ip neigh show"),
    ]:
        out = command_output(cmd)
        if out.strip() and not out.startswith("(failed"):
            print(f"  {label}:")
            prefix = ".".join(my_ip.split(".")[:3]) + "."
            for line in out.splitlines():
                if line.strip() and (prefix in line or "addr" in cmd or "route" in cmd):
                    print(f"    {line}")


# ── Platform-dispatching WiFi API ─────────────────────────────────────────────

def get_wifi_interface(override=None):
    if is_linux():
        return linux_get_wifi_interface(override)
    return _windows_get_wifi_interface()


def get_active_ssid(home_iface="wlan0"):
    if is_linux():
        return linux_get_active_ssid(home_iface)
    return _windows_get_active_ssid()


def scan_wifi(iface=None):
    if is_linux():
        return linux_scan_wifi(iface or "wlan0")
    return _windows_scan_wifi()


def connect_wifi(ssid, password, interface):
    if is_linux():
        return linux_connect_wifi(ssid, password, interface)
    return _windows_connect_wifi(ssid, password, interface)


def wait_for_ip(interface, timeout=20):
    if is_linux():
        return linux_wait_for_ip(interface, timeout)
    return _windows_wait_for_ip(interface, timeout)


def arp_table_hosts(my_ip, iface=None):
    if is_linux():
        return linux_arp_table_hosts(my_ip)
    return _windows_arp_table_hosts(my_ip)


def arp_scan_subnet(my_ip, iface=None):
    if is_linux():
        return linux_arp_scan_subnet(my_ip, iface or "wlan0")
    return _windows_arp_scan_subnet(my_ip)


def reconnect_wifi(ssid, interface):
    if is_linux():
        linux_disconnect_wifi(interface)
    else:
        _windows_reconnect_wifi(ssid, interface)


def print_network_diagnostics(interface, my_ip):
    if is_linux():
        linux_print_network_diagnostics(interface, my_ip)
    else:
        _windows_print_network_diagnostics(interface, my_ip)


# ── TCP / HTTP / RTSP probes ──────────────────────────────────────────────────

def tcp_port_scan(candidate_ips, ports, timeout):
    print("\nTCP port scan:")
    results = {}
    for ip in candidate_ips:
        open_ports = []
        refused = 0
        timed_out = 0
        for port in ports:
            try:
                with socket.create_connection((ip, port), timeout=timeout):
                    open_ports.append(port)
            except ConnectionRefusedError:
                refused += 1
            except socket.timeout:
                timed_out += 1
            except OSError:
                timed_out += 1
        results[ip] = open_ports
        open_text = ", ".join(str(p) for p in open_ports) if open_ports else "(none)"
        print(
            f"  {ip}: open={open_text}; "
            f"refused={refused}; no-response={timed_out}"
        )
    return results


def probe_http_api(candidate_ips, http_ports, open_ports_by_ip, timeout):
    """Try HTTP API paths against each candidate IP. Returns (ip, port) or (None, None)."""
    session = requests.Session()
    session.trust_env = False  # never send camera-local requests through a proxy

    for ip in candidate_ips:
        if open_ports_by_ip is None:
            ports = http_ports
        else:
            open_ports = set(open_ports_by_ip.get(ip, []))
            ports = [p for p in http_ports if p in open_ports]
            if not ports:
                print(f"  {ip}: no HTTP candidate ports open")
                continue
        for path in CAMERA_HTTP_PATHS:
            for port in ports:
                port_part = "" if port == 80 else f":{port}"
                url = f"http://{ip}{port_part}{path}"
                print(f"  GET {url}")
                try:
                    with session.get(url, timeout=timeout, stream=True) as resp:
                        content_type = resp.headers.get("content-type", "(none)")
                        chunk = b""
                        for chunk in resp.iter_content(chunk_size=256):
                            break
                        body = chunk.decode(errors="replace")
                        print(
                            f"    HTTP {resp.status_code}; "
                            f"type={content_type}; first={body[:200]!r}"
                        )
                        if resp.status_code < 500:
                            return ip, port
                except requests.exceptions.ConnectionError:
                    print("    No response")
                except requests.exceptions.Timeout:
                    print("    Timeout")
                except Exception as e:
                    print(f"    Error: {e}")
    return None, None


def probe_command_paths(camera_ip, paths, timeout):
    """Run read-only command/status probes against the camera HTTP API."""
    session = requests.Session()
    session.trust_env = False
    base_url = f"http://{camera_ip}:8080"
    print("\nRead-only command probes:")
    for path in paths:
        path = path if path.startswith("/") else "/" + path
        url = base_url + path
        print(f"  GET {url}")
        try:
            resp = session.get(
                url,
                timeout=timeout,
                headers={"Connection": "close"},
            )
        except requests.exceptions.ConnectionError:
            print("    No response")
            continue
        except requests.exceptions.Timeout:
            print("    Timeout")
            continue
        except Exception as exc:
            print(f"    Error: {exc}")
            continue

        content_type = resp.headers.get("content-type", "(none)")
        print(
            f"    HTTP {resp.status_code}; "
            f"type={content_type}; length={len(resp.content)}"
        )
        try:
            parsed = resp.json()
        except ValueError:
            body = resp.text[:500].replace("\r", "")
            print(f"    body={body!r}")
            continue
        print(json.dumps(parsed, indent=2, sort_keys=True))


def start_keepalive(camera_ip, interval_secs):
    """Periodically reset the camera standby timer while WiFi is connected."""
    if not interval_secs or interval_secs <= 0:
        return None, None

    stop_event = threading.Event()

    def worker():
        session = requests.Session()
        session.trust_env = False
        url = f"http://{camera_ip}:8080/cmd/standby/reset"
        while not stop_event.is_set():
            try:
                resp = session.get(url, timeout=(0.8, 2.0))
                body = resp.text[:120].replace("\r", "").replace("\n", "\\n")
                print(f"  Keepalive {url}: HTTP {resp.status_code}; first={body!r}")
            except requests.exceptions.RequestException as exc:
                if not stop_event.is_set():
                    print(f"  Keepalive {url}: {exc}")
            if stop_event.wait(interval_secs):
                break

    thread = threading.Thread(target=worker, name="camera-keepalive", daemon=True)
    print(
        f"  Starting keepalive every {interval_secs:g}s: "
        f"http://{camera_ip}:8080/cmd/standby/reset"
    )
    thread.start()
    return stop_event, thread


def stop_keepalive(stop_event, thread):
    if not stop_event:
        return
    stop_event.set()
    if thread:
        thread.join(timeout=3)


def send_standby_now(camera_ip):
    """Ask the camera to leave the active WiFi/API session."""
    session = requests.Session()
    session.trust_env = False
    url = f"http://{camera_ip}:8080/cmd/standby/now"
    print(f"  Sending standby: {url}")
    try:
        resp = session.get(
            url,
            timeout=(0.8, 2.0),
            headers={"Connection": "close"},
        )
        body = resp.text[:120].replace("\r", "").replace("\n", "\\n")
        print(f"  Standby response: HTTP {resp.status_code}; first={body!r}")
        return True
    except requests.exceptions.RequestException as exc:
        print(f"  Standby request failed: {exc}")
        return False


def finish_camera_session(
    camera_ip,
    keepalive_stop,
    keepalive_thread,
    no_reconnect,
    home_ssid,
    interface,
    send_standby=True,
):
    stop_keepalive(keepalive_stop, keepalive_thread)
    if send_standby:
        send_standby_now(camera_ip)
    if not no_reconnect:
        if is_linux():
            # wlan0 was never touched; just tear down the camera interface
            linux_disconnect_wifi(interface)
        elif home_ssid:
            print()
            reconnect_wifi(home_ssid, interface)
        else:
            print("\n(No home network to reconnect to — staying on camera WiFi)")
    else:
        print(f"\n(--no-reconnect: leaving {interface} as-is)")


# ── WiFi scan display ─────────────────────────────────────────────────────────

def print_networks(networks, label):
    if not networks:
        print(f"  {label}: (none visible)")
        return
    print(f"  {label}:")
    for ssid, bssid in sorted(networks.items()):
        print(f"    {ssid:<40} {bssid}")


# ── Media download helpers ────────────────────────────────────────────────────

def extension_for_response(path, content_type):
    lower_type = (content_type or "").lower()
    suffix = Path(path).suffix.lower().lstrip(".")
    if "jpeg" in lower_type or "jpg" in lower_type:
        return "jpg"
    if "mp4" in lower_type:
        return "mp4"
    if suffix:
        return suffix
    return "bin"


def sample_kind(content_type):
    lower_type = (content_type or "").lower()
    if "jpeg" in lower_type or "jpg" in lower_type:
        return "jpg"
    if "mp4" in lower_type:
        return "mp4"
    return None


def sample_kind_from_path(url_path):
    last = url_path.rstrip("/").split("/")[-1].lower()
    if last in ("jpg", "jpeg"):
        return "jpg"
    if last == "mp4":
        return "mp4"
    return None


def sample_output_path(url_path, ext, save_dir):
    stem = url_path.strip("/").replace("/", "_").replace("?", "_").replace("&", "_")
    if not stem:
        stem = "root"
    return save_dir / f"{stem}.{ext}"


def save_response_sample(
    resp,
    first_chunk,
    chunk_iter,
    url_path,
    save_dir,
    max_bytes,
    append=False,
    initial_bytes=0,
):
    ext = extension_for_response(url_path, resp.headers.get("content-type", ""))
    out_path = sample_output_path(url_path, ext, save_dir)

    total = initial_bytes if append else 0
    complete = True
    try:
        with out_path.open("ab" if append else "wb") as fh:
            if first_chunk:
                fh.write(first_chunk)
                total += len(first_chunk)
            for chunk in chunk_iter:
                if not chunk:
                    continue
                if total + len(chunk) > max_bytes:
                    keep = max_bytes - total
                    if keep > 0:
                        fh.write(chunk[:keep])
                        total += keep
                    print(f"    Saved partial sample: {out_path} ({total} bytes; hit limit)")
                    return out_path, total, False
                fh.write(chunk)
                total += len(chunk)
    except requests.exceptions.RequestException as exc:
        complete = False
        print(f"    Download interrupted for {out_path}: {exc}")
    except OSError as exc:
        complete = False
        print(f"    Save failed for {out_path}: {exc}")

    status = validate_saved_sample(out_path, ext)
    sample_type = "sample" if complete else "partial sample"
    print(f"    Saved {sample_type}: {out_path} ({total} bytes; {status})")
    return out_path, total, complete


def validate_saved_sample(path, ext):
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"validation failed: {exc}"
    if ext == "jpg":
        if len(data) >= 4 and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
            return "jpeg complete"
        return "jpeg may be incomplete"
    if ext == "mp4":
        top_boxes = parse_mp4_top_boxes(data)
        incomplete = [
            (typ, start, size)
            for typ, start, size in top_boxes
            if size and start + size > len(data)
        ]
        if incomplete:
            typ, start, size = incomplete[0]
            return (
                f"mp4 incomplete: {typ} expects end at {start + size} "
                f"bytes, file has {len(data)}"
            )
        if b"ftyp" in data[:32] and b"mdat" in data:
            return "mp4 complete enough: has ftyp+mdat"
        if b"ftyp" in data[:32]:
            return "mp4 missing mdat"
        return "mp4 header not recognized"
    return "saved"


def parse_mp4_top_boxes(data):
    boxes = []
    pos = 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8].decode("latin1", errors="replace")
        header = 8
        if size == 1:
            if pos + 16 > len(data):
                boxes.append((typ, pos, None))
                break
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = len(data) - pos
        if size < header:
            boxes.append((typ, pos, None))
            break
        boxes.append((typ, pos, size))
        pos += size
        if pos > len(data):
            break
    return boxes


def probe_http_paths(
    candidate_ips,
    http_ports,
    paths,
    timeout,
    stop_on_success=True,
    save_samples=False,
    save_dir=None,
    max_download_bytes=100 * 1024 * 1024,
    resume_samples=False,
):
    session = requests.Session()
    session.trust_env = False
    first_match = (None, None)
    saved_kinds = set()
    if save_samples and save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
    for ip in candidate_ips:
        for port in http_ports:
            for path in paths:
                port_part = "" if port == 80 else f":{port}"
                url = f"http://{ip}{port_part}{path}"
                print(f"  GET {url}")
                headers = {}
                path_kind = sample_kind_from_path(path)
                resume_from = 0
                resume_path = None
                if (
                    save_samples and resume_samples and save_dir and path_kind
                    and path_kind not in saved_kinds
                ):
                    resume_path = sample_output_path(path, path_kind, save_dir)
                    if resume_path.exists():
                        resume_from = resume_path.stat().st_size
                    if resume_from > 0:
                        headers["Range"] = f"bytes={resume_from}-"
                        print(f"    Resuming {resume_path} from byte {resume_from}")
                try:
                    with session.get(url, timeout=timeout, stream=True, headers=headers) as resp:
                        content_type = resp.headers.get("content-type", "(none)")
                        content_length = resp.headers.get("content-length", "(unknown)")
                        append = bool(headers) and resp.status_code == 206
                        if headers and not append:
                            print("    Range not honored; restarting local file")
                        chunk = b""
                        chunk_iter = resp.iter_content(chunk_size=64 * 1024)
                        for chunk in chunk_iter:
                            break
                        body = chunk.decode(errors="replace")
                        print(
                            f"    HTTP {resp.status_code}; "
                            f"type={content_type}; length={content_length}; "
                            f"first={body[:200]!r}"
                        )
                        if resp.status_code < 500:
                            if first_match == (None, None):
                                first_match = (ip, port)
                            kind = sample_kind(content_type) or path_kind
                            if save_samples and kind and kind not in saved_kinds:
                                save_response_sample(
                                    resp,
                                    chunk,
                                    chunk_iter,
                                    path,
                                    save_dir,
                                    max_download_bytes,
                                    append=append,
                                    initial_bytes=resume_from if append else 0,
                                )
                                saved_kinds.add(kind)
                                if {"jpg", "mp4"}.issubset(saved_kinds):
                                    return first_match
                            if stop_on_success:
                                return first_match
                except requests.exceptions.ConnectionError:
                    print("    No response")
                except requests.exceptions.Timeout:
                    print("    Timeout")
                except Exception as e:
                    print(f"    Error: {e}")
    return first_match


def rtsp_exchange(ip, path, method, cseq, timeout):
    target = f"rtsp://{ip}:554{path}"
    headers = [
        f"{method} {target} RTSP/1.0",
        f"CSeq: {cseq}",
        "User-Agent: gardepro-probe",
    ]
    if method == "DESCRIBE":
        headers.append("Accept: application/sdp")
    request = "\r\n".join(headers) + "\r\n\r\n"

    with socket.create_connection((ip, 554), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) >= 4096:
                break
        return b"".join(chunks).decode(errors="replace")


def probe_rtsp(candidate_ips, open_ports_by_ip, paths, timeout):
    print("\nProbing RTSP ...")
    confirmed = []
    for ip in candidate_ips:
        if open_ports_by_ip is not None and 554 not in open_ports_by_ip.get(ip, []):
            print(f"  {ip}: port 554 not open")
            continue

        try:
            response = rtsp_exchange(ip, "", "OPTIONS", 1, timeout)
        except Exception as exc:
            print(f"  OPTIONS rtsp://{ip}:554 failed: {exc}")
            continue

        first = response[:500].replace("\r", "")
        print(f"  OPTIONS rtsp://{ip}:554")
        print(f"    {first!r}")
        if "RTSP/" in response:
            confirmed.append((ip, 554, "OPTIONS"))

        cseq = 2
        for path in paths:
            display_path = path or "/"
            try:
                response = rtsp_exchange(ip, path, "DESCRIBE", cseq, timeout)
            except Exception as exc:
                print(f"  DESCRIBE rtsp://{ip}:554{display_path} failed: {exc}")
                cseq += 1
                continue

            first = response[:500].replace("\r", "")
            status_line = first.splitlines()[0] if first.splitlines() else "(no response)"
            print(f"  DESCRIBE rtsp://{ip}:554{display_path}")
            print(f"    {status_line!r}")
            lower = response.lower()
            if "application/sdp" in lower or "m=video" in lower:
                print(f"    SDP: {first!r}")
                confirmed.append((ip, 554, display_path))
                break
            cseq += 1
    return confirmed


# ── BLE ───────────────────────────────────────────────────────────────────────

def expected_ssid(address):
    address = getattr(address, "address", address)
    return "CAM8Z8_" + address.replace(":", "").upper()


async def find_device(address, ble_adapter):
    if address:
        return address
    print("Scanning for camera...")
    scanner_kwargs = {}
    adapter_args = bluez_args(ble_adapter)
    if adapter_args:
        scanner_kwargs["bluez"] = adapter_args
    results = await BleakScanner.discover(
        timeout=10, return_adv=True, **scanner_kwargs
    )
    matches = [d for d, _ in results.values()
               if d.name and any(h in d.name.lower() for h in NAME_HINTS)]
    if not matches:
        print("Camera not found. Pass --address explicitly.")
        return None
    print(f"Found: {matches[0].name!r}  [{matches[0].address}]")
    return matches[0]


async def wake(
    address,
    password,
    home_ssid,
    no_reconnect,
    probe,
    wait_secs,
    wake_only,
    ports,
    http_ports,
    tcp_timeout,
    http_timeout,
    skip_port_scan,
    skip_http_probe,
    skip_rtsp_probe,
    rtsp_paths,
    do_arp_scan,
    fast_only,
    save_samples,
    save_dir,
    max_download_mb,
    download_timeout,
    resume_samples,
    keepalive_interval,
    hold_session,
    cmd_probe,
    cmd_probe_paths,
    cmd_probe_only,
    ble_adapter,
    wifi_interface,
):
    responses = []
    ssid = expected_ssid(address)
    interface = wifi_interface  # already resolved in main()

    def on_notify(char, data):
        text = data.decode(errors="replace")
        print(f"  << [{char.uuid[:8]}] {data.hex()}  {text!r}")
        responses.append(data)

    # Baseline
    print_ble_adapter_warning(ble_adapter)
    print(f"WiFi interface : {interface}")
    if is_linux():
        print(f"Home network   : {home_ssid or '(wlan0 not connected)'} (wlan0 — unaffected)")
    else:
        print(f"Home network   : {home_ssid or '(none detected)'}")
    print(f"Camera hotspot : {ssid}\n")

    wifi_before = scan_wifi(interface)
    hotspot_already_up = ssid in wifi_before

    ble_label = getattr(address, "address", address)
    client_kwargs = {}
    adapter_args = bluez_args(ble_adapter)
    if adapter_args:
        client_kwargs["bluez"] = adapter_args

    print(f"Connecting to BLE {ble_label} ...")
    async with BleakClient(address, **client_kwargs) as client:
        print("BLE connected.\n")

        # Subscribe to AT_CHAR only — this is the confirmed wake channel.
        # NUS_TX (6e400003) is the binary app protocol channel; subscribing to
        # it signals a full session to the camera and interferes with the wake flow.
        await client.start_notify(AT_CHAR, on_notify)
        if probe:
            # Also listen on NUS_TX so we catch any probe responses there.
            try:
                await client.start_notify(NUS_TX, on_notify)
            except Exception:
                pass
            print("\n-- AT probe (sending to AT_CHAR, NUS_RX, and raw write) --")
            for cmd in PROBE_CMDS:
                for target in [AT_CHAR, NUS_RX]:
                    print(f"  >> [{target[:8]}] {cmd!r}")
                    try:
                        await client.write_gatt_char(target, cmd, response=False)
                    except Exception as e:
                        print(f"     (error on {target[:8]}: {e})")
                await asyncio.sleep(1.0)  # longer wait — camera may be slow
            print()

        for i in range(3):
            print(f"Sending WAKEPULSE ({i+1}/3) ...")
            await client.write_gatt_char(AT_CHAR, WAKE_CMD, response=True)
            await asyncio.sleep(0.4)

        ok = any(b"OK" in r for r in responses)
        print(f"\n{'[OK]' if ok else '[?]'} Camera {'acknowledged' if ok else 'no response'}. "
              f"Waiting up to {wait_secs}s for hotspot ...")

        found = hotspot_already_up
        for i in range(wait_secs):
            await asyncio.sleep(1)
            after = scan_wifi(interface)
            if ssid in after:
                if not found:
                    print(f"\n*** Hotspot appeared after {i+1}s: {ssid}  [{after[ssid]}] ***")
                found = True
                break
            if i % 5 == 4:
                print(f"  ...{i+1}s — not yet visible")

        if not found:
            print(f"\nHotspot {ssid!r} not detected after {wait_secs}s.")
            print_networks(scan_wifi(interface), "Visible networks")
            return

    # BLE session ends here — camera WiFi is up, no need to hold BLE open
    if wake_only or password is None:
        print(f"\nHotspot is up: {ssid}")
        print(f"Connect to it now, then re-run with -p <password> to auto-probe.")
        if is_linux():
            print(f"  sudo ip link set {interface} up")
            print(f"  sudo wpa_supplicant -B -i {interface} -c /tmp/cam_wpa.conf -D nl80211")
            print(f"  # (create /tmp/cam_wpa.conf with SSID={ssid})")
        return

    print(f"\nConnecting {interface} to {ssid} ...")
    if not connect_wifi(ssid, password, interface):
        print(f"  Failed to connect to {ssid}. Check password and try again.")
        return

    my_ip, gateway = wait_for_ip(interface)
    if not my_ip:
        print("  DHCP timed out — no IP assigned. Check password or try --password \"\"")
        return

    # Build candidate list immediately. The camera service may only stay reachable
    # briefly, so do not spend the first window on a full ARP sweep.
    candidates = []
    if gateway:
        candidates.append(gateway)
    if my_ip:
        likely_gateway = ".".join(my_ip.split(".")[:3]) + ".1"
        if likely_gateway != my_ip and likely_gateway not in candidates:
            candidates.append(likely_gateway)
    arp_hosts = (arp_scan_subnet(my_ip, interface) if do_arp_scan
                 else arp_table_hosts(my_ip, interface))
    for h in arp_hosts:
        if h not in candidates and h != my_ip:
            candidates.append(h)
    if not candidates:
        print("  No hosts found on subnet — cannot probe HTTP API")
        return
    print(f"  Candidates: {candidates}")

    keepalive_stop = keepalive_thread = None
    if keepalive_interval > 0:
        keepalive_stop, keepalive_thread = start_keepalive(
            candidates[0], keepalive_interval
        )

    cleanup_done = False
    try:
        if cmd_probe or cmd_probe_only:
            probe_command_paths(candidates[0], cmd_probe_paths, http_timeout)

        if cmd_probe_only:
            print("\nCommand-probe-only mode complete.")
            finish_camera_session(
                candidates[0],
                keepalive_stop,
                keepalive_thread,
                no_reconnect,
                home_ssid,
                interface,
            )
            cleanup_done = True
            return

        if hold_session:
            base_url = f"http://{candidates[0]}:8080"
            print("\nManual session mode is active.")
            print(f"Camera HTTP base : {base_url}")
            print(f"Camera RTSP live : rtsp://{candidates[0]}:554/live.sdp")
            if is_linux():
                print(
                    f"\nTo browse from your laptop via SSH port forward:\n"
                    f"  ssh -L 18080:{candidates[0]}:8080"
                    f" -L 18554:{candidates[0]}:554 -N khaney@192.168.86.73\n"
                    f"  Then open: http://localhost:18080/cmd/getSetting\n"
                    f"\nOr SOCKS5 proxy for full subnet access:\n"
                    f"  ssh -D 1080 -N khaney@192.168.86.73\n"
                    f"  Configure browser SOCKS5: localhost:1080\n"
                    f"  Then open: http://{candidates[0]}:8080/cmd/getSetting"
                )
            if keepalive_interval > 0:
                print("\nKeepalive is running. Press Ctrl-C to stop.")
            else:
                print("\nNo keepalive interval set. Press Ctrl-C to stop.")
            while True:
                await asyncio.sleep(1)

        fast_tcp_timeout = min(tcp_timeout, 0.4)
        if save_samples:
            fast_http_timeout = (min(http_timeout, 0.8), download_timeout)
        else:
            fast_http_timeout = min(http_timeout, 0.8)

        rtsp_hits = []
        if skip_rtsp_probe:
            print("\nFast RTSP probe skipped.")
        else:
            print("\nFast RTSP probe immediately after DHCP ...")
            rtsp_hits = probe_rtsp(candidates, None, FAST_RTSP_PATHS, fast_tcp_timeout)

        camera_ip = camera_port = None
        if skip_http_probe:
            print("\nFast HTTP probe skipped.")
        else:
            print("\nFast HTTP probe immediately after DHCP ...")
            camera_ip, camera_port = probe_http_paths(
                candidates,
                FAST_HTTP_PORTS,
                FAST_HTTP_PATHS,
                fast_http_timeout,
                stop_on_success=False,
                save_samples=save_samples,
                save_dir=save_dir,
                max_download_bytes=max_download_mb * 1024 * 1024,
                resume_samples=resume_samples,
            )

        if fast_only:
            print("\nFast-only mode complete.")
            if rtsp_hits:
                print("RTSP endpoint candidates:")
                for ip, port, path in rtsp_hits:
                    if path == "OPTIONS":
                        print(f"  rtsp://{ip}:{port}/ responds to OPTIONS")
                    else:
                        print(f"  rtsp://{ip}:{port}{path}")
            if camera_ip:
                port_part = "" if camera_port == 80 else f":{camera_port}"
                print(f"HTTP endpoint candidate: http://{camera_ip}{port_part}")
            finish_camera_session(
                candidates[0],
                keepalive_stop,
                keepalive_thread,
                no_reconnect,
                home_ssid,
                interface,
            )
            cleanup_done = True
            return

        print_network_diagnostics(interface, my_ip)

        if skip_port_scan:
            open_ports_by_ip = None
            print("\nTCP port scan skipped.")
        else:
            open_ports_by_ip = tcp_port_scan(candidates, ports, tcp_timeout)

        if camera_ip:
            print("\nSkipping slower HTTP probe because fast HTTP already matched.")
        elif skip_http_probe:
            print("\nHTTP probe skipped.")
        else:
            print("\nProbing HTTP API ...")
            camera_ip, camera_port = probe_http_api(
                candidates, http_ports, open_ports_by_ip, http_timeout
            )
        if camera_ip:
            port_part = "" if camera_port == 80 else f":{camera_port}"
            base_url = f"http://{camera_ip}{port_part}"
            print(f"\nCamera HTTP endpoint confirmed: {base_url}")
            print("You can now use the HTTP API:")
            print(f"  curl \"{base_url}/SetMode?Storage\"")
            print(f"  curl \"{base_url}/Storage?GetFilePage=0&type=Photo\"")
            print(f"  curl \"{base_url}/Storage?Download=<fid>\" -o photo.jpg")
        else:
            print("\nNo HTTP endpoint confirmed.")
            print("Keep the full output; the TCP scan result is the next clue.")

        if rtsp_hits:
            print("\nSkipping slower RTSP probe because fast RTSP already matched.")
        elif skip_rtsp_probe:
            print("\nRTSP probe skipped.")
        else:
            rtsp_hits = probe_rtsp(candidates, open_ports_by_ip, rtsp_paths, tcp_timeout)
            if rtsp_hits:
                print("\nRTSP endpoint candidates:")
                for ip, port, path in rtsp_hits:
                    if path == "OPTIONS":
                        print(f"  rtsp://{ip}:{port}/ responds to OPTIONS")
                    else:
                        print(f"  rtsp://{ip}:{port}{path}")
            else:
                print("\nNo RTSP endpoint confirmed.")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nCtrl-C received; ending camera session.")
    finally:
        if not cleanup_done:
            finish_camera_session(
                candidates[0],
                keepalive_stop,
                keepalive_thread,
                no_reconnect,
                home_ssid,
                interface,
            )


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="GardePro E6P BLE wake + WiFi connect")
    parser.add_argument("--address", default=CAMERA_ADDRESS,
                        help="BLE address; if omitted, scan by camera name hints")
    parser.add_argument("--ble-adapter",
                        help="Bluetooth adapter to use on Linux/BlueZ, e.g. hci0 or hci1")
    parser.add_argument("--list-ble-adapters", action="store_true",
                        help="List visible Bluetooth adapters/devices and exit")
    parser.add_argument("--wifi-interface",
                        help="Network interface for camera WiFi "
                             "(Linux default: first wlx* adapter; Windows: auto-detected)")
    parser.add_argument("-p", "--password", default=None,
                        help="Camera WiFi password; use \"\" for open/no-password network")
    parser.add_argument("-r", "--reconnect", default=None,
                        help="Home SSID to reconnect to when done (default: auto-detected; "
                             "Linux: informational only, wlan0 is never disconnected)")
    parser.add_argument("--no-reconnect", action="store_true",
                        help="Stay on camera WiFi when done (Linux: leave camera iface UP)")
    parser.add_argument("--probe", action="store_true",
                        help="Send AT WiFi config queries before wake command")
    parser.add_argument("--wait", type=int, default=HOTSPOT_POLL_SECS_DEFAULT,
                        metavar="SECS",
                        help=f"Seconds to wait for hotspot (default: {HOTSPOT_POLL_SECS_DEFAULT})")
    parser.add_argument("--wake-only", action="store_true",
                        help="Wake hotspot and exit once visible; do not connect or probe")
    parser.add_argument("--ports", type=parse_ports,
                        default=TCP_PROBE_PORTS_DEFAULT,
                        help="Comma-separated TCP ports to scan after WiFi connects")
    parser.add_argument("--http-ports", type=parse_ports,
                        default=HTTP_PROBE_PORTS_DEFAULT,
                        help="Comma-separated ports to try as HTTP after WiFi connects")
    parser.add_argument("--tcp-timeout", type=float, default=0.4,
                        help="Seconds to wait for each TCP connect attempt")
    parser.add_argument("--http-timeout", type=float, default=1.0,
                        help="Seconds to wait for each HTTP request")
    parser.add_argument("--arp-scan", action="store_true",
                        help="Run slower ping/ARP sweep before full probes")
    parser.add_argument("--fast-only", action="store_true",
                        help="Run only immediate post-DHCP RTSP/HTTP probes")
    parser.add_argument("--save-samples", action="store_true",
                        help="Save first JPEG and first MP4 found during fast HTTP probes")
    parser.add_argument("--save-dir", type=Path, default=Path("captures"),
                        help="Directory for --save-samples output")
    parser.add_argument("--max-download-mb", type=int, default=100,
                        help="Maximum bytes per saved sample, in MB")
    parser.add_argument("--download-timeout", type=float, default=120.0,
                        help="Read timeout in seconds while saving media samples")
    parser.add_argument("--resume-samples", action="store_true",
                        help="Resume existing sample files with HTTP Range when supported")
    parser.add_argument("--keepalive-interval", type=float, default=0.0,
                        metavar="SECS",
                        help="Periodically GET /cmd/standby/reset on port 8080; 0 disables")
    parser.add_argument("--hold-session", action="store_true",
                        help="Connect to camera WiFi and keep the session open until Ctrl-C "
                             "(prints SSH tunnel commands on Linux)")
    parser.add_argument("--cmd-probe", action="store_true",
                        help="Run read-only /cmd status/settings probes after DHCP")
    parser.add_argument("--cmd-probe-only", action="store_true",
                        help="Run read-only /cmd probes after DHCP, then exit")
    parser.add_argument("--cmd-probe-paths", type=parse_csv,
                        default=READ_ONLY_CMD_PATHS,
                        help="Comma-separated /cmd paths for --cmd-probe")
    parser.add_argument("--skip-port-scan", action="store_true",
                        help="Skip TCP scan and directly try HTTP probes")
    parser.add_argument("--skip-http-probe", action="store_true",
                        help="Skip HTTP probes after the TCP scan")
    parser.add_argument("--skip-rtsp-probe", action="store_true",
                        help="Skip RTSP probes on port 554")
    parser.add_argument("--rtsp-paths", type=parse_csv,
                        default=RTSP_PATHS_DEFAULT,
                        help="Comma-separated RTSP paths to DESCRIBE on port 554")
    parser.add_argument("--log-file",
                        help="Also write console output to this file as the script runs")
    args = parser.parse_args()

    log_handle = None
    if args.log_file:
        log_handle = open(args.log_file, "w", encoding="utf-8", buffering=1)
        original_print = builtins.print

        def tee_print(*values, **kwargs):
            original_print(*values, **kwargs)
            kwargs_for_file = dict(kwargs)
            kwargs_for_file["file"] = log_handle
            kwargs_for_file.setdefault("flush", True)
            original_print(*values, **kwargs_for_file)

        builtins.print = tee_print

    try:
        if args.list_ble_adapters:
            list_ble_adapters()
            return

        address = await find_device(args.address, args.ble_adapter)
        if not address:
            return

        wifi_iface = get_wifi_interface(args.wifi_interface)
        home_ssid = args.reconnect or get_active_ssid()
        await wake(
            address,
            args.password,
            home_ssid,
            args.no_reconnect,
            args.probe,
            args.wait,
            args.wake_only,
            args.ports,
            args.http_ports,
            args.tcp_timeout,
            args.http_timeout,
            args.skip_port_scan,
            args.skip_http_probe,
            args.skip_rtsp_probe,
            args.rtsp_paths,
            args.arp_scan,
            args.fast_only,
            args.save_samples,
            args.save_dir,
            args.max_download_mb,
            args.download_timeout,
            args.resume_samples,
            args.keepalive_interval,
            args.hold_session,
            args.cmd_probe,
            args.cmd_probe_paths,
            args.cmd_probe_only,
            args.ble_adapter,
            wifi_iface,
        )
    finally:
        if log_handle:
            log_handle.close()


if __name__ == "__main__":
    asyncio.run(main())
