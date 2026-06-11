# Refactor: image discovery via `/list/detail/backward` (match the official app)

## Goal
Replace the sequential per-ID probe in `web/server.py` `_enumerate_media()` with the
camera's native listing endpoint, the way the official GardePro app does it. The app does
**not** scan ID ranges — it asks the camera for a JSON manifest of what's on the card and
pages through it. This is fewer HTTP round-trips and is authoritative about what exists.

Scope is the **discovery mechanism only**. Everything downstream of discovery
(thumb caching, analysis, alerts, pending-deletion flush, DB schema) stays as-is.

## Endpoints (verified by sniffing the app; documented in CLAUDE.md)
- `GET /list/detail/backward/{ts}/{count}` — up to `{count}` items **older than** timestamp
  `{ts}`, returned **newest-first**. This is what the app opens with.
- `GET /list/detail/forward/{id}/{count}` — up to `{count}` items **forward from** `{id}`.
  (Not needed for this refactor; backward paging covers it. Mentioned for completeness.)

## ⚠️ MUST CONFIRM FIRST — the JSON response schema is unknown
The capture pcaps are gone, so the exact field names in the listing response are **not
documented**. Before writing the parser, capture one real response and record its shape.
With the camera awake and the Pi connected to its hotspot (192.168.8.1:8080):

```bash
# From the Pi while connected to the camera WiFi. Use a large ts to get the newest page.
python3 - <<'PY'
import requests, json
s = requests.Session(); s.trust_env = False
r = s.get("http://192.168.8.1:8080/list/detail/backward/9999999999/20", timeout=5)
print(r.status_code, r.headers.get("content-type"))
print(json.dumps(r.json(), indent=2)[:2000])
PY
```

Record and then map these fields into the parser:
- **item id** (the `{id}` used by `/file/{id}/JPG`, `/thumb/{id}/...`, `/cmd/delete/{id}/...`)
- **kind** (image vs video — how the app distinguishes jpg from mp4; may be a `type`/`ext`
  field, or you keep deriving it from the `/thumb` suffix as today)
- **timestamp** → maps to our `captured_at` (note the format: epoch seconds? a string?
  the same format `{ts}` expects for the next backward call?)
- whether the list is under a top-level key (e.g. `{"data":[...]}`) or a bare array
- what an **empty / end-of-card** response looks like (empty array vs. error code)

Confirm the meaning and unit of `{ts}`: it's whatever value you pass to get the *next,
older* page. Easiest is to take the **smallest timestamp** from the current page and pass
it (minus 1 if the boundary is inclusive) as `{ts}` for the next call. If the camera
actually keys paging off the last **id** rather than timestamp, use
`/list/detail/forward`/`backward` accordingly — verify by observing whether passing the
page's min-timestamp returns the next older batch without duplicates or gaps.

## New algorithm for `_enumerate_media()`
Page backward from "newest", accumulating items, until the camera returns fewer than
`PAGE` items (end of card) **or** an entire page is already known to the DB (incremental
stop — we've caught up to what we already have). Keep `PAGE` modest, e.g. 20–50.

```
PAGE = 25
ts = <max sentinel that returns the newest page>   # confirm the camera accepts this
results = []
seen_ids = set()
known = set of (id, kind) already in DB           # from a cheap DB query, see below
while True:
    page = GET /list/detail/backward/{ts}/{PAGE}   # parse JSON → list of {id, kind, captured_at}
    if page is empty: break
    new_in_page = 0
    for item in page:
        if item.id in seen_ids: continue           # guard against boundary dupes
        seen_ids.add(item.id)
        results.append(item)
        if (item.id, item.kind) not in known: new_in_page += 1
    # incremental stop: a full page we already had → nothing new beyond here
    if len(page) < PAGE: break                      # reached end of card
    if new_in_page == 0: break                       # caught up to known history
    ts = <oldest timestamp in page, adjusted for the next backward call>
```

Notes:
- `results` ends up newest-first; the rest of the code doesn't care about order, but if
  any downstream assumes ascending id, sort before returning (`results.sort(key=id)`).
- The incremental stop is the efficiency win and mirrors the app. If you'd rather be
  maximally safe on the first cut, you may skip the `new_in_page == 0` early-out and always
  page to end-of-card; it's just a few more calls. Recommend keeping the early-out.

## Preserve these behaviors (do not regress)
1. **Empty-scan guard.** If the listing call fails or yields zero items, log
   `"Media scan returned no results — preserving cached gallery"` and `return results`
   **without** wiping `_media` or the DB. (Same as current lines ~493–497.)
2. **Incremental progress broadcast.** Keep pushing `{"type":"media_progress","count":N}`
   and updating `_media` / `_state["media_count"]` as pages come in (today it broadcasts
   every 6 items — per-page is fine and simpler).
3. **DB sync block unchanged.** Keep the existing `_sync_to_db` logic verbatim:
   `_db.upsert_media(id, kind, captured_at)` per item, `_db.set_scan_hwm(max id)`,
   `_db.set_last_synced()`, refresh `_state["last_synced"]` / `_state["last_event"]`, then
   filter out `_db.get_pending_deletions()` from `_media`. `upsert_media()` already handles
   on-camera ID reuse via `captured_at` comparison — keep feeding it `captured_at`.
4. **`captured_at`.** Prefer the timestamp from the listing JSON. If that field turns out
   to be missing/unreliable, fall back to the current approach of reading the
   `Last-Modified` header from a `/file/{id}/JPG` HEAD/GET (`_parse_http_date`). Don't probe
   every file just for the date if the list already provides it.
5. **HWM floor.** The high-water-mark (`_db.get_scan_hwm`) and `get_max_media_id` exist to
   anchor the old probe's stop condition. The listing approach is authoritative, so the
   floor is no longer needed to decide when to stop. Still call `_db.set_scan_hwm(max id)`
   after a successful sync (point 3) so other code depending on the HWM keeps working.

## Fallback / safety
Wrap the listing path in try/except. If `/list/detail/backward` errors, returns non-JSON,
or the parsed schema doesn't match (e.g. firmware variation), **fall back to the existing
sequential `/file/{id}/JPG` probe loop** rather than failing the sync. Easiest is to keep
the old loop as a private helper `_enumerate_media_probe()` and have `_enumerate_media()`
try the listing first, falling back on exception. This keeps the deletion-gap robustness of
the probe as a safety net.

## Out of scope (call out, don't implement unless asked)
- **Pruning stale DB rows.** Because the listing is authoritative, you *could* delete DB
  rows whose ids no longer appear on the camera (true on-camera deletions). Today deletions
  are handled only through the `pending_delete` flush flow, and enumerate is purely
  additive. Changing to prune-on-enumerate is a behavior change — leave it out of this
  refactor unless the user asks.

## Call sites (no signature change needed)
`_enumerate_media()` is `async` and returns `list[dict]`. Keep that signature. Callers:
`_connection_flow` (~line 810), `_sync_cache` (line 554), and the cached-fallback path
(~line 961). None need changes if the return contract (`[{"id","kind","captured_at"}]`)
is preserved.

## Verification
1. Print the live listing JSON (the capture snippet above) and confirm the field mapping.
2. Connect with a known card; confirm `media_count` matches the app's gallery count and
   that the newest images appear.
3. Delete an image on the camera, reconnect; confirm it drops out of the gallery (it simply
   won't be in the listing) and that the sync doesn't error.
4. Confirm `journalctl -u gardepro -f` shows the new path making a handful of
   `/list/detail/backward` calls instead of hundreds of `/file/{id}/JPG` probes.
5. Force the fallback (temporarily point the listing URL at a bad path) and confirm the
   probe loop still enumerates.
