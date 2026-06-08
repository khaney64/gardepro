# BLE Wake Skip on Retry — Optimization Notes

**Date:** 2026-06-08  
**Branch:** main  
**Commit:** (applied after 75fac58)

---

## What was changed

### `_connection_flow(skip_ble_wake: bool = False)`
Added `skip_ble_wake` parameter. When `True` (attempt 2 only):
- Does a quick single WiFi scan to confirm hotspot is still visible; raises immediately if not ("Hotspot gone — will retry with full wake")
- Skips the BLE scan (`find_device`), wake pulse (`write_gatt_char`), and 60s hotspot wait loop
- Goes straight to WiFi association using `_last_camera_ssid` (remembered from attempt 1)

Attempt 3 always does the full BLE wake (`skip_ble_wake` only set for `_attempt == 2`), so it's a guaranteed fresh start if attempt 2 also fails.

`_last_camera_ssid: Optional[str]` (new module-level global) stores the SSID from attempt 1.

### `_connect_with_retry(label: str) -> bool`
New shared helper that runs `_connection_flow` with up to `AUTO_SYNC_RETRIES+1` attempts, logging with a caller-supplied prefix (`Auto-sync`, `Sync`, `Connect`). Returns `True` if connected, `False` if all attempts exhausted.

Previously only `_auto_sync_loop` had retry logic; manual connect and sync-now were single-shot (nearly always failing on first attempt due to DHCP timeout). All three now use this helper.

### `_auto_sync_loop`
Simplified to call `_connect_with_retry("Auto-sync")`. Added `for…else` sleep fix: after all attempts fail, sleeps `SYNC_INTERVAL` before the next cycle. Without this, the loop recalculated wait from `last_synced` (unchanged on failure) and spun immediately into another full retry cycle.

### `_connection_flow` exception handlers (`CancelledError` + `Exception`)
Now send `GET /cmd/standby/now` before WiFi disconnect when `_state["camera_ip"]` is set — i.e. when the camera was reached but something failed post-DHCP (e.g. HTTP verify timeout, mid-enumeration crash). Previously standby was only sent via `_disconnect_flow`; these error paths skipped it, leaving the camera awake.

### `api_connect` / `api_sync`
Both now use `_connect_with_retry` instead of a single `_connection_flow()` call.

---

## Why

Analysis of 344 auto-sync cycles over 4 days (Jun 4–8, 2026) showed:

| Outcome | Count | % |
|---|---|---|
| Succeeded on attempt 1 | 10 | 3% |
| Succeeded on attempt 2 | 295 | 86% |
| Succeeded on attempt 3 | 34 | 10% |
| Exhausted all 3 attempts | **5** | **1.5%** |

- **Root cause of retries: DHCP timeout on attempt 1** (363 DHCP timeouts across 344 cycles — nearly every first attempt fails at DHCP).
- DHCP when it succeeds: avg 11.1s, max 13.4s. Timeout threshold: ~42s.
- The camera hotspot stays up after a DHCP failure, so re-sending the BLE wake pulse on retry is redundant.

**Complete failures (all 3 attempts exhausted):**

| Time (UTC) | Next cycle succeeded |
|---|---|
| 2026-06-06 16:36:56 | 40s later |
| 2026-06-07 01:00:31 | 2m15s later |
| 2026-06-07 10:11:08 | 49s later |
| 2026-06-07 15:00:47 | 1m11s later |
| 2026-06-08 01:54:45 | 48s later |

In every case the next scheduled cycle connected immediately — the camera was on the edge and just needed slightly more time. No sustained outages.

**Time wasted per failed attempt 1:**
~10s BLE scan + ~4s wake pulse + 30s retry wait = ~44s of dead time that can be eliminated on attempt 2.

With 86% of cycles needing exactly one retry, this saves roughly **4–5 minutes of cumulative dead time per day**.

The 30s `AUTO_SYNC_RETRY_DELAY` was left unchanged for now — the camera may need a moment to reset its DHCP server between attempts. Could be revisited after seeing retry data with this change in place.

---

## Baseline stats (pre-change, for comparison)

- Log range: 2026-06-04 → 2026-06-08 (4 days)
- Total sync cycles: 344 (339 succeeded, 5 exhausted all retries)
- DHCP success time: min 2.4s / avg 11.1s / max 13.4s
- DHCP timeouts: 363 (≈1.05 per cycle — almost always fails on first try)
- Overall success rate: ~98.5% per cycle; 100% eventually (next cycle always recovered)

---

## How to revert

`git revert` the commit that introduced these changes, or manually:
- Remove `_connect_with_retry` and replace callers with direct `_connection_flow()` calls
- Remove `skip_ble_wake` parameter and its `if/else` branch from `_connection_flow`
- Remove `_last_camera_ssid` global
- Remove the standby calls from the `CancelledError` / `Exception` handlers
- Remove the `await asyncio.sleep(SYNC_INTERVAL)` after the retry loop in `_auto_sync_loop`

---

## What to watch for after deploying

- Does attempt 2 succeed more quickly (should save ~14s per failed cycle)?
- Any new failure mode where attempt 2 fails because the hotspot actually dropped?
- Does overall success rate stay at ~98.5% (5 total failures was baseline)?

Run the same `journalctl` analysis after a day or two to compare.
