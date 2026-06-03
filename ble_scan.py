"""Scan for nearby BLE devices and print name, address, RSSI."""
import asyncio
from bleak import BleakScanner


async def scan():
    print("Scanning for 10 seconds — make sure the camera is powered on...\n")
    results = await BleakScanner.discover(timeout=10, return_adv=True)
    if not results:
        print("No BLE devices found.")
        return
    # Sort by RSSI descending (strongest signal first)
    rows = sorted(
        ((d, adv) for d, adv in results.values()),
        key=lambda x: x[1].rssi,
        reverse=True,
    )
    print(f"{'ADDRESS':<20} {'RSSI':>5}  NAME")
    print("-" * 60)
    for device, adv in rows:
        name = device.name or "(unknown)"
        print(f"{device.address:<20} {adv.rssi:>5}  {name}")


asyncio.run(scan())
