"""
Parse a btsnoop HCI log and extract BLE ATT writes and notifications.

The parser keeps GATT handle maps per ACL connection handle and reassembles
fragmented L2CAP packets before decoding ATT. That matters for the GardePro
traffic because the app protocol uses long NUS notifications.

Usage:
    python parse_btsnoop.py btsnoop_hci.log
    python parse_btsnoop.py btsnoop_hci.log --uuid 6e4000
    python parse_btsnoop.py btsnoop_hci.log --jsonl gardepro_frames.jsonl
"""
import argparse
import io
import json
import struct
import sys
from datetime import datetime, timezone

# Force UTF-8 output so non-ASCII bytes in repr do not crash on Windows cp1252.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_UNIX_EPOCH_DELTA_US = 62_168_256_000_000_000

HCI_ACL = 0x02
HCI_EVT = 0x04

HCI_EVT_DISCONN_COMPLETE = 0x05
HCI_EVT_LE_META = 0x3E
HCI_LE_CONN_COMPLETE = 0x01
HCI_LE_ENHANCED_CONN_COMPLETE = 0x0A

L2CAP_CID_ATT = 0x0004

ATT_ERROR_RSP = 0x01
ATT_READ_BY_TYPE_REQ = 0x08
ATT_FIND_INFO_RSP = 0x05
ATT_READ_BY_TYPE_RSP = 0x09
ATT_READ_REQ = 0x0A
ATT_READ_RSP = 0x0B
ATT_WRITE_REQ = 0x12
ATT_WRITE_RSP = 0x13
ATT_WRITE_CMD = 0x52
ATT_NOTIFY = 0x1B
ATT_INDICATE = 0x1D


def iter_records(path):
    """Yield (timestamp_us, flags, data) for each HCI record."""
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != BTSNOOP_MAGIC:
            raise ValueError(f"Not a btsnoop file (magic={magic!r})")
        f.read(8)  # version, datalink
        while True:
            hdr = f.read(24)
            if len(hdr) < 24:
                break
            _orig_len, incl_len, flags, _drops = struct.unpack(">IIII", hdr[:16])
            ts_us = struct.unpack(">q", hdr[16:])[0]
            data = f.read(incl_len)
            yield ts_us, flags, data


def ts_to_str(ts_us):
    try:
        unix_us = ts_us - BTSNOOP_UNIX_EPOCH_DELTA_US
        dt = datetime.fromtimestamp(unix_us / 1_000_000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} UTC"
    except Exception:
        return f"+{ts_us}us"


def uuid_str(raw):
    """Format a 2-byte or 16-byte UUID."""
    if len(raw) == 2:
        return f"0x{struct.unpack('<H', raw)[0]:04X}"
    if len(raw) == 16:
        h = raw[::-1].hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return raw.hex()


def format_addr(raw):
    """Bluetooth addresses are carried little-endian in HCI events."""
    if len(raw) != 6:
        return None
    return ":".join(f"{b:02X}" for b in raw[::-1])


def parse_hci_event(data, connections):
    """Update connection metadata from HCI events."""
    if len(data) < 3 or data[0] != HCI_EVT:
        return

    event_code = data[1]
    params = data[3:3 + data[2]]

    if event_code == HCI_EVT_DISCONN_COMPLETE and len(params) >= 4:
        handle = struct.unpack_from("<H", params, 1)[0] & 0x0FFF
        connections.setdefault(handle, {})["disconnected"] = True
        return

    if event_code != HCI_EVT_LE_META or not params:
        return

    subevent = params[0]
    if subevent == HCI_LE_CONN_COMPLETE and len(params) >= 12:
        status = params[1]
        if status != 0:
            return
        handle = struct.unpack_from("<H", params, 2)[0] & 0x0FFF
        role = params[4]
        peer_addr_type = params[5]
        peer_addr = format_addr(params[6:12])
        connections.setdefault(handle, {}).update({
            "role": role,
            "peer_addr_type": peer_addr_type,
            "peer_addr": peer_addr,
        })
    elif subevent == HCI_LE_ENHANCED_CONN_COMPLETE and len(params) >= 30:
        status = params[1]
        if status != 0:
            return
        handle = struct.unpack_from("<H", params, 2)[0] & 0x0FFF
        role = params[4]
        peer_addr_type = params[5]
        peer_addr = format_addr(params[6:12])
        local_rpa = format_addr(params[12:18])
        peer_rpa = format_addr(params[18:24])
        connections.setdefault(handle, {}).update({
            "role": role,
            "peer_addr_type": peer_addr_type,
            "peer_addr": peer_addr,
            "local_rpa": local_rpa,
            "peer_rpa": peer_rpa,
        })


def parse_acl_header(data):
    if len(data) < 5 or data[0] != HCI_ACL:
        return None
    handle_flags = struct.unpack_from("<H", data, 1)[0]
    acl_len = struct.unpack_from("<H", data, 3)[0]
    return {
        "handle": handle_flags & 0x0FFF,
        "pb": (handle_flags >> 12) & 0x03,
        "bc": (handle_flags >> 14) & 0x03,
        "payload": data[5:5 + acl_len],
    }


def reassemble_l2cap(acl, direction, fragments):
    """
    Return (cid, payload) when a full L2CAP SDU is available, otherwise None.
    PB 0x01 is a continuation fragment. Other PB values can start an SDU.
    """
    key = (acl["handle"], direction)
    payload = acl["payload"]

    if acl["pb"] == 0x01:
        state = fragments.get(key)
        if not state:
            return None
        state["data"].extend(payload)
        if len(state["data"]) >= state["expected"]:
            full = bytes(state["data"][:state["expected"]])
            fragments.pop(key, None)
            return state["cid"], full[4:]
        return None

    if len(payload) < 4:
        return None

    l2cap_len = struct.unpack_from("<H", payload, 0)[0]
    cid = struct.unpack_from("<H", payload, 2)[0]
    expected = 4 + l2cap_len

    if len(payload) >= expected:
        return cid, payload[4:expected]

    fragments[key] = {
        "cid": cid,
        "expected": expected,
        "data": bytearray(payload),
    }
    return None


def parse_att(att, handle_map, att_state, direction):
    """Update handle_map and return decoded ATT event dictionaries."""
    if not att:
        return []

    opcode = att[0]
    payload = att[1:]
    events = []

    if opcode == ATT_READ_BY_TYPE_REQ and len(payload) >= 6:
        attr_type = uuid_str(payload[4:])
        if direction == "sent":
            att_state["pending_read_by_type"] = attr_type

    elif opcode == ATT_FIND_INFO_RSP and len(payload) >= 1:
        fmt = payload[0]
        uuid_len = 2 if fmt == 1 else 16
        i = 1
        while i + 2 + uuid_len <= len(payload):
            handle = struct.unpack_from("<H", payload, i)[0]
            handle_map[handle] = uuid_str(payload[i + 2:i + 2 + uuid_len])
            i += 2 + uuid_len

    elif opcode == ATT_READ_BY_TYPE_RSP and len(payload) >= 2:
        requested_type = att_state.pop("pending_read_by_type", None)
        if requested_type != "0x2803":
            return events
        item_len = payload[0]
        i = 1
        while item_len and i + item_len <= len(payload):
            attr_handle = struct.unpack_from("<H", payload, i)[0]
            val = payload[i + 2:i + item_len]
            if len(val) >= 3:
                char_handle = struct.unpack_from("<H", val, 1)[0]
                char_uuid = uuid_str(val[3:])
                handle_map[attr_handle] = "0x2803"
                handle_map[char_handle] = char_uuid
            i += item_len

    elif opcode == ATT_READ_REQ:
        if len(payload) >= 2:
            handle = struct.unpack_from("<H", payload, 0)[0]
            if direction == "sent":
                att_state["pending_read_handle"] = handle
            events.append({
                "att_opcode": opcode,
                "operation": "READ_REQ",
                "direction": "APP->CAM" if direction == "sent" else "CAM->APP",
                "att_handle": handle,
                "uuid": handle_map.get(handle, f"handle=0x{handle:04X}"),
                "value_hex": "",
                "value_text": "",
            })

    elif opcode == ATT_READ_RSP:
        handle = att_state.pop("pending_read_handle", 0)
        value = payload
        events.append({
            "att_opcode": opcode,
            "operation": "READ_RSP",
            "direction": "CAM->APP" if direction == "recv" else "APP->CAM",
            "att_handle": handle,
            "uuid": handle_map.get(handle, f"handle=0x{handle:04X}") if handle else "read-response",
            "value_hex": value.hex(),
            "value_text": value.decode(errors="replace"),
        })

    elif opcode == ATT_WRITE_RSP:
        events.append({
            "att_opcode": opcode,
            "operation": "WRITE_RSP",
            "direction": "CAM->APP" if direction == "recv" else "APP->CAM",
            "att_handle": 0,
            "uuid": "write-response",
            "value_hex": "",
            "value_text": "",
        })

    elif opcode in (ATT_WRITE_REQ, ATT_WRITE_CMD):
        if len(payload) >= 2:
            handle = struct.unpack_from("<H", payload, 0)[0]
            value = payload[2:]
            events.append({
                "att_opcode": opcode,
                "operation": "WRITE_REQ" if opcode == ATT_WRITE_REQ else "WRITE_CMD",
                "direction": "APP->CAM" if direction == "sent" else "CAM->APP",
                "att_handle": handle,
                "uuid": handle_map.get(handle, f"handle=0x{handle:04X}"),
                "value_hex": value.hex(),
                "value_text": value.decode(errors="replace"),
            })

    elif opcode in (ATT_NOTIFY, ATT_INDICATE):
        if len(payload) >= 2:
            handle = struct.unpack_from("<H", payload, 0)[0]
            value = payload[2:]
            events.append({
                "att_opcode": opcode,
                "operation": "NOTIFY" if opcode == ATT_NOTIFY else "INDICATE",
                "direction": "CAM->APP" if direction == "recv" else "APP->CAM",
                "att_handle": handle,
                "uuid": handle_map.get(handle, f"handle=0x{handle:04X}"),
                "value_hex": value.hex(),
                "value_text": value.decode(errors="replace"),
            })

    return events


def matches_filters(event, args):
    haystack = " ".join(str(event.get(k, "")) for k in (
        "time", "direction", "operation", "uuid", "value_hex", "value_text",
        "peer_addr", "conn_handle_hex",
    )).lower()

    if args.filter and args.filter.lower() not in haystack:
        return False
    if args.uuid and args.uuid.lower() not in str(event["uuid"]).lower():
        return False
    if args.direction and args.direction != event["direction"]:
        return False
    if args.att_handle is not None and args.att_handle != event["att_handle"]:
        return False
    if args.conn_handle is not None and args.conn_handle != event["conn_handle"]:
        return False
    if args.address:
        peer_addr = (event.get("peer_addr") or "").lower()
        peer_rpa = (event.get("peer_rpa") or "").lower()
        wanted = args.address.lower()
        if wanted not in (peer_addr, peer_rpa):
            return False
    return True


def parse_int(value):
    return int(value, 0)


def print_event(event):
    text = event["value_text"]
    print(
        f"  {event['time']}  hci={event['conn_handle_hex']:<6} "
        f"peer={event.get('peer_addr') or '?':<17} "
        f"[{event['direction']}] {event['operation']:<9} "
        f"att=0x{event['att_handle']:04X} uuid={event['uuid']} "
        f"value={event['value_hex']} ({text!r})"
    )


def main():
    parser = argparse.ArgumentParser(description="Parse btsnoop HCI log for BLE ATT activity")
    parser.add_argument("log", help="Path to btsnoop_hci.log")
    parser.add_argument("--filter", default=None,
                        help="Only show events containing this text")
    parser.add_argument("--uuid", default=None,
                        help="Only show events whose decoded UUID contains this text")
    parser.add_argument("--address", default=None,
                        help="Only show events for this peer Bluetooth address")
    parser.add_argument("--direction", choices=["APP->CAM", "CAM->APP"], default=None,
                        help="Only show one ATT direction")
    parser.add_argument("--att-handle", type=parse_int, default=None,
                        help="Only show one ATT handle, e.g. 0x001E")
    parser.add_argument("--conn-handle", type=parse_int, default=None,
                        help="Only show one HCI ACL connection handle, e.g. 0x0001")
    parser.add_argument("--jsonl", default=None,
                        help="Write matching decoded events to this JSONL file")
    args = parser.parse_args()

    connections = {}
    handle_maps = {}
    att_states = {}
    fragments = {}
    total = 0
    att_events = 0
    printed = 0

    jsonl = open(args.jsonl, "w", encoding="utf-8") if args.jsonl else None
    try:
        print(f"Parsing {args.log} ...\n")

        for ts_us, flags, data in iter_records(args.log):
            if not data:
                continue
            total += 1
            pkt_type = data[0]
            direction = "sent" if (flags & 0x01) == 0 else "recv"

            if pkt_type == HCI_EVT:
                parse_hci_event(data, connections)
                continue

            acl = parse_acl_header(data)
            if not acl:
                continue

            l2cap = reassemble_l2cap(acl, direction, fragments)
            if not l2cap:
                continue
            cid, att = l2cap
            if cid != L2CAP_CID_ATT:
                continue

            conn_handle = acl["handle"]
            handle_map = handle_maps.setdefault(conn_handle, {})
            att_state = att_states.setdefault(conn_handle, {})
            for event in parse_att(att, handle_map, att_state, direction):
                att_events += 1
                meta = connections.get(conn_handle, {})
                event.update({
                    "time": ts_to_str(ts_us),
                    "timestamp_us": ts_us,
                    "conn_handle": conn_handle,
                    "conn_handle_hex": f"0x{conn_handle:04X}",
                    "peer_addr": meta.get("peer_addr"),
                    "peer_rpa": meta.get("peer_rpa"),
                })
                if matches_filters(event, args):
                    print_event(event)
                    printed += 1
                    if jsonl:
                        jsonl.write(json.dumps(event, ensure_ascii=False) + "\n")

        print("\n-- Summary --")
        print(f"Total HCI records : {total}")
        print(f"ATT decoded events: {att_events}")
        print(f"Printed/exported  : {printed}")
        print(f"Connections seen  : {len(handle_maps)}")

        if handle_maps:
            print("\n-- Connection Handle Maps --")
            for conn_handle in sorted(handle_maps):
                meta = connections.get(conn_handle, {})
                peer = meta.get("peer_addr") or "?"
                print(f"  HCI 0x{conn_handle:04X} peer={peer}")
                for att_handle in sorted(handle_maps[conn_handle]):
                    print(f"    ATT 0x{att_handle:04X}  {handle_maps[conn_handle][att_handle]}")
    finally:
        if jsonl:
            jsonl.close()


if __name__ == "__main__":
    main()
